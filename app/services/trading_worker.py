"""
TradingWorker: 사용자별 백그라운드 매매 루프.
- 매 0.5초마다 현재가 폴링
- 익절/손절 조건 도달 시 시장가 매도 실행
- Discord 채널로 체결 알림 전송
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from app.services.exchange import ExchangeService

logger = logging.getLogger(__name__)

POLL_INTERVAL = 0.5  # 초


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
        if self.target_profit_pct is None:
            return None
        return self.buy_price * (1 + self.target_profit_pct / 100)

    @property
    def stop_price(self) -> float | None:
        if self.stop_loss_pct is None:
            return None
        return self.buy_price * (1 - self.stop_loss_pct / 100)


class TradingWorker:
    """사용자 한 명의 설정(BotSetting)에 대응하는 워커"""

    def __init__(
        self,
        setting_id: int,
        user_id: str,
        symbol: str,
        buy_amount_krw: float,
        target_profit_pct: float | None,
        stop_loss_pct: float | None,
        exchange: ExchangeService,
        notify_callback,  # async def callback(user_id, msg)
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

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name=f"worker-{self.setting_id}")
        logger.info("워커 시작: user=%s symbol=%s", self.user_id, self.symbol)

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        logger.info("워커 중지: user=%s symbol=%s", self.user_id, self.symbol)

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

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

    async def _buy(self) -> None:
        current_price = await self.exchange.fetch_current_price(self.symbol)
        order = await self.exchange.create_market_buy_order(self.symbol, self.buy_amount_krw)
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
        pos = self._position
        if pos is None:
            return

        current_price = await self.exchange.fetch_current_price(self.symbol)
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
    _instance: "WorkerRegistry | None" = None
    _workers: dict[int, TradingWorker] = {}

    @classmethod
    def get(cls) -> "WorkerRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(self, worker: TradingWorker) -> None:
        self._workers[worker.setting_id] = worker

    def unregister(self, setting_id: int) -> None:
        worker = self._workers.pop(setting_id, None)
        if worker:
            worker.stop()

    def get_worker(self, setting_id: int) -> TradingWorker | None:
        return self._workers.get(setting_id)

    def stop_all_for_user(self, user_id: str) -> None:
        to_stop = [w for w in self._workers.values() if w.user_id == user_id]
        for w in to_stop:
            w.stop()
            del self._workers[w.setting_id]
