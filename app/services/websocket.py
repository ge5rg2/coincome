"""
UpbitWebsocketManager: 업비트 실시간 시세 중앙 관리자.

설계 원칙:
- 싱글턴 패턴으로 애플리케이션 전체에서 하나의 WebSocket 연결만 유지.
- wss://api.upbit.com/websocket/v1 에 연결해 구독 심볼의 ticker를 수신.
- 수신된 현재가를 current_prices(dict) 메모리에 최신화.
- 워커가 추가/제거될 때 subscribe()를 호출하면 구독 목록을 동적으로 갱신.
- 연결 끊김 시 자동 재연결(RECONNECT_DELAY 간격).
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid

import websockets
import websockets.exceptions
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.bot_setting import BotSetting

logger = logging.getLogger(__name__)

UPBIT_WS_URL = "wss://api.upbit.com/websocket/v1"
RECONNECT_DELAY = 5       # 재연결 대기 (초)
RECV_TIMEOUT = 0.2        # recv() 최대 대기 — 구독 이벤트 폴링 주기


# ------------------------------------------------------------------
# 심볼 형식 변환 헬퍼
# ------------------------------------------------------------------

def ccxt_to_upbit(symbol: str) -> str:
    """ccxt 형식 → 업비트 형식.  예: 'BTC/KRW' → 'KRW-BTC'"""
    base, quote = symbol.split("/")
    return f"{quote}-{base}"


def upbit_to_ccxt(code: str) -> str:
    """업비트 형식 → ccxt 형식.  예: 'KRW-BTC' → 'BTC/KRW'"""
    quote, base = code.split("-")
    return f"{base}/{quote}"


# ------------------------------------------------------------------
# 싱글턴 WebSocket 매니저
# ------------------------------------------------------------------

class UpbitWebsocketManager:
    """업비트 실시간 시세를 중앙 집중적으로 수신·관리하는 싱글턴 클래스."""

    _instance: "UpbitWebsocketManager | None" = None

    def __init__(self) -> None:
        # 심볼별 최신 현재가 캐시 (key: 'BTC/KRW' ccxt 형식)
        self.current_prices: dict[str, float] = {}
        # 현재 구독 중인 심볼 집합 (ccxt 형식)
        self._subscribed_symbols: set[str] = set()
        # 구독 목록 변경 시 WebSocket 재전송 신호
        self._subscribe_event: asyncio.Event = asyncio.Event()
        self._task: asyncio.Task | None = None

    @classmethod
    def get(cls) -> "UpbitWebsocketManager":
        """싱글턴 인스턴스 반환"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # 라이프사이클
    # ------------------------------------------------------------------

    def start(self) -> None:
        """백그라운드 WebSocket 태스크를 시작한다. 이미 실행 중이면 무시."""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="upbit-ws-manager")
        logger.info("UpbitWebsocketManager 시작됨")

    def stop(self) -> None:
        """백그라운드 태스크를 취소한다."""
        if self._task:
            self._task.cancel()
        logger.info("UpbitWebsocketManager 종료 요청")

    # ------------------------------------------------------------------
    # 구독 관리 (외부 인터페이스)
    # ------------------------------------------------------------------

    async def subscribe(self, symbols: set[str]) -> None:
        """
        구독 심볼 전체를 교체한다. 변경이 없으면 아무것도 하지 않는다.

        Args:
            symbols: 구독할 심볼 집합 (ccxt 형식, 예: {'BTC/KRW', 'ETH/KRW'})
        """
        if symbols == self._subscribed_symbols:
            return
        self._subscribed_symbols = set(symbols)
        self._subscribe_event.set()
        logger.info("WebSocket 구독 목록 변경 → %s", self._subscribed_symbols)

    async def add_symbol(self, symbol: str) -> None:
        """단일 심볼을 구독에 추가한다."""
        await self.subscribe(self._subscribed_symbols | {symbol})

    async def remove_symbol(self, symbol: str) -> None:
        """단일 심볼을 구독에서 제거한다."""
        await self.subscribe(self._subscribed_symbols - {symbol})

    def get_price(self, symbol: str) -> float | None:
        """
        메모리 캐시에서 현재가를 반환한다.

        Returns:
            현재가(float) 또는 아직 수신 전이면 None
        """
        return self.current_prices.get(symbol)

    # ------------------------------------------------------------------
    # 내부 로직
    # ------------------------------------------------------------------

    async def _load_active_symbols_from_db(self) -> set[str]:
        """앱 시작 시 DB의 is_running=True 심볼을 초기 구독 목록으로 로드한다."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(BotSetting.symbol).where(BotSetting.is_running.is_(True))
            )
            return set(result.scalars().all())

    def _build_subscribe_message(self) -> str:
        """업비트 WebSocket 구독 메시지를 생성한다.

        형식: [{"ticket":"..."}, {"type":"ticker","codes":["KRW-BTC",...]}]
        """
        codes = [ccxt_to_upbit(s) for s in self._subscribed_symbols]
        return json.dumps([
            {"ticket": str(uuid.uuid4())},
            {"type": "ticker", "codes": codes, "isOnlyRealtime": True},
        ])

    async def _run(self) -> None:
        """자동 재연결 루프. 연결 끊김 시 RECONNECT_DELAY 후 재시도."""
        # 앱 시작 시 DB에서 기존 실행 중인 심볼 복원
        self._subscribed_symbols = await self._load_active_symbols_from_db()
        if self._subscribed_symbols:
            logger.info("DB 복원된 구독 심볼: %s", self._subscribed_symbols)

        while True:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                logger.info("UpbitWebsocketManager 태스크 취소됨")
                break
            except Exception as exc:
                logger.warning(
                    "WebSocket 연결 오류: %s — %d초 후 재연결", exc, RECONNECT_DELAY
                )
                await asyncio.sleep(RECONNECT_DELAY)

    async def _connect_and_listen(self) -> None:
        """
        업비트 WebSocket에 연결하고 ticker 메시지를 수신한다.

        - 연결 직후 현재 구독 목록이 있으면 즉시 구독 전송.
        - 수신 루프에서 0.2초마다 구독 변경 이벤트를 확인하여 동적 재구독.
        - ConnectionClosed 예외는 상위 _run()으로 전파되어 재연결 처리.
        """
        async with websockets.connect(
            UPBIT_WS_URL,
            ping_interval=60,   # 60초마다 ping — 서버 keepalive
            ping_timeout=30,
        ) as ws:
            logger.info("업비트 WebSocket 연결 완료")

            # 초기 구독 전송
            if self._subscribed_symbols:
                await ws.send(self._build_subscribe_message())
                logger.info("초기 구독 전송: %s", self._subscribed_symbols)

            while True:
                # ── 구독 변경 이벤트 확인 ──────────────────────────────
                if self._subscribe_event.is_set():
                    self._subscribe_event.clear()
                    if self._subscribed_symbols:
                        await ws.send(self._build_subscribe_message())
                        logger.info("구독 갱신 전송: %s", self._subscribed_symbols)
                    else:
                        logger.info("구독 심볼 없음 — 수신 대기 중")

                # ── 메시지 수신 (짧은 타임아웃으로 이벤트 폴링 허용) ──
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
                except asyncio.TimeoutError:
                    # 정상 타임아웃 — 루프 상단으로 돌아가 이벤트 재확인
                    continue
                except websockets.exceptions.ConnectionClosed as exc:
                    logger.warning("WebSocket 연결 종료: %s", exc)
                    raise  # _run()에서 재연결 처리

                # ── 수신 데이터 파싱 ──────────────────────────────────
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("WebSocket 메시지 파싱 실패: %s", raw)
                    continue

                if data.get("type") == "ticker":
                    code: str = data["code"]             # 'KRW-BTC'
                    price: float = float(data["trade_price"])
                    symbol = upbit_to_ccxt(code)         # 'BTC/KRW'
                    self.current_prices[symbol] = price
