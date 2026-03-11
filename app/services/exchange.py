"""
ExchangeService: ccxt를 통한 업비트 API 추상화 레이어.
사용자별 API 키를 런타임에 주입할 수 있도록 인스턴스 단위로 설계.
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
    # 시세 조회
    # ------------------------------------------------------------------

    async def fetch_ticker(self, symbol: str) -> dict:
        """현재가 및 거래 정보 반환. symbol 예: 'BTC/KRW'"""
        loop = asyncio.get_event_loop()
        ticker = await loop.run_in_executor(None, partial(self._exchange.fetch_ticker, symbol))
        return ticker

    async def fetch_current_price(self, symbol: str) -> float:
        """현재가(last)만 반환"""
        ticker = await self.fetch_ticker(symbol)
        return float(ticker["last"])

    async def fetch_ohlcv(self, symbol: str, timeframe: str = "1m", limit: int = 100) -> list:
        """OHLCV 캔들 데이터 반환"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, partial(self._exchange.fetch_ohlcv, symbol, timeframe, limit=limit)
        )

    # ------------------------------------------------------------------
    # 잔고 조회
    # ------------------------------------------------------------------

    async def fetch_balance(self) -> dict:
        """전체 잔고 반환"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._exchange.fetch_balance)

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
        업비트는 매수 시 KRW 금액을 quoteOrderQty로 전달.
        ccxt upbit는 create_order(symbol, 'market', 'buy', amount, price=None, params={'cost': krw})
        """
        loop = asyncio.get_event_loop()
        order = await loop.run_in_executor(
            None,
            partial(
                self._exchange.create_order,
                symbol,
                "market",
                "buy",
                amount_krw,         # upbit: 매수 시 KRW 금액
                None,
                {"createMarketBuyOrderRequiresPrice": False},
            ),
        )
        logger.info("매수 주문 완료: %s %.0f KRW → %s", symbol, amount_krw, order.get("id"))
        return order

    async def create_market_sell_order(self, symbol: str, amount_coin: float) -> dict:
        """시장가 매도. amount_coin: 매도할 코인 수량"""
        loop = asyncio.get_event_loop()
        order = await loop.run_in_executor(
            None,
            partial(self._exchange.create_market_sell_order, symbol, amount_coin),
        )
        logger.info("매도 주문 완료: %s %.8f → %s", symbol, amount_coin, order.get("id"))
        return order

    # ------------------------------------------------------------------
    # 주문 조회
    # ------------------------------------------------------------------

    async def fetch_order(self, order_id: str, symbol: str) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, partial(self._exchange.fetch_order, order_id, symbol)
        )

    async def fetch_open_orders(self, symbol: str) -> list:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, partial(self._exchange.fetch_open_orders, symbol)
        )
