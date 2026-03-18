"""
ExchangeService: ccxt를 통한 업비트 REST API 추상화 레이어.

역할 범위 (WebSocket 리팩토링 이후):
  - 시세 조회: UpbitWebsocketManager가 담당 → 이 클래스에서 제거
  - 잔고 조회: fetch_balance, fetch_krw_balance, fetch_coin_balance
  - 주문 실행: create_market_buy_order, create_market_sell_order
  - 주문 조회: fetch_order, fetch_open_orders

사용자별 API 키를 런타임에 주입하므로 인스턴스 단위로 설계.
"""
from __future__ import annotations

import asyncio
import logging
from functools import partial

import ccxt

logger = logging.getLogger(__name__)


class ExchangeService:
    def __init__(self, access_key: str, secret_key: str) -> None:
        self._exchange = ccxt.upbit(
            {
                "apiKey": access_key,
                "secret": secret_key,
                "enableRateLimit": True,
            }
        )

    # ------------------------------------------------------------------
    # 잔고 조회
    # ------------------------------------------------------------------

    async def fetch_balance(self) -> dict:
        """전체 잔고 반환"""
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, self._exchange.fetch_balance),
            timeout=10.0,
        )

    async def fetch_krw_balance(self) -> float:
        """KRW 가용 잔고 반환"""
        balance = await self.fetch_balance()
        return float(balance.get("KRW", {}).get("free", 0))

    async def fetch_coin_balance(self, symbol: str) -> float:
        """특정 코인의 가용 잔고 반환. symbol='BTC/KRW' 이면 'BTC' 추출"""
        coin = symbol.split("/")[0]
        balance = await self.fetch_balance()
        return float(balance.get(coin, {}).get("free", 0))

    # ------------------------------------------------------------------
    # 주문 실행
    # ------------------------------------------------------------------

    async def create_market_buy_order(self, symbol: str, amount_krw: float) -> dict:
        """
        시장가 매수.

        업비트는 매수 시 KRW 금액을 amount에 전달.
        ccxt upbit: create_order(symbol, 'market', 'buy', krw_amount, ...)
        """
        loop = asyncio.get_running_loop()
        order = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                partial(
                    self._exchange.create_order,
                    symbol,
                    "market",
                    "buy",
                    amount_krw,
                    None,
                    {"createMarketBuyOrderRequiresPrice": False},
                ),
            ),
            timeout=10.0,
        )
        logger.info("매수 주문 완료: %s %.0f KRW → order_id=%s", symbol, amount_krw, order.get("id"))
        return order

    async def create_market_sell_order(self, symbol: str, amount_coin: float) -> dict:
        """시장가 매도. amount_coin: 매도할 코인 수량"""
        loop = asyncio.get_running_loop()
        order = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                partial(self._exchange.create_market_sell_order, symbol, amount_coin),
            ),
            timeout=10.0,
        )
        logger.info("매도 주문 완료: %s %.8f → order_id=%s", symbol, amount_coin, order.get("id"))
        return order

    # ------------------------------------------------------------------
    # 주문 조회
    # ------------------------------------------------------------------

    async def fetch_order(self, order_id: str, symbol: str) -> dict:
        """주문 ID로 단일 주문 조회"""
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(
                None, partial(self._exchange.fetch_order, order_id, symbol)
            ),
            timeout=10.0,
        )

    async def fetch_open_orders(self, symbol: str) -> list:
        """미체결 주문 목록 조회"""
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(
                None, partial(self._exchange.fetch_open_orders, symbol)
            ),
            timeout=10.0,
        )
