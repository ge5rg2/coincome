"""
TradingWorker: 사용자별 백그라운드 매매 루프.

변경 사항 (WebSocket 리팩토링):
- 시세 조회를 REST API(fetch_current_price) 대신 UpbitWebsocketManager의
  메모리 캐시(current_prices)에서 읽도록 변경.
- 폴링 주기 0.5초 → 1.0초 (메모리 참조이므로 부하 무의미).
- ExchangeService는 매수/매도 주문 실행과 잔고 조회에만 사용.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.services.exchange import ExchangeService
from app.services.websocket import UpbitWebsocketManager

logger = logging.getLogger(__name__)

POLL_INTERVAL = 1.0       # 초 (메모리 읽기 — CPU 부하 없음)
PRICE_WAIT_TIMEOUT = 5.0  # 초 — 매수 전 WebSocket 가격 수신 대기 최대 시간


@dataclass
class Position:
    """매수 후 보유 중인 포지션 정보"""
    symbol: str
    buy_price: float
    amount_coin: float
    buy_amount_krw: float
    target_profit_pct: float | None = None
    stop_loss_pct: float | None = None

    @property
    def target_price(self) -> float | None:
        """익절 목표가. target_profit_pct가 없으면 None."""
        if self.target_profit_pct is None:
            return None
        return self.buy_price * (1 + self.target_profit_pct / 100)

    @property
    def stop_price(self) -> float | None:
        """손절 기준가. stop_loss_pct가 없으면 None."""
        if self.stop_loss_pct is None:
            return None
        return self.buy_price * (1 - self.stop_loss_pct / 100)


class TradingWorker:
    """사용자 한 명의 BotSetting에 대응하는 워커.

    시세는 UpbitWebsocketManager의 current_prices 캐시를 직접 참조하며,
    매수/매도 주문만 ExchangeService(REST API)를 통해 실행한다.
    """

    def __init__(
        self,
        setting_id: int,
        user_id: str,
        symbol: str,
        buy_amount_krw: float,
        target_profit_pct: float | None,
        stop_loss_pct: float | None,
        exchange: ExchangeService,
        notify_callback,  # async def callback(user_id: str, msg: str)
    ) -> None:
        self.setting_id = setting_id
        self.user_id = user_id
        self.symbol = symbol
        self.buy_amount_krw = buy_amount_krw
        self.target_profit_pct = target_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.exchange = exchange
        self.notify = notify_callback

        self._task: asyncio.Task | None = None
        self._position: Position | None = None

    # ------------------------------------------------------------------
    # 라이프사이클
    # ------------------------------------------------------------------

    def start(self) -> None:
        """워커 태스크를 시작한다. 이미 실행 중이면 무시."""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run(), name=f"worker-{self.setting_id}"
        )
        logger.info("워커 시작: user=%s symbol=%s", self.user_id, self.symbol)

    def stop(self) -> None:
        """워커 태스크를 취소한다."""
        if self._task:
            self._task.cancel()
        logger.info("워커 중지: user=%s symbol=%s", self.user_id, self.symbol)

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------
    # 내부 루프
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        try:
            await self._buy()
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                if self._position:
                    await self._check_exit_conditions()
        except asyncio.CancelledError:
            logger.info("워커 취소됨: setting_id=%s", self.setting_id)
        except Exception as exc:
            logger.exception("워커 오류: setting_id=%s error=%s", self.setting_id, exc)
            await self.notify(self.user_id, f"⚠️ [{self.symbol}] 봇 오류 발생: {exc}")

    async def _wait_for_ws_price(self) -> float:
        """
        UpbitWebsocketManager의 캐시에 현재가가 수신될 때까지 대기한다.

        WebSocket이 막 연결된 직후에는 캐시가 비어있을 수 있으므로,
        PRICE_WAIT_TIMEOUT 초까지 0.1초 간격으로 재시도한다.

        Raises:
            RuntimeError: 타임아웃 내에 가격을 수신하지 못한 경우
        """
        ws_manager = UpbitWebsocketManager.get()
        deadline = asyncio.get_event_loop().time() + PRICE_WAIT_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            price = ws_manager.get_price(self.symbol)
            if price is not None:
                return price
            await asyncio.sleep(0.1)
        raise RuntimeError(
            f"현재가 수신 타임아웃: {self.symbol} ({PRICE_WAIT_TIMEOUT}s 초과)"
        )

    async def _buy(self) -> None:
        """WebSocket 캐시에서 현재가를 읽어 시장가 매수 주문을 실행한다."""
        # 매수 기준가: WebSocket 캐시에서 읽기 (REST 호출 없음)
        current_price = await self._wait_for_ws_price()
        order = await self.exchange.create_market_buy_order(self.symbol, self.buy_amount_krw)
        # 체결 수량: 주문 응답에 filled 값이 있으면 사용, 없으면 추정
        amount_coin = float(order.get("filled", 0)) or (self.buy_amount_krw / current_price)

        self._position = Position(
            symbol=self.symbol,
            buy_price=current_price,
            amount_coin=amount_coin,
            buy_amount_krw=self.buy_amount_krw,
            target_profit_pct=self.target_profit_pct,
            stop_loss_pct=self.stop_loss_pct,
        )
        msg = (
            f"✅ **매수 체결** `{self.symbol}`\n"
            f"매수가: {current_price:,.0f} KRW\n"
            f"수량: {amount_coin:.6f}\n"
            f"투자금액: {self.buy_amount_krw:,.0f} KRW"
        )
        await self.notify(self.user_id, msg)

    async def _check_exit_conditions(self) -> None:
        """
        WebSocket 캐시의 현재가로 익절/손절 조건을 판단한다.

        현재가가 아직 캐시에 없으면 해당 틱을 건너뛴다.
        REST API를 호출하지 않으므로 Rate Limit에 영향 없음.
        """
        pos = self._position
        if pos is None:
            return

        # ── 시세 조회: REST 호출 없이 메모리 캐시에서 읽기 ──────────
        current_price = UpbitWebsocketManager.get().get_price(self.symbol)
        if current_price is None:
            # WebSocket 재연결 직후 등 일시적으로 캐시 미존재 → 다음 틱에 재시도
            logger.debug("현재가 캐시 없음, 스킵: symbol=%s", self.symbol)
            return

        profit_pct = (current_price - pos.buy_price) / pos.buy_price * 100

        should_sell = False
        reason = ""

        if pos.target_price and current_price >= pos.target_price:
            should_sell = True
            reason = f"🎯 익절 ({profit_pct:+.2f}%)"
        elif pos.stop_price and current_price <= pos.stop_price:
            should_sell = True
            reason = f"🛑 손절 ({profit_pct:+.2f}%)"

        if should_sell:
            await self._sell(current_price, profit_pct, reason)

    async def _sell(self, current_price: float, profit_pct: float, reason: str) -> None:
        """시장가 매도 주문 실행 후 Discord 알림 전송."""
        pos = self._position
        if pos is None:
            return

        order = await self.exchange.create_market_sell_order(self.symbol, pos.amount_coin)
        sell_price = float(order.get("average", current_price))
        realized_pnl = (sell_price - pos.buy_price) * pos.amount_coin

        msg = (
            f"{'🟢' if realized_pnl >= 0 else '🔴'} **매도 체결** `{self.symbol}` — {reason}\n"
            f"매수가: {pos.buy_price:,.0f} KRW  →  매도가: {sell_price:,.0f} KRW\n"
            f"수익률: **{profit_pct:+.2f}%** | 손익: {realized_pnl:+,.0f} KRW"
        )
        await self.notify(self.user_id, msg)
        self._position = None
        self.stop()


# ------------------------------------------------------------------
# 전역 워커 레지스트리 (싱글턴)
# ------------------------------------------------------------------

class WorkerRegistry:
    """애플리케이션 전체 TradingWorker를 관리하는 싱글턴 레지스트리.

    워커를 등록/해제할 때 UpbitWebsocketManager의 구독 목록도 함께 동기화한다.
    """

    _instance: "WorkerRegistry | None" = None
    _workers: dict[int, TradingWorker] = {}

    @classmethod
    def get(cls) -> "WorkerRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def active_symbols(self) -> set[str]:
        """현재 실행 중인 워커들이 감시하는 심볼 집합을 반환한다."""
        return {w.symbol for w in self._workers.values() if w.is_running}

    async def register(self, worker: TradingWorker) -> None:
        """워커를 등록하고 WebSocket 구독을 추가한다."""
        self._workers[worker.setting_id] = worker
        await UpbitWebsocketManager.get().add_symbol(worker.symbol)

    async def unregister(self, setting_id: int) -> None:
        """워커를 제거하고 필요 시 WebSocket 구독을 해제한다."""
        worker = self._workers.pop(setting_id, None)
        if worker:
            worker.stop()
            # 동일 심볼을 감시하는 다른 워커가 없으면 구독 해제
            await UpbitWebsocketManager.get().subscribe(self.active_symbols())

    def get_worker(self, setting_id: int) -> TradingWorker | None:
        return self._workers.get(setting_id)

    async def stop_all_for_user(self, user_id: str) -> None:
        """특정 유저의 모든 워커를 중지하고 WebSocket 구독을 정리한다."""
        to_stop = [w for w in self._workers.values() if w.user_id == user_id]
        for w in to_stop:
            w.stop()
            self._workers.pop(w.setting_id, None)
        # 남은 활성 심볼로 구독 목록 재동기화
        await UpbitWebsocketManager.get().subscribe(self.active_symbols())
