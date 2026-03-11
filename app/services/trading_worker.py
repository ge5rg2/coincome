"""
TradingWorker: 사용자별 백그라운드 매매 루프.

상태 영속성 (State Persistence):
- 매수 체결 직후 buy_price / amount_coin 을 DB에 저장.
- 서버 재시작 시 _decide_entry()가 DB 값을 확인해:
    * buy_price 존재 → 포지션 복구 후 매도 감시 루프로 진입.
    * buy_price 없음 → 신규 시장가 매수 후 루프 진입.
- 매도 체결 또는 수동 중지 시 is_running=False, buy_price/amount_coin=NULL 로 초기화.
- 서버 강제 종료(CancelledError)는 DB를 건드리지 않아 다음 기동 시 복구 가능.

시세 조회:
- UpbitWebsocketManager 메모리 캐시 전용. REST 호출 없음.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.bot_setting import BotSetting
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
        """태스크만 취소한다. DB는 건드리지 않는다 (재시작 복구 보존)."""
        if self._task:
            self._task.cancel()
        logger.info("워커 태스크 취소: user=%s symbol=%s", self.user_id, self.symbol)

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------
    # 내부 루프
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        try:
            # DB 상태 확인 → 신규 매수 또는 기존 포지션 복구
            await self._decide_entry()
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                if self._position:
                    await self._check_exit_conditions()
        except asyncio.CancelledError:
            # 서버 종료(재시작) 또는 stop() 호출 — DB는 건드리지 않음.
            # 재시작 후 복구를 위해 buy_price / amount_coin 보존.
            logger.info("워커 취소됨: setting_id=%s (DB 상태 보존)", self.setting_id)
        except Exception as exc:
            logger.exception("워커 오류: setting_id=%s error=%s", self.setting_id, exc)
            await self.notify(self.user_id, f"⚠️ [{self.symbol}] 봇 오류 발생: {exc}")
            # 예외로 인한 비정상 종료는 DB 정리 (수동 재시작 유도)
            await self._clear_position_from_db()

    # ------------------------------------------------------------------
    # 신규 진입 vs 복구 결정
    # ------------------------------------------------------------------

    async def _decide_entry(self) -> None:
        """
        DB의 buy_price / amount_coin 존재 여부로 진입 방식을 결정한다.

        [신규 진입] buy_price IS NULL  → 시장가 매수 실행 후 DB에 기록.
        [상태 복구] buy_price IS NOT NULL → DB 값으로 Position 복원 후
                    매도 감시 루프로 바로 진입 (중복 매수 방지).
        """
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(BotSetting).where(BotSetting.id == self.setting_id)
            )
            setting = result.scalar_one_or_none()

        if setting is None:
            raise RuntimeError(f"BotSetting 레코드 없음: id={self.setting_id}")

        if setting.buy_price is not None and setting.amount_coin is not None:
            # ── 상태 복구 경로 ──────────────────────────────────────
            self._position = Position(
                symbol=self.symbol,
                buy_price=float(setting.buy_price),
                amount_coin=float(setting.amount_coin),
                buy_amount_krw=self.buy_amount_krw,
                target_profit_pct=self.target_profit_pct,
                stop_loss_pct=self.stop_loss_pct,
            )
            logger.info(
                "포지션 복구: setting_id=%s symbol=%s buy_price=%.0f amount_coin=%.6f",
                self.setting_id, self.symbol,
                setting.buy_price, setting.amount_coin,
            )
            await self.notify(
                self.user_id,
                f"🔄 **포지션 복구** `{self.symbol}`\n"
                f"매수가: {float(setting.buy_price):,.0f} KRW  |  "
                f"수량: {float(setting.amount_coin):.6f}\n"
                f"매도 감시를 재개합니다.",
            )
        else:
            # ── 신규 진입 경로 ──────────────────────────────────────
            await self._buy()

    # ------------------------------------------------------------------
    # 매수
    # ------------------------------------------------------------------

    async def _wait_for_ws_price(self) -> float:
        """WebSocket 캐시에 현재가가 수신될 때까지 최대 PRICE_WAIT_TIMEOUT 초 대기."""
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
        """시장가 매수 실행 후 체결 결과를 DB에 저장한다."""
        current_price = await self._wait_for_ws_price()
        order = await self.exchange.create_market_buy_order(self.symbol, self.buy_amount_krw)
        amount_coin = float(order.get("filled", 0)) or (self.buy_amount_krw / current_price)

        # 포지션 메모리 기록
        self._position = Position(
            symbol=self.symbol,
            buy_price=current_price,
            amount_coin=amount_coin,
            buy_amount_krw=self.buy_amount_krw,
            target_profit_pct=self.target_profit_pct,
            stop_loss_pct=self.stop_loss_pct,
        )

        # ── 체결 결과 DB 저장 (재시작 복구 기준값) ──────────────────
        await self._save_position_to_db(current_price, amount_coin)

        await self.notify(
            self.user_id,
            f"✅ **매수 체결** `{self.symbol}`\n"
            f"매수가: {current_price:,.0f} KRW\n"
            f"수량: {amount_coin:.6f}\n"
            f"투자금액: {self.buy_amount_krw:,.0f} KRW",
        )

    # ------------------------------------------------------------------
    # 매도 감시
    # ------------------------------------------------------------------

    async def _check_exit_conditions(self) -> None:
        """
        WebSocket 캐시의 현재가로 익절/손절 조건을 판단한다.
        REST 호출 없음 — Rate Limit 영향 없음.
        """
        pos = self._position
        if pos is None:
            return

        current_price = UpbitWebsocketManager.get().get_price(self.symbol)
        if current_price is None:
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

    # ------------------------------------------------------------------
    # 매도
    # ------------------------------------------------------------------

    async def _sell(self, current_price: float, profit_pct: float, reason: str) -> None:
        """시장가 매도 실행 후 DB를 초기화하고 Discord 알림을 전송한다."""
        pos = self._position
        if pos is None:
            return

        order = await self.exchange.create_market_sell_order(self.symbol, pos.amount_coin)
        sell_price = float(order.get("average", current_price))
        realized_pnl = (sell_price - pos.buy_price) * pos.amount_coin

        # ── DB 초기화 (is_running=False, buy_price/amount_coin=NULL) ──
        await self._clear_position_from_db()

        await self.notify(
            self.user_id,
            f"{'🟢' if realized_pnl >= 0 else '🔴'} **매도 체결** `{self.symbol}` — {reason}\n"
            f"매수가: {pos.buy_price:,.0f} KRW  →  매도가: {sell_price:,.0f} KRW\n"
            f"수익률: **{profit_pct:+.2f}%** | 손익: {realized_pnl:+,.0f} KRW",
        )
        self._position = None
        self.stop()

    # ------------------------------------------------------------------
    # DB 헬퍼
    # ------------------------------------------------------------------

    async def _save_position_to_db(self, buy_price: float, amount_coin: float) -> None:
        """매수 체결 후 buy_price / amount_coin 을 DB에 기록한다."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(BotSetting).where(BotSetting.id == self.setting_id)
            )
            setting = result.scalar_one_or_none()
            if setting:
                setting.buy_price = buy_price
                setting.amount_coin = amount_coin
                await db.commit()
        logger.info(
            "포지션 DB 저장: setting_id=%s buy_price=%.0f amount_coin=%.6f",
            self.setting_id, buy_price, amount_coin,
        )

    async def _clear_position_from_db(self) -> None:
        """
        매도 완료 또는 수동 중지 시 DB를 초기화한다.
        is_running=False, buy_price=NULL, amount_coin=NULL
        """
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(BotSetting).where(BotSetting.id == self.setting_id)
            )
            setting = result.scalar_one_or_none()
            if setting:
                setting.is_running = False
                setting.buy_price = None
                setting.amount_coin = None
                await db.commit()
        logger.info("포지션 DB 초기화: setting_id=%s", self.setting_id)


# ------------------------------------------------------------------
# 전역 워커 레지스트리 (싱글턴)
# ------------------------------------------------------------------


class WorkerRegistry:
    """애플리케이션 전체 TradingWorker를 관리하는 싱글턴 레지스트리.

    워커 등록/해제 시 UpbitWebsocketManager 구독 목록도 함께 동기화한다.
    수동 중지 시 _clear_position_from_db()를 먼저 호출해 DB를 정리한다.
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
        """워커를 수동 제거한다. DB 상태를 초기화한 후 태스크를 취소한다."""
        worker = self._workers.pop(setting_id, None)
        if worker:
            # 수동 중지: DB 초기화 후 취소 (재시작 복구 대상에서 제외)
            await worker._clear_position_from_db()
            worker.stop()
            await UpbitWebsocketManager.get().subscribe(self.active_symbols())

    def get_worker(self, setting_id: int) -> TradingWorker | None:
        return self._workers.get(setting_id)

    async def stop_all_for_user(self, user_id: str) -> None:
        """특정 유저의 모든 워커를 수동 중지하고 DB·WebSocket 구독을 정리한다."""
        to_stop = [w for w in self._workers.values() if w.user_id == user_id]
        for w in to_stop:
            # 수동 중지: DB 초기화 후 취소 (재시작 복구 대상에서 제외)
            await w._clear_position_from_db()
            w.stop()
            self._workers.pop(w.setting_id, None)
        await UpbitWebsocketManager.get().subscribe(self.active_symbols())
