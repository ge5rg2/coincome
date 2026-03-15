"""
MarketDataManager: AI 자동 매매를 위한 시장 데이터 캐싱 관리자.

동작 흐름 (1시간 주기 갱신):
  [1차 스크리닝]
    fetch_markets() 로 전체 마켓 목록을 조회한 뒤 /KRW 심볼만 추출.
    URL 길이 제한 방어를 위해 심볼 리스트를 CHUNK_SIZE(100)개씩 분할.
    청크마다 fetch_tickers(chunk) 를 호출해 결과를 하나의 딕셔너리로 병합.
    24h 거래대금(quoteVolume) 내림차순 정렬 후 상위 TOP_N(10)개 심볼만 추출.

  [2차 정밀 캐싱]
    Top N 코인별로 4h 봉과 1h 봉 OHLCV를 순차 fetch (코인 간 0.8초 간격).
    pandas 로 각 타임프레임의 RSI(14) · MA20 계산 후 del df 로 메모리 즉시 해제.
    결과를 self._cache[symbol] 딕셔너리에 저장.

캐시 구조:
    {
      "BTC/KRW": {
        "price":        float,        # 현재가 (4h 마지막 봉 종가)
        "change_pct":   float | None, # 24h 변동률 (%)
        "volume_krw":   float | None, # 24h 거래대금 (KRW)
        # ── Swing (4h 봉) 지표 ─────────────────────────────────────
        "rsi14":        float | None, # 4h RSI(14)
        "ma20":         float | None, # 4h 20봉 이동평균
        "candles":      list[dict],   # 최근 5개 4h 봉 요약
        # ── Scalping (1h 봉) 지표 ──────────────────────────────────
        "rsi14_1h":     float | None, # 1h RSI(14)
        "ma20_1h":      float | None, # 1h 20봉 이동평균
        "candles_1h":   list[dict],   # 최근 5개 1h 봉 요약
        "updated_at":   datetime,
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

TOP_N          = 10    # 1차 스크리닝 선택 코인 수
CHUNK_SIZE     = 100   # fetch_tickers() 1회 호출 시 최대 심볼 수 (URL 길이 제한 방어)
CHUNK_SLEEP    = 0.3   # 청크 간 Rate-Limit 방지 대기 (초)
REFRESH_INTERVAL = 1 * 3600  # 캐시 갱신 주기 (초) — 1h 단타 모드를 위해 1시간으로 단축
OHLCV_LIMIT    = 60    # 4h 봉 캔들 수 (RSI14 + MA20 계산에 충분)
OHLCV_LIMIT_1H = 60    # 1h 봉 캔들 수
COIN_SLEEP     = 0.8   # 코인별 4h/1h 두 번 fetch 후 다음 코인으로 넘어가기 전 대기 (초)
TF_SLEEP       = 0.3   # 동일 코인 내 4h → 1h 전환 시 Rate-Limit 방지 대기 (초)


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
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss
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


def _ohlcv_to_candles(ohlcv: list[list], tail: int = 5) -> list[dict]:
    """OHLCV 리스트의 마지막 N개 봉을 딕셔너리 리스트로 변환한다.

    Args:
        ohlcv: ccxt fetch_ohlcv() 반환값.
        tail:  반환할 봉 개수.

    Returns:
        {time, open, high, low, close, volume} 딕셔너리 리스트.
    """
    return [
        {
            "time":   int(row[0]),
            "open":   float(row[1]),
            "high":   float(row[2]),
            "low":    float(row[3]),
            "close":  float(row[4]),
            "volume": float(row[5]),
        }
        for row in ohlcv[-tail:]
    ]


class MarketDataManager:
    """업비트 KRW 마켓 시장 데이터를 주기적으로 스크리닝·캐싱하는 싱글턴.

    Attributes:
        _cache:       심볼 → 지표 요약 딕셔너리.
        _top_symbols: 최신 Top N 심볼 리스트.
        _task:        백그라운드 갱신 asyncio.Task.
        _exchange:    공용(public) ccxt upbit 인스턴스.
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
        logger.info("MarketDataManager 백그라운드 루프 시작 (갱신 주기: %d분)", REFRESH_INTERVAL // 60)

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
            4h·1h 지표를 모두 담은 딕셔너리, 또는 캐시 미존재 시 None.
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
        """1시간마다 _refresh()를 호출하는 무한 루프.

        기동 직후 첫 갱신을 즉시 실행하고, 이후 REFRESH_INTERVAL 간격으로 반복한다.
        1시간 갱신 주기는 SCALPING(1h 봉) 모드의 지표 신선도를 유지하기 위함이다.
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
    # 1차 스크리닝 + 2차 정밀 캐싱 (4h + 1h)
    # ------------------------------------------------------------------

    async def _refresh(self) -> None:
        """전체 마켓 스크리닝 → Top N 정밀 캐싱(4h + 1h)을 순차 실행한다.

        비즈니스 로직:
          1. fetch_markets() 로 전체 마켓 목록 조회 → /KRW 심볼 필터링
          2. 심볼 리스트를 CHUNK_SIZE 개씩 분할해 fetch_tickers(chunk) 반복 호출 후 병합
          3. quoteVolume 내림차순 정렬 → 상위 TOP_N 심볼 추출
          4. 각 심볼별:
             a. 4h OHLCV → RSI14·MA20 계산 (Swing용)
             b. 0.3초 대기 → 1h OHLCV → RSI14·MA20 계산 (Scalping용)
             c. del df 로 메모리 즉시 해제
          5. 결과를 self._cache 에 반영 (원자적 교체)
        """
        loop = asyncio.get_event_loop()

        # ── 1차 스크리닝: 마켓 목록 조회 → KRW 필터 → 청크 분할 fetch_tickers ──
        logger.info("MarketDataManager: 1차 스크리닝 시작 (fetch_markets)")

        markets: list[dict] = await loop.run_in_executor(
            None, self._exchange.fetch_markets
        )

        krw_symbols: list[str] = [
            m["symbol"]
            for m in markets
            if isinstance(m, dict) and m.get("symbol", "").endswith("/KRW")
        ]
        logger.info("KRW 마켓 심볼 추출 완료: %d 개", len(krw_symbols))

        tickers: dict = {}
        total_chunks = (len(krw_symbols) + CHUNK_SIZE - 1) // CHUNK_SIZE

        for idx, start in enumerate(range(0, len(krw_symbols), CHUNK_SIZE)):
            chunk = krw_symbols[start: start + CHUNK_SIZE]
            fetch_fn = partial(self._exchange.fetch_tickers, chunk)
            chunk_tickers: dict = await loop.run_in_executor(None, fetch_fn)
            tickers.update(chunk_tickers)
            logger.debug(
                "fetch_tickers 청크 %d/%d 완료 (%d 개)",
                idx + 1, total_chunks, len(chunk),
            )
            await asyncio.sleep(CHUNK_SLEEP)

        krw_tickers = [
            {
                "symbol":      symbol,
                "quoteVolume": info.get("quoteVolume") or 0.0,
                "percentage":  info.get("percentage"),
                # ccxt upbit ticker 기준: last = 최근 체결가
                "last_price":  float(info.get("last") or info.get("close") or 0),
            }
            for symbol, info in tickers.items()
            if symbol.endswith("/KRW") and (info.get("quoteVolume") or 0) > 0
        ]

        # ── 엽전주 하드 필터: 현재가 100원 미만 코인은 AI 분석 대상에서 원천 차단 ──
        # 업비트 API 틱 데이터 기준으로 last_price(최근 체결가)가 100 KRW 미만이면
        # Top N 후보에서 완전 제외. 프롬프트 데이터 자체에 엽전주가 유입되지 않게 한다.
        before_filter = len(krw_tickers)
        krw_tickers = [t for t in krw_tickers if t["last_price"] >= 100]
        filtered_count = before_filter - len(krw_tickers)
        if filtered_count > 0:
            logger.info(
                "엽전주 필터 적용: %d개 코인 제외 (100원 미만) — 잔여 %d개",
                filtered_count, len(krw_tickers),
            )

        krw_tickers.sort(key=lambda x: x["quoteVolume"], reverse=True)
        top_symbols = [t["symbol"] for t in krw_tickers[:TOP_N]]

        logger.info(
            "1차 스크리닝 완료: KRW 마켓 %d 개 (엽전주 %d개 제외) → Top %d: %s",
            len(krw_tickers), filtered_count, TOP_N, top_symbols,
        )

        ticker_map = {t["symbol"]: t for t in krw_tickers if t["symbol"] in top_symbols}

        # ── 2차 정밀 캐싱: Top N 코인별 4h + 1h OHLCV + 지표 계산 ─────
        new_cache: dict[str, dict] = {}

        for symbol in top_symbols:
            entry: dict = {}
            try:
                ticker_info = ticker_map.get(symbol, {})

                # ── (A) 4h 봉 OHLCV (Swing 지표) ──────────────────────
                fetch_4h = partial(
                    self._exchange.fetch_ohlcv,
                    symbol, "4h", None, OHLCV_LIMIT,
                )
                ohlcv_4h: list[list] = await loop.run_in_executor(None, fetch_4h)

                if ohlcv_4h:
                    df4 = pd.DataFrame(
                        ohlcv_4h,
                        columns=["timestamp", "open", "high", "low", "close", "volume"],
                    )
                    close4  = df4["close"]
                    rsi14   = _calc_rsi(close4, 14)
                    ma20    = _calc_ma(close4, 20)
                    candles = _ohlcv_to_candles(ohlcv_4h)
                    price   = float(ohlcv_4h[-1][4])
                    del df4
                else:
                    logger.warning("4h OHLCV 데이터 없음: %s", symbol)
                    rsi14   = None
                    ma20    = None
                    candles = []
                    price   = 0.0

                entry.update({
                    "price":      price,
                    "change_pct": ticker_info.get("percentage"),
                    "volume_krw": ticker_info.get("quoteVolume"),
                    "rsi14":      rsi14,
                    "ma20":       ma20,
                    "candles":    candles,
                })

                # ── Rate-Limit 방지 대기 (4h → 1h 전환) ────────────────
                await asyncio.sleep(TF_SLEEP)

                # ── (B) 1h 봉 OHLCV (Scalping 지표) ───────────────────
                fetch_1h = partial(
                    self._exchange.fetch_ohlcv,
                    symbol, "1h", None, OHLCV_LIMIT_1H,
                )
                ohlcv_1h: list[list] = await loop.run_in_executor(None, fetch_1h)

                if ohlcv_1h:
                    df1 = pd.DataFrame(
                        ohlcv_1h,
                        columns=["timestamp", "open", "high", "low", "close", "volume"],
                    )
                    close1      = df1["close"]
                    rsi14_1h    = _calc_rsi(close1, 14)
                    ma20_1h     = _calc_ma(close1, 20)
                    candles_1h  = _ohlcv_to_candles(ohlcv_1h)
                    del df1
                else:
                    logger.warning("1h OHLCV 데이터 없음: %s", symbol)
                    rsi14_1h   = None
                    ma20_1h    = None
                    candles_1h = []

                entry.update({
                    "rsi14_1h":   rsi14_1h,
                    "ma20_1h":    ma20_1h,
                    "candles_1h": candles_1h,
                    "updated_at": datetime.now(timezone.utc),
                })

                new_cache[symbol] = entry

                logger.info(
                    "캐싱 완료: %-10s  가격=%s  "
                    "RSI4h=%.1f  MA4h=%.0f  |  RSI1h=%.1f  MA1h=%.0f",
                    symbol,
                    f"{price:,.0f}",
                    rsi14    if rsi14    is not None else float("nan"),
                    ma20     if ma20     is not None else float("nan"),
                    rsi14_1h if rsi14_1h is not None else float("nan"),
                    ma20_1h  if ma20_1h  is not None else float("nan"),
                )

            except Exception as exc:
                logger.error("코인 데이터 캐싱 실패: symbol=%s err=%s", symbol, exc)

            # ── 코인 간 Rate-Limit 방지 대기 ──────────────────────────
            await asyncio.sleep(COIN_SLEEP)

        # 전체 완료 후 캐시를 원자적으로 교체
        self._cache    = new_cache
        self._top_symbols = top_symbols

        logger.info(
            "MarketDataManager 캐시 갱신 완료: %d 개 코인 (4h+1h 지표 포함)",
            len(new_cache),
        )
