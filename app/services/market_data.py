"""
MarketDataManager: AI 자동 매매를 위한 시장 데이터 캐싱 관리자.

동작 흐름 (4시간 주기 갱신):
  [1차 스크리닝]
    ccxt fetch_tickers() 로 업비트 전체 KRW 마켓의 24h 거래대금(quoteVolume)을 조회.
    내림차순 정렬 후 상위 TOP_N(10)개 심볼만 추출.

  [2차 정밀 캐싱]
    Top N 코인별로 4h 봉 OHLCV fetch_ohlcv() 실행 (순차 처리, 코인 간 0.5초 간격).
    pandas 로 RSI(14) · MA20 계산 후 del df 로 메모리 즉시 해제 (OOM 방지).
    결과를 self._cache[symbol] 딕셔너리에 저장.

캐시 구조:
    {
      "BTC/KRW": {
        "price":       float,        # 4h 마지막 봉 종가
        "change_pct":  float | None, # 24h 변동률 (%)
        "volume_krw":  float | None, # 24h 거래대금 (KRW)
        "rsi14":       float | None, # RSI(14)
        "ma20":        float | None, # 20봉 이동평균
        "candles":     list[dict],   # 최근 5개 4h 봉 요약
        "updated_at":  datetime,
      }, ...
    }

싱글턴 패턴으로 애플리케이션 전체에서 하나의 인스턴스만 운용.
AI 워커는 get_summary(symbol) / get_top_symbols() 로 즉시 조회 가능.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from functools import partial

import ccxt
import pandas as pd

logger = logging.getLogger(__name__)

TOP_N = 10               # 1차 스크리닝 선택 코인 수
REFRESH_INTERVAL = 4 * 3600  # 2차 캐싱 갱신 주기 (초)
OHLCV_LIMIT = 60         # 4h 봉 캔들 수 (RSI14 + MA20 계산에 충분)
COIN_SLEEP = 0.5         # 코인 간 Rate-Limit 방지 대기 (초)


def _calc_rsi(close: pd.Series, period: int = 14) -> float | None:
    """RSI(Relative Strength Index)를 계산해 마지막 값을 반환한다.

    Args:
        close: 종가 Series.
        period: RSI 기간 (기본 14).

    Returns:
        마지막 봉의 RSI 값, 또는 데이터 부족 시 None.
    """
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    rsi_series = 100 - (100 / (1 + rs))
    val = rsi_series.iloc[-1]
    return float(val) if pd.notna(val) else None


def _calc_ma(close: pd.Series, period: int = 20) -> float | None:
    """단순 이동평균(MA)을 계산해 마지막 값을 반환한다.

    Args:
        close: 종가 Series.
        period: MA 기간 (기본 20).

    Returns:
        마지막 봉의 MA 값, 또는 데이터 부족 시 None.
    """
    if len(close) < period:
        return None
    ma_series = close.rolling(period).mean()
    val = ma_series.iloc[-1]
    return float(val) if pd.notna(val) else None


class MarketDataManager:
    """업비트 KRW 마켓 시장 데이터를 주기적으로 스크리닝·캐싱하는 싱글턴.

    Attributes:
        _cache: 심볼 → 지표 요약 딕셔너리.
        _top_symbols: 최신 Top N 심볼 리스트.
        _task: 백그라운드 갱신 asyncio.Task.
        _exchange: 공용(public) ccxt upbit 인스턴스.
    """

    _instance: MarketDataManager | None = None

    def __init__(self) -> None:
        # API 키 없이 공용 마켓 데이터만 사용하므로 인증 불필요
        self._exchange = ccxt.upbit({"enableRateLimit": True})
        self._cache: dict[str, dict] = {}
        self._top_symbols: list[str] = []
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 싱글턴 접근
    # ------------------------------------------------------------------

    @classmethod
    def get(cls) -> "MarketDataManager":
        """싱글턴 인스턴스를 반환한다."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # 라이프사이클
    # ------------------------------------------------------------------

    def start(self) -> None:
        """백그라운드 갱신 루프를 시작한다. 이미 실행 중이면 무시."""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run_loop(), name="market-data-manager"
        )
        logger.info("MarketDataManager 백그라운드 루프 시작")

    def stop(self) -> None:
        """백그라운드 루프를 정상 종료한다."""
        if self._task:
            self._task.cancel()
        logger.info("MarketDataManager 중지")

    # ------------------------------------------------------------------
    # 캐시 조회 API (AI 워커가 호출)
    # ------------------------------------------------------------------

    def get_summary(self, symbol: str) -> dict | None:
        """심볼에 대한 최신 지표 요약을 반환한다.

        Args:
            symbol: CCXT 표준 심볼 (예: "BTC/KRW").

        Returns:
            price·rsi14·ma20·candles 등을 담은 딕셔너리, 또는 캐시 미존재 시 None.
        """
        return self._cache.get(symbol)

    def get_top_symbols(self) -> list[str]:
        """최신 Top N 심볼 리스트를 반환한다."""
        return list(self._top_symbols)

    def get_all(self) -> dict[str, dict]:
        """전체 캐시 스냅샷을 반환한다."""
        return dict(self._cache)

    # ------------------------------------------------------------------
    # 백그라운드 루프
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """4시간마다 _refresh()를 호출하는 무한 루프.

        기동 직후 첫 갱신을 즉시 실행하고, 이후 REFRESH_INTERVAL 간격으로 반복한다.
        """
        while True:
            try:
                await self._refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("MarketDataManager 갱신 실패: %s", exc)

            try:
                await asyncio.sleep(REFRESH_INTERVAL)
            except asyncio.CancelledError:
                raise

    # ------------------------------------------------------------------
    # 1차 스크리닝 + 2차 정밀 캐싱
    # ------------------------------------------------------------------

    async def _refresh(self) -> None:
        """전체 마켓 스크리닝 → Top N 정밀 캐싱을 순차 실행한다.

        비즈니스 로직:
          1. ccxt fetch_tickers() 로 전체 KRW 마켓 24h 거래대금 수집
          2. quoteVolume 내림차순 정렬 → 상위 TOP_N 심볼 추출
          3. 각 심볼별 4h OHLCV → RSI(14) · MA20 계산 → del df
          4. 결과를 self._cache 에 반영 (원자적 교체)
        """
        loop = asyncio.get_event_loop()

        # ── 1차 스크리닝: 전체 KRW 마켓 거래대금 조회 ──────────────────
        logger.info("MarketDataManager: 1차 스크리닝 시작 (fetch_tickers)")
        tickers: dict = await loop.run_in_executor(
            None, self._exchange.fetch_tickers
        )

        krw_tickers = [
            {
                "symbol": symbol,
                "quoteVolume": info.get("quoteVolume") or 0.0,
                "percentage": info.get("percentage"),       # 24h 변동률 (%)
            }
            for symbol, info in tickers.items()
            if symbol.endswith("/KRW") and (info.get("quoteVolume") or 0) > 0
        ]
        krw_tickers.sort(key=lambda x: x["quoteVolume"], reverse=True)
        top_symbols = [t["symbol"] for t in krw_tickers[:TOP_N]]

        logger.info(
            "1차 스크리닝 완료: KRW 마켓 %d 개 → Top %d: %s",
            len(krw_tickers), TOP_N, top_symbols,
        )

        # ticker 정보 맵 (volume/change 재활용)
        ticker_map = {t["symbol"]: t for t in krw_tickers if t["symbol"] in top_symbols}

        # ── 2차 정밀 캐싱: Top N 코인별 OHLCV + 지표 계산 ─────────────
        new_cache: dict[str, dict] = {}

        for symbol in top_symbols:
            try:
                # 4h 봉 OHLCV 조회 (run_in_executor로 동기 ccxt 래핑)
                fetch_fn = partial(
                    self._exchange.fetch_ohlcv,
                    symbol, "4h", None, OHLCV_LIMIT,
                )
                ohlcv: list[list] = await loop.run_in_executor(None, fetch_fn)

                if not ohlcv:
                    logger.warning("OHLCV 데이터 없음: %s", symbol)
                    continue

                # pandas 로 지표 계산 (동기 연산이지만 데이터 소량 → 인라인 처리)
                df = pd.DataFrame(
                    ohlcv,
                    columns=["timestamp", "open", "high", "low", "close", "volume"],
                )
                close = df["close"]

                rsi14 = _calc_rsi(close, 14)
                ma20 = _calc_ma(close, 20)

                # 최근 5개 봉 요약 (AI 컨텍스트 전달용)
                recent_candles = [
                    {
                        "time": int(row.timestamp),
                        "open": float(row.open),
                        "high": float(row.high),
                        "low": float(row.low),
                        "close": float(row.close),
                        "volume": float(row.volume),
                    }
                    for row in df.tail(5).itertuples(index=False)
                ]

                # ── 명시적 메모리 해제 (OOM 방지) ──────────────────────
                del df

                ticker_info = ticker_map.get(symbol, {})
                new_cache[symbol] = {
                    "price": float(ohlcv[-1][4]),              # 마지막 봉 종가
                    "change_pct": ticker_info.get("percentage"),
                    "volume_krw": ticker_info.get("quoteVolume"),
                    "rsi14": rsi14,
                    "ma20": ma20,
                    "candles": recent_candles,
                    "updated_at": datetime.now(timezone.utc),
                }

                logger.info(
                    "캐싱 완료: %-10s  가격=%,.0f  RSI=%.1f  MA20=%.0f",
                    symbol,
                    new_cache[symbol]["price"],
                    rsi14 if rsi14 is not None else float("nan"),
                    ma20 if ma20 is not None else float("nan"),
                )

            except Exception as exc:
                logger.error("코인 데이터 캐싱 실패: symbol=%s err=%s", symbol, exc)

            # ── 코인 간 Rate-Limit 방지 대기 ─────────────────────────
            await asyncio.sleep(COIN_SLEEP)

        # 전체 완료 후 캐시를 원자적으로 교체
        self._cache = new_cache
        self._top_symbols = top_symbols

        logger.info(
            "MarketDataManager 캐시 갱신 완료: %d 개 코인 (Top %d 중 성공)",
            len(new_cache), TOP_N,
        )
