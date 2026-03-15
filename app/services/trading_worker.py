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
from app.models.trade_history import TradeHistory
from app.models.user import User
from app.services.exchange import ExchangeService
from app.services.websocket import UpbitWebsocketManager

logger = logging.getLogger(__name__)

POLL_INTERVAL = 1.0        # 초 (메모리 읽기 — CPU 부하 없음)
PRICE_WAIT_TIMEOUT = 5.0   # 초 — 매수 전 WebSocket 가격 수신 대기 최대 시간
MIN_SELL_ORDER_KRW = 5_000  # 업비트 최소 주문 금액 — 이 미만이면 매도 불가 판단


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
        exchange: ExchangeService | None,
        notify_callback,  # async def callback(user_id: str, msg: str)
        is_paper_trading: bool = False,
    ) -> None:
        self.setting_id = setting_id
        self.user_id = user_id
        self.symbol = symbol
        self.buy_amount_krw = buy_amount_krw
        self.target_profit_pct = target_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.exchange = exchange
        self.notify = notify_callback
        self.is_paper_trading = is_paper_trading  # True = 가상 매매, False = 실거래

        self._task: asyncio.Task | None = None
        self._position: Position | None = None
        self._db_refresh_counter: int = 0   # _check_exit_conditions 호출 횟수 카운터

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

    async def force_sell(self, reason: str = "🤖 AI 강제 청산") -> bool:
        """AI 리뷰에 의한 강제 시장가 청산을 수행한다.

        _review_existing_positions 에서 action=SELL 판정 시 외부에서 직접 호출한다.
        내부 _sell() 메서드를 트리거하므로 DB 초기화·TradeHistory INSERT·
        가상 잔고 복원·DM 알림이 일반 익절/손절과 동일한 흐름으로 처리된다.

        Args:
            reason: 매도 사유 문자열 (DM 알림 및 로그에 포함됨).

        Returns:
            True  = 청산 성공
            False = 포지션 없거나 현재가 없어 청산 불가
        """
        if self._position is None:
            logger.warning(
                "force_sell: 포지션 없음 (이미 청산됨): setting_id=%s", self.setting_id
            )
            return False

        current_price = UpbitWebsocketManager.get().get_price(self.symbol)
        if current_price is None:
            logger.warning(
                "force_sell: 현재가 없음 (강제 청산 스킵): setting_id=%s symbol=%s",
                self.setting_id, self.symbol,
            )
            return False

        profit_pct = (
            (current_price - self._position.buy_price) / self._position.buy_price * 100
        )
        logger.info(
            "AI 강제 청산 트리거: setting_id=%s symbol=%s profit_pct=%.2f%% reason=%s",
            self.setting_id, self.symbol, profit_pct, reason,
        )
        await self._sell(current_price, profit_pct, reason)
        return True

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
            mode_tag = "🎮 [모의투자] " if self.is_paper_trading else ""
            await self.notify(
                self.user_id,
                f"🔄 **{mode_tag}포지션 복구** `{self.symbol}`\n"
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
        """시장가 매수 실행 후 체결 결과를 DB에 저장한다.

        업비트 시장가 매수 수수료(0.05%) 및 소폭의 슬리피지를 감안해
        실제 주문 금액은 유저 설정 금액의 99.9%만 사용한다.
        포지션 기록·수익률 계산 기준은 유저가 의도한 원래 금액(buy_amount_krw)으로 유지한다.

        모의투자(is_paper_trading=True) 시에는 업비트 API를 호출하지 않고
        슬리피지 0.1%가 반영된 가상 체결가로 포지션을 기록한다.
        """
        current_price = await self._wait_for_ws_price()

        # ── 수수료·슬리피지 안전 버퍼 (0.1% 차감) ───────────────────
        # 업비트는 원화 주문 금액에 소수점을 허용하지 않으므로 int() 처리
        safe_buy_amount: int = int(self.buy_amount_krw * 0.999)

        # ════════════════════════════════════════════════════════════
        # 🎮 모의투자 분기 — 실제 API 호출 없이 가상 체결 처리
        # ════════════════════════════════════════════════════════════
        if self.is_paper_trading:
            # 시장가 매수 슬리피지 0.1% 반영한 가상 체결가
            fill_price = current_price * 1.001
            amount_coin = safe_buy_amount / fill_price

            # 가상 잔고 차감
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(User).where(User.user_id == self.user_id))
                user = result.scalar_one_or_none()
                if user is not None:
                    user.virtual_krw = float(user.virtual_krw) - safe_buy_amount
                    await db.commit()
                    logger.info(
                        "[모의투자] 가상 잔고 차감: user=%s amount=%d remaining=%.0f",
                        self.user_id, safe_buy_amount, user.virtual_krw,
                    )

            # 포지션 메모리 기록 (슬리피지 반영된 fill_price 기준)
            self._position = Position(
                symbol=self.symbol,
                buy_price=fill_price,
                amount_coin=amount_coin,
                buy_amount_krw=self.buy_amount_krw,
                target_profit_pct=self.target_profit_pct,
                stop_loss_pct=self.stop_loss_pct,
            )
            await self._save_position_to_db(fill_price, amount_coin)

            await self.notify(
                self.user_id,
                f"🎮 **[모의투자] 매수 체결** `{self.symbol}`\n"
                f"매수가: {fill_price:,.0f} KRW (슬리피지 0.1% 반영)\n"
                f"수량: {amount_coin:.6f}\n"
                f"투자금액: {safe_buy_amount:,.0f} KRW",
            )
            return
        # ════════════════════════════════════════════════════════════

        # ── 실거래: 업비트 시장가 매수 API 호출 ──────────────────────
        order = await self.exchange.create_market_buy_order(self.symbol, safe_buy_amount)
        amount_coin = float(order.get("filled", 0)) or (safe_buy_amount / current_price)

        # 포지션 메모리 기록
        # buy_amount_krw 는 유저가 설정한 원래 금액을 유지 (수익률 계산 기준 일관성)
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
            f"투자금액: {safe_buy_amount:,.0f} KRW (수수료 여유분 0.1% 제외)",
        )

    # ------------------------------------------------------------------
    # 매도 감시
    # ------------------------------------------------------------------

    async def _check_exit_conditions(self) -> None:
        """
        WebSocket 캐시의 현재가로 익절/손절 조건을 판단한다.
        REST 호출 없음 — Rate Limit 영향 없음.

        약 1분(60회 폴링)마다 DB에서 target_profit_pct·stop_loss_pct 를 재조회해
        AI 펀드 매니저가 외부에서 변경한 목표값을 인메모리에 동기화한다.
        """
        pos = self._position
        if pos is None:
            return

        # ── 주기적 DB 갱신 (60회 = POLL_INTERVAL 기준 약 1분) ──────
        self._db_refresh_counter += 1
        if self._db_refresh_counter >= 60:
            self._db_refresh_counter = 0
            await self._refresh_targets_from_db()

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

    async def _refresh_targets_from_db(self) -> None:
        """DB에서 target_profit_pct·stop_loss_pct 를 재조회해 인메모리를 최신화한다.

        AI 펀드 매니저가 주기적으로 DB를 업데이트할 수 있으므로,
        워커도 1분 간격으로 DB 값을 재확인해 갭을 없앤다.
        """
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(BotSetting).where(BotSetting.id == self.setting_id)
                )
                setting = result.scalar_one_or_none()

            if setting is None:
                return

            new_tgt = float(setting.target_profit_pct) if setting.target_profit_pct is not None else None
            new_sl  = float(setting.stop_loss_pct)     if setting.stop_loss_pct     is not None else None

            changed = (
                new_tgt != self.target_profit_pct
                or new_sl != self.stop_loss_pct
            )

            # 인메모리 워커 값 갱신
            self.target_profit_pct = new_tgt
            self.stop_loss_pct      = new_sl

            # Position 객체도 동기화 (목표가·손절가 즉시 반영)
            if self._position:
                self._position.target_profit_pct = new_tgt
                self._position.stop_loss_pct      = new_sl

            if changed:
                logger.info(
                    "워커 목표값 DB 재동기화: setting_id=%s tgt=%.1f%% sl=%.1f%%",
                    self.setting_id,
                    new_tgt if new_tgt is not None else 0.0,
                    new_sl  if new_sl  is not None else 0.0,
                )

        except Exception as exc:
            # DB 재조회 실패는 치명적이지 않으므로 WARNING 수준으로만 기록
            logger.warning(
                "워커 목표값 DB 재조회 실패 (기존 값 유지): setting_id=%s err=%s",
                self.setting_id, exc,
            )

    # ------------------------------------------------------------------
    # 매도
    # ------------------------------------------------------------------

    async def _sell(self, current_price: float, profit_pct: float, reason: str) -> None:
        """실제 잔고를 확인한 뒤 수량을 보정하여 시장가 매도를 실행한다.

        insufficient_funds_ask 방지를 위해 매도 직전 업비트 실제 free 잔고를 조회하고
        세 가지 경로로 분기한다.

        1. 잔고 조회 실패(네트워크 오류): 이번 사이클 건너뛰고 다음 주기에 재시도.
        2. 잔고 × 현재가 < MIN_SELL_ORDER_KRW: 수동 매도 추정 → DB 정리 후 안전 종료.
        3. 정상 범위 잔고: min(actual, db) 수량으로 매도 (수수료·소수점 오차 자동 보정).
        """
        pos = self._position
        if pos is None:
            return

        # ════════════════════════════════════════════════════════════
        # 🎮 모의투자 분기 — 실제 API 호출 없이 가상 체결 처리
        # ════════════════════════════════════════════════════════════
        if self.is_paper_trading:
            # 현재 웹소켓 가격을 가상 매도 체결가로 사용
            sell_price = current_price
            sell_amount = pos.amount_coin
            proceeds = sell_price * sell_amount                    # 매도 총 수령액 (KRW)
            realized_pnl = (sell_price - pos.buy_price) * sell_amount  # 순수익 (KRW)

            # 가상 잔고 복원 (체결 대금 전액 합산)
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(User).where(User.user_id == self.user_id))
                user = result.scalar_one_or_none()
                if user is not None:
                    user.virtual_krw = float(user.virtual_krw) + proceeds
                    await db.commit()
                    logger.info(
                        "[모의투자] 가상 잔고 복원: user=%s proceeds=%.0f balance=%.0f",
                        self.user_id, proceeds, user.virtual_krw,
                    )

            # 거래 이력 INSERT
            async with AsyncSessionLocal() as db:
                history = TradeHistory(
                    user_id=self.user_id,
                    symbol=self.symbol,
                    buy_price=pos.buy_price,
                    sell_price=sell_price,
                    profit_pct=profit_pct,
                    profit_krw=realized_pnl,
                    buy_amount_krw=pos.buy_amount_krw,
                    is_paper_trading=True,
                )
                db.add(history)
                await db.commit()
                logger.info(
                    "[모의투자] 거래 이력 저장: user=%s symbol=%s profit_pct=%.2f%% profit_krw=%.0f",
                    self.user_id, self.symbol, profit_pct, realized_pnl,
                )

            await self._clear_position_from_db()
            await self.notify(
                self.user_id,
                f"{'🟢' if realized_pnl >= 0 else '🔴'} **[🎮 모의투자] 매도 체결** "
                f"`{self.symbol}` — {reason}\n"
                f"매수가: {pos.buy_price:,.0f} KRW  →  매도가: {sell_price:,.0f} KRW\n"
                f"수익률: **{profit_pct:+.2f}%** | 손익: {realized_pnl:+,.0f} KRW",
            )
            self._position = None
            self.stop()
            return
        # ════════════════════════════════════════════════════════════

        # ── 1. 실제 가용 잔고 조회 ─────────────────────────────────────
        try:
            actual_balance = await self.exchange.fetch_coin_balance(self.symbol)
        except Exception as exc:
            # 일시적 네트워크 오류 — 이번 사이클 스킵, 다음 폴링 주기에서 재시도
            logger.warning(
                "잔고 조회 실패 (매도 스킵, 다음 주기 재시도): setting_id=%s err=%s",
                self.setting_id, exc,
            )
            return

        # ── 2. 수량 보정 ───────────────────────────────────────────────
        # 봇이 매수한 수량 이상은 매도하지 않아 의도치 않은 추가 코인 매도를 방지한다.
        sell_amount = min(actual_balance, pos.amount_coin)

        if actual_balance < pos.amount_coin:
            logger.info(
                "매도 수량 보정(오차 %.6f): setting_id=%s DB=%.6f → 실제=%.6f",
                pos.amount_coin - actual_balance,
                self.setting_id, pos.amount_coin, actual_balance,
            )

        # ── 3. 최소 주문 미만 → 수동 매도 추정, DB 정리 후 안전 종료 ──
        if sell_amount * current_price < MIN_SELL_ORDER_KRW:
            logger.warning(
                "실제 잔고 부족으로 매도 취소 (수동 매도 추정): "
                "setting_id=%s actual=%.6f value≈%.0f KRW",
                self.setting_id, actual_balance, actual_balance * current_price,
            )
            await self.notify(
                self.user_id,
                f"⚠️ **감시 종료** `{self.symbol}`\n"
                f"실제 코인 잔고(`{actual_balance:.6f}`)가 최소 주문 금액"
                f"({MIN_SELL_ORDER_KRW:,.0f} KRW) 미만입니다.\n"
                f"수동으로 이미 매도되었거나 다른 주문에 묶여 있을 수 있습니다.\n"
                f"DB 포지션만 초기화하고 감시를 종료합니다.",
            )
            await self._clear_position_from_db()
            self._position = None
            self.stop()
            return

        # ── 4. 시장가 매도 실행 ────────────────────────────────────────
        order = await self.exchange.create_market_sell_order(self.symbol, sell_amount)
        # order["average"] 키가 존재하더라도 Upbit ccxt는 즉시 응답에서 None을 반환할 수 있음
        # → or 연산으로 None 폴백 처리
        sell_price = float(order.get("average") or current_price)
        realized_pnl = (sell_price - pos.buy_price) * sell_amount
        actual_profit_pct = (sell_price - pos.buy_price) / pos.buy_price * 100

        # ── DB 초기화 (is_running=False, buy_price/amount_coin=NULL) ──
        await self._clear_position_from_db()

        # ── 실전 거래 이력 INSERT (/ai통계 실전 성과 집계에 활용) ──────
        async with AsyncSessionLocal() as db:
            history = TradeHistory(
                user_id=self.user_id,
                symbol=self.symbol,
                buy_price=pos.buy_price,
                sell_price=sell_price,
                profit_pct=actual_profit_pct,
                profit_krw=realized_pnl,
                buy_amount_krw=pos.buy_amount_krw,
                is_paper_trading=False,
            )
            db.add(history)
            await db.commit()
            logger.info(
                "[실거래] 거래 이력 저장: user=%s symbol=%s profit_pct=%.2f%% profit_krw=%.0f",
                self.user_id, self.symbol, actual_profit_pct, realized_pnl,
            )

        await self.notify(
            self.user_id,
            f"{'🟢' if realized_pnl >= 0 else '🔴'} **매도 체결** `{self.symbol}` — {reason}\n"
            f"매수가: {pos.buy_price:,.0f} KRW  →  매도가: {sell_price:,.0f} KRW\n"
            f"수익률: **{actual_profit_pct:+.2f}%** | 손익: {realized_pnl:+,.0f} KRW",
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

    async def stop_paper_for_user(self, user_id: str) -> None:
        """특정 유저의 모의투자(is_paper_trading=True) 워커 태스크만 취소한다.

        DB 조작(BotSetting·TradeHistory 삭제, virtual_krw 초기화)은
        호출 측(/ai모의초기화 커맨드)에서 별도로 처리하므로,
        이 메서드는 인메모리 태스크 취소와 레지스트리 정리만 담당한다.

        Args:
            user_id: Discord 사용자 ID.
        """
        to_stop = [
            w for w in self._workers.values()
            if w.user_id == user_id and w.is_paper_trading
        ]
        for w in to_stop:
            w.stop()  # asyncio 태스크 취소만 (DB 건드리지 않음)
            self._workers.pop(w.setting_id, None)
        if to_stop:
            logger.info(
                "모의투자 워커 %d개 취소 완료: user_id=%s", len(to_stop), user_id
            )
            await UpbitWebsocketManager.get().subscribe(self.active_symbols())
