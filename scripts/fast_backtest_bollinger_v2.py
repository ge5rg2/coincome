"""
fast_backtest_bollinger_v2.py — 메이저 코인 볼린저 핑퐁 V2 — 3가지 개선 가설 동시 검증.

1차 백테스트(fast_backtest_bollinger.py) 결과 분석:
  승률 41.6% / EV 음수 → 하락 추세에서의 BB 하단 터치 = '떨어지는 칼날' 패턴
  원인: 추세 방향성 미고려 → 롱 편향 진입이 하락 추세에서 연속 손절

V2 검증 가설 (3가지 동시 실행):
  Case A │ 스캘핑화       │ 1h 봉 │ BB(20,2.0σ) 하단 & RSI<40                │ TP +2.0% / SL -1.5%
  Case B │ 장기 추세 필터 │ 4h 봉 │ BB(20,2.0σ) 하단 & RSI<40 & Close>EMA200 │ TP +3.0% / SL -3.0%
  Case C │ 극단 과매도    │ 4h 봉 │ BB(20,2.8σ) 하단 & RSI<40                │ TP +3.0% / SL -3.0%

데이터 처리:
  - 로컬 JSON 캐시(.cache/ohlcv/) 에서만 읽음 — 업비트 API 미호출
  - 1h / 4h 데이터는 프로세스 내 메모리 캐시로 타임프레임당 1회만 로드
  - Case B & C 는 동일 4h 캐시 딕셔너리 재사용 (중복 I/O 방지)
  - Case B 의 EMA200 워밍업(200봉) 을 고려해 4h 캐시 최소봉 기준을 220봉으로 설정

실행 예시:
  python scripts/fast_backtest_bollinger_v2.py
  python scripts/fast_backtest_bollinger_v2.py --symbol BTC_KRW
  python scripts/fast_backtest_bollinger_v2.py --csv
  python scripts/fast_backtest_bollinger_v2.py --no-detail   # 케이스별 상세 출력 생략
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ──────────────────────────────────────────────────────────────────────────────
# ★ 공통 파라미터
# ──────────────────────────────────────────────────────────────────────────────
RSI_PERIOD:         int   = 14     # RSI 계산 기간 (봉)
BB_WINDOW:          int   = 20     # 볼린저 밴드 SMA 기간 (봉)
EMA_LONG_PERIOD:    int   = 200    # 장기 추세 필터용 EMA (Case B)

SNIPER_WEIGHT_PCT:  float = 20.0       # 🛡️ SNIPER — 잔고의 20% 투입
BEAST_WEIGHT_PCT:   float = 70.0       # 🔥 BEAST  — 잔고의 70% 투입
INITIAL_BALANCE:    float = 1_000_000  # 초기 시드 (KRW)

MIN_TRADES_FOR_DETAIL: int = 3

# 테스트 대상: 메이저 8종 (볼린저 핑퐁 대상)
WHITELIST: list[str] = [
    "BTC_KRW",
    "ETH_KRW",
    "XRP_KRW",
    "SOL_KRW",
    "DOGE_KRW",
    "ADA_KRW",
    "SUI_KRW",
    "PEPE_KRW",
]

# 4h 캐시 최소 봉 수: EMA200 워밍업 200봉 + RSI 14봉 + 여유
_4H_MIN_CANDLES: int = EMA_LONG_PERIOD + RSI_PERIOD + 10
# 1h 캐시 최소 봉 수: BB20 워밍업 + RSI14 + 여유
_1H_MIN_CANDLES: int = BB_WINDOW + RSI_PERIOD + 5

# ──────────────────────────────────────────────────────────────────────────────
# 경로 설정
# ──────────────────────────────────────────────────────────────────────────────
_ROOT      = Path(__file__).parent.parent
CACHE_DIR  = _ROOT / ".cache" / "ohlcv"
RESULT_DIR = _ROOT / ".result"

# ──────────────────────────────────────────────────────────────────────────────
# 로깅
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ANSI 색상 코드
_G  = "\033[32m"   # 녹색 — WIN / 양수
_R  = "\033[31m"   # 빨간 — LOSS / 음수
_Y  = "\033[33m"   # 노란 — 경고
_C  = "\033[36m"   # 청록 — 헤더
_B  = "\033[1m"    # 굵게
_RS = "\033[0m"    # 리셋

# KST 타임존
try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except ImportError:
    from datetime import timedelta
    KST = timezone(timedelta(hours=9))  # type: ignore[assignment]

# pandas 임포트 (볼린저 밴드 rolling std — 없으면 순수 Python 폴백)
try:
    import pandas as pd
    _PANDAS_AVAILABLE = True
except ImportError:
    _PANDAS_AVAILABLE = False
    logger.warning(
        "pandas 미설치 → 순수 Python 폴백으로 볼린저 밴드 계산. "
        "정확한 rolling std를 원하면 `pip install pandas` 를 실행하세요."
    )


# ──────────────────────────────────────────────────────────────────────────────
# 가설(Case) 설정 데이터클래스
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class CaseConfig:
    """단일 백테스트 가설의 파라미터를 담는 불변 설정 객체.

    Attributes:
        name:        케이스 식별자 ("A" / "B" / "C").
        label:       터미널 출력용 레이블.
        timeframe:   OHLCV 캐시 타임프레임 ("1h" / "4h").
        bb_std:      볼린저 밴드 표준편차 배수 (σ).
        rsi_max:     진입 허용 RSI 상한 (이 이상이면 진입 금지).
        tp_pct:      익절률 (%).
        sl_pct:      손절률 (%).
        use_ema200:  True 이면 Close > EMA200 조건 추가 (Case B).
    """

    name:       str
    label:      str
    timeframe:  str
    bb_std:     float
    rsi_max:    float
    tp_pct:     float
    sl_pct:     float
    use_ema200: bool = False


@dataclass
class CaseResult:
    """단일 가설의 백테스트 집계 결과.

    Attributes:
        cfg:          해당 가설의 CaseConfig.
        trades:       entry_ts 오름차순 정렬된 전체 거래 내역.
        s_final:      SNIPER 비중(20%) 시뮬레이션 최종 잔고 (KRW).
        s_mdd:        SNIPER 최대 낙폭 (%).
        b_final:      BEAST 비중(70%) 시뮬레이션 최종 잔고 (KRW).
        b_mdd:        BEAST 최대 낙폭 (%).
    """

    cfg:     CaseConfig
    trades:  list[dict[str, Any]] = field(default_factory=list)
    s_final: float = INITIAL_BALANCE
    s_mdd:   float = 0.0
    b_final: float = INITIAL_BALANCE
    b_mdd:   float = 0.0

    # ── 집계 프로퍼티 ──────────────────────────────────────────────────────

    @property
    def total(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t["result"] == "WIN")

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t["result"] == "LOSS")

    @property
    def timeouts(self) -> int:
        return sum(1 for t in self.trades if t["result"] == "TIMEOUT")

    @property
    def win_rate(self) -> float:
        return (self.wins / self.total * 100) if self.total else 0.0

    @property
    def avg_pnl(self) -> float:
        return (sum(t["pnl_pct"] for t in self.trades) / self.total) if self.total else 0.0

    @property
    def expectancy(self) -> float:
        """기대값(EV) = (승률 × TP) − (패율 × SL)."""
        if not self.total:
            return 0.0
        loss_rate = self.losses / self.total * 100
        return (self.win_rate / 100) * self.cfg.tp_pct - (loss_rate / 100) * self.cfg.sl_pct

    @property
    def s_roi(self) -> float:
        return (self.s_final - INITIAL_BALANCE) / INITIAL_BALANCE * 100

    @property
    def b_roi(self) -> float:
        return (self.b_final - INITIAL_BALANCE) / INITIAL_BALANCE * 100


# ──────────────────────────────────────────────────────────────────────────────
# 3가지 가설 정의
# ──────────────────────────────────────────────────────────────────────────────

CASES: list[CaseConfig] = [
    CaseConfig(
        name="A",
        label="1h 스캘핑  (BB 2.0σ / TP2% SL1.5%)",
        timeframe="1h",
        bb_std=2.0,
        rsi_max=40.0,
        tp_pct=2.0,
        sl_pct=1.5,
        use_ema200=False,
    ),
    CaseConfig(
        name="B",
        label="4h 추세필터 (BB 2.0σ / EMA200 / TP3% SL3%)",
        timeframe="4h",
        bb_std=2.0,
        rsi_max=40.0,
        tp_pct=3.0,
        sl_pct=3.0,
        use_ema200=True,
    ),
    CaseConfig(
        name="C",
        label="4h 극단과매도 (BB 2.8σ / TP3% SL3%)",
        timeframe="4h",
        bb_std=2.8,
        rsi_max=40.0,
        tp_pct=3.0,
        sl_pct=3.0,
        use_ema200=False,
    ),
]


# ──────────────────────────────────────────────────────────────────────────────
# 지표 계산
# ──────────────────────────────────────────────────────────────────────────────

def calc_ema(closes: list[float], period: int) -> list[float | None]:
    """지수이동평균(EMA)을 계산한다.

    첫 period 봉의 단순평균(SMA)을 초기 시드로 사용하고
    이후 EMA 승수(2 / (period+1))를 적용해 지수 가중 이동평균을 적용한다.

    Args:
        closes: 종가 리스트 (오름차순, 과거→현재).
        period: EMA 기간 (봉 수).

    Returns:
        각 봉의 EMA값 리스트. 워밍업 구간(period-1 미만)은 None.
    """
    result: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return result

    # 초기 시드: 첫 period 봉의 SMA
    seed = sum(closes[:period]) / period
    result[period - 1] = seed

    multiplier = 2.0 / (period + 1)
    prev = seed
    for i in range(period, len(closes)):
        ema = closes[i] * multiplier + prev * (1.0 - multiplier)
        result[i] = ema
        prev = ema

    return result


def calc_bollinger_bands(
    closes: list[float],
    window: int = BB_WINDOW,
    num_std: float = 2.0,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """볼린저 밴드 (Upper / Mid / Lower)를 계산한다.

    pandas가 있으면 rolling(ddof=1) std를 사용하고,
    없으면 순수 Python으로 동일한 표본 표준편차(ddof=1)를 계산한다.

    Args:
        closes:  종가 리스트 (오름차순, 과거→현재).
        window:  SMA·표준편차 계산 기간 (봉). 기본 20.
        num_std: 표준편차 배수. 기본 2.0.

    Returns:
        (upper, mid, lower) — 각각 봉 수만큼의 float|None 리스트.
        워밍업 구간은 None.
    """
    n = len(closes)
    upper: list[float | None] = [None] * n
    mid:   list[float | None] = [None] * n
    lower: list[float | None] = [None] * n

    if _PANDAS_AVAILABLE:
        s    = pd.Series(closes)
        _mid = s.rolling(window).mean()
        _std = s.rolling(window).std(ddof=1)
        for i in range(n):
            mv, sv = _mid.iloc[i], _std.iloc[i]
            if pd.isna(mv) or pd.isna(sv):
                continue
            mid[i]   = float(mv)
            upper[i] = float(mv) + num_std * float(sv)
            lower[i] = float(mv) - num_std * float(sv)
    else:
        # 순수 Python 폴백 — 표본 표준편차 (ddof=1)
        for i in range(window - 1, n):
            w     = closes[i - window + 1 : i + 1]
            mean  = sum(w) / window
            var   = sum((v - mean) ** 2 for v in w) / (window - 1)
            std   = math.sqrt(var)
            mid[i]   = mean
            upper[i] = mean + num_std * std
            lower[i] = mean - num_std * std

    return upper, mid, lower


def calc_rsi(closes: list[float], period: int = RSI_PERIOD) -> list[float | None]:
    """Wilder's Smoothed RSI(상대강도지수)를 계산한다.

    초기 avg_gain / avg_loss는 단순 평균으로 시드하고
    이후 Wilder's 지수 가중 이동평균(EMA)을 적용한다.

    Args:
        closes: 종가 리스트 (오름차순, 과거→현재).
        period: RSI 기간 (봉 수).

    Returns:
        각 봉의 RSI값 리스트. 계산 불가 구간은 None.
    """
    result: list[float | None] = [None] * len(closes)
    if len(closes) < period + 1:
        return result

    init_gains: list[float] = []
    init_losses: list[float] = []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        init_gains.append(diff if diff > 0 else 0.0)
        init_losses.append(abs(diff) if diff < 0 else 0.0)

    avg_gain = sum(init_gains)  / period
    avg_loss = sum(init_losses) / period

    result[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

    for i in range(period + 1, len(closes)):
        diff     = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + (diff if diff > 0 else 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + (abs(diff) if diff < 0 else 0.0)) / period
        result[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

    return result


# ──────────────────────────────────────────────────────────────────────────────
# 데이터 로더 (in-memory 캐시로 타임프레임당 1회 I/O)
# ──────────────────────────────────────────────────────────────────────────────

# 프로세스 내 메모리 캐시: {"1h": {sym: ohlcv}, "4h": {sym: ohlcv}}
_OHLCV_CACHE: dict[str, dict[str, list[list]]] = {}


def _load_ohlcv_from_disk(
    timeframe: str,
    min_candles: int,
) -> dict[str, list[list]]:
    """CACHE_DIR 내 JSON 파일들 중 WHITELIST 심볼만 로드한다.

    업비트 API 미호출 — 로컬 JSON 파일 전용.

    Args:
        timeframe:   타임프레임 접미사 (예: "1h", "4h").
        min_candles: 최소 봉 수. 미달 심볼은 스킵.

    Returns:
        {심볼: [[ts, o, h, l, c, v], ...]} 딕셔너리.
    """
    if not CACHE_DIR.exists():
        logger.error("캐시 디렉터리 없음: %s", CACHE_DIR)
        logger.error("먼저 `python scripts/backtester.py` 를 실행해 캐시를 생성하세요.")
        sys.exit(1)

    files = sorted(CACHE_DIR.glob(f"*_{timeframe}.json"))
    if not files:
        logger.error("캐시 파일 없음 (패턴: *_%s.json)", timeframe)
        sys.exit(1)

    whitelist_upper = {s.upper() for s in WHITELIST}
    result: dict[str, list[list]] = {}

    for path in files:
        sym = path.stem[: -(len(timeframe) + 1)]   # "BTC_KRW_4h" → "BTC_KRW"
        if sym.upper() not in whitelist_upper:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list) or len(data) < min_candles:
                logger.warning(
                    "데이터 부족 스킵 (%s %s): %d봉 < 최소 %d봉",
                    sym, timeframe, len(data), min_candles,
                )
                continue
            result[sym] = data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("파일 로드 실패 (%s): %s", path.name, exc)

    if not result:
        logger.error(
            "로드된 심볼 없음 (timeframe=%s, min_candles=%d). "
            "캐시를 먼저 생성하세요.",
            timeframe, min_candles,
        )
        sys.exit(1)

    return result


def get_ohlcv(
    timeframe: str,
    symbol_filter: str | None = None,
) -> dict[str, list[list]]:
    """타임프레임별 OHLCV를 메모리 캐시에서 반환한다.

    동일 타임프레임은 첫 호출 시 JSON 디스크에서 로드하고
    이후 호출에서는 캐시를 그대로 반환해 중복 I/O를 방지한다.

    Args:
        timeframe:     "1h" 또는 "4h".
        symbol_filter: 특정 심볼만 추출 (예: "BTC_KRW"). None이면 전체 반환.

    Returns:
        {심볼: ohlcv} 딕셔너리.
    """
    if timeframe not in _OHLCV_CACHE:
        # 타임프레임별 최소봉 기준 적용 (EMA200 워밍업 고려)
        min_c = _4H_MIN_CANDLES if timeframe == "4h" else _1H_MIN_CANDLES
        logger.info(
            "[캐시 로드] %s 봉 데이터 로드 시작 (최소 %d봉, API 미호출)",
            timeframe, min_c,
        )
        _OHLCV_CACHE[timeframe] = _load_ohlcv_from_disk(timeframe, min_c)
        logger.info(
            "[캐시 로드] %s 봉 완료: %d개 심볼",
            timeframe, len(_OHLCV_CACHE[timeframe]),
        )
    else:
        logger.info("[캐시 HIT] %s 봉 데이터 재사용 (I/O 스킵)", timeframe)

    data = _OHLCV_CACHE[timeframe]
    if symbol_filter:
        return {k: v for k, v in data.items() if k.upper() == symbol_filter.upper()}
    return data


# ──────────────────────────────────────────────────────────────────────────────
# 단일 심볼 백테스트 (케이스 설정 주입)
# ──────────────────────────────────────────────────────────────────────────────

def backtest_symbol_case(
    sym: str,
    ohlcv: list[list],
    cfg: CaseConfig,
) -> list[dict[str, Any]]:
    """CaseConfig 기준으로 단일 심볼 볼린저 핑퐁 백테스트를 실행한다.

    [진입 조건]
      - 항상: Close < BB 하단 (Lower)  AND  RSI < cfg.rsi_max
      - Case B 한정: Close > EMA200 (상승 추세 내 눌림목만 공략)

    [청산 조건]
      - TP: 진입가 × (1 + tp_pct/100) 이상 고가 도달 → WIN
      - SL: 진입가 × (1 - sl_pct/100) 이하 저가 도달 → LOSS
      - 동일 봉 TP·SL 동시 충족: WIN 우선 (상방 이동 먼저 가정)

    [포지션 겹침 방지]
      현재 포지션 청산 전 신규 진입 신호 무시 (per-symbol).

    Args:
        sym:   심볼 식별자 (예: "BTC_KRW").
        ohlcv: [[ts_ms, open, high, low, close, volume], ...] (오름차순).
        cfg:   CaseConfig — 타임프레임·TP/SL·EMA200 사용 여부 등 포함.

    Returns:
        거래 내역 리스트.
    """
    closes = [float(c[4]) for c in ohlcv]
    highs  = [float(c[2]) for c in ohlcv]
    lows   = [float(c[3]) for c in ohlcv]
    ts_arr = [int(c[0])   for c in ohlcv]

    # ── 지표 계산 ─────────────────────────────────────────────────────────
    _, _, bb_lower = calc_bollinger_bands(closes, BB_WINDOW, cfg.bb_std)
    rsi_vals       = calc_rsi(closes, RSI_PERIOD)
    ema200_vals: list[float | None] = (
        calc_ema(closes, EMA_LONG_PERIOD) if cfg.use_ema200 else [None] * len(closes)
    )

    trades: list[dict[str, Any]] = []
    in_position  = False
    entry_price  = 0.0
    entry_ts_val = 0
    entry_idx    = 0

    for i in range(1, len(ohlcv)):

        # ── [보유 중] TP / SL 청산 조건 확인 ─────────────────────────────
        if in_position:
            h        = highs[i]
            l        = lows[i]
            tp_price = entry_price * (1 + cfg.tp_pct / 100)
            sl_price = entry_price * (1 - cfg.sl_pct / 100)

            # 우선순위 1: WIN — TP 도달
            if h >= tp_price:
                trades.append({
                    "symbol":       sym,
                    "entry_ts":     entry_ts_val,
                    "exit_ts":      ts_arr[i],
                    "entry_price":  entry_price,
                    "result":       "WIN",
                    "pnl_pct":      cfg.tp_pct,
                    "candles_held": i - entry_idx,
                })
                in_position = False
                continue

            # 우선순위 2: LOSS — SL 도달
            if l <= sl_price:
                trades.append({
                    "symbol":       sym,
                    "entry_ts":     entry_ts_val,
                    "exit_ts":      ts_arr[i],
                    "entry_price":  entry_price,
                    "result":       "LOSS",
                    "pnl_pct":      -cfg.sl_pct,
                    "candles_held": i - entry_idx,
                })
                in_position = False
                continue

            continue  # TP·SL 미도달 → 포지션 유지

        # ── [미보유] 진입 조건 확인 ──────────────────────────────────────
        lower_curr = bb_lower[i]
        rsi_curr   = rsi_vals[i]

        # 지표 미준비 구간 → 스킵
        if lower_curr is None or rsi_curr is None:
            continue

        # Case B: EMA200 워밍업 미완료 시 스킵
        if cfg.use_ema200:
            ema200_curr = ema200_vals[i]
            if ema200_curr is None:
                continue

        close_curr = closes[i]

        # 조건 A: Close < BB 하단 (과매도 하단 이탈·터치)
        below_lower: bool = close_curr < lower_curr

        # 조건 B: RSI < rsi_max (과매도 확인)
        rsi_ok: bool = rsi_curr < cfg.rsi_max

        # 조건 C (Case B 전용): Close > EMA200 (상승 추세 내 눌림목만 진입)
        trend_ok: bool = True
        if cfg.use_ema200:
            ema200_curr = ema200_vals[i]  # type: ignore[assignment]
            trend_ok = close_curr > ema200_curr  # type: ignore[operator]

        if below_lower and rsi_ok and trend_ok:
            in_position  = True
            entry_price  = close_curr
            entry_ts_val = ts_arr[i]
            entry_idx    = i

    # ── 데이터 소진 후 미청산 포지션 → TIMEOUT ────────────────────────────
    if in_position:
        last_close = closes[-1]
        pnl        = (last_close - entry_price) / entry_price * 100
        trades.append({
            "symbol":       sym,
            "entry_ts":     entry_ts_val,
            "exit_ts":      ts_arr[-1],
            "entry_price":  entry_price,
            "result":       "TIMEOUT",
            "pnl_pct":      round(pnl, 4),
            "candles_held": len(ohlcv) - 1 - entry_idx,
        })

    return trades


# ──────────────────────────────────────────────────────────────────────────────
# 잔고 시뮬레이션
# ──────────────────────────────────────────────────────────────────────────────

def simulate_balance(
    trades: list[dict],
    weight_pct: float,
    initial: float = INITIAL_BALANCE,
) -> tuple[float, float]:
    """거래 내역을 시간순으로 순회하며 최종 잔고와 MDD를 계산한다.

    매 거래마다 당시 잔고의 weight_pct%를 투입하고
    pnl_pct에 따라 잔고를 갱신한다. 잔고가 0 이하이면 파산 처리한다.

    Args:
        trades:     entry_ts 기준 정렬된 거래 내역.
        weight_pct: 매 거래당 잔고 대비 투입 비중 (%).
        initial:    초기 잔고 (KRW).

    Returns:
        (final_balance, max_drawdown_pct) 튜플.
    """
    balance = initial
    peak    = initial
    max_dd  = 0.0

    for t in trades:
        if balance <= 0:
            break
        invested = balance * weight_pct / 100
        balance += invested * t["pnl_pct"] / 100
        if balance > peak:
            peak = balance
        dd = (peak - balance) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    return round(balance, 2), round(max_dd, 2)


# ──────────────────────────────────────────────────────────────────────────────
# 단일 가설 실행
# ──────────────────────────────────────────────────────────────────────────────

def run_case(
    cfg: CaseConfig,
    symbol_filter: str | None = None,
) -> CaseResult:
    """단일 가설(CaseConfig)에 대해 WHITELIST 전체 심볼을 백테스트하고 결과를 반환한다.

    Args:
        cfg:           CaseConfig 인스턴스.
        symbol_filter: 특정 심볼만 테스트 (None이면 WHITELIST 전체).

    Returns:
        CaseResult 인스턴스 (trades 포함, SNIPER/BEAST 시뮬레이션 완료).
    """
    logger.info(
        "===== Case %s 시작: %s (%s 봉, BB %.1fσ, RSI<%d, TP %.1f%% SL %.1f%%, EMA200=%s) =====",
        cfg.name, cfg.label, cfg.timeframe,
        cfg.bb_std, int(cfg.rsi_max), cfg.tp_pct, cfg.sl_pct, cfg.use_ema200,
    )

    ohlcv_map = get_ohlcv(cfg.timeframe, symbol_filter)
    all_trades: list[dict] = []

    for sym, ohlcv in sorted(ohlcv_map.items()):
        trades = backtest_symbol_case(sym, ohlcv, cfg)
        logger.info(
            "  Case %s | %-14s | %4d봉 | 거래 %2d회 (W %d / L %d / TO %d)",
            cfg.name, sym, len(ohlcv), len(trades),
            sum(1 for t in trades if t["result"] == "WIN"),
            sum(1 for t in trades if t["result"] == "LOSS"),
            sum(1 for t in trades if t["result"] == "TIMEOUT"),
        )
        all_trades.extend(trades)

    # 시간순 정렬 (잔고 시뮬레이션 누적 정확도 보장)
    all_trades.sort(key=lambda t: t["entry_ts"])

    s_final, s_mdd = simulate_balance(all_trades, SNIPER_WEIGHT_PCT)
    b_final, b_mdd = simulate_balance(all_trades, BEAST_WEIGHT_PCT)

    result = CaseResult(
        cfg=cfg,
        trades=all_trades,
        s_final=s_final,
        s_mdd=s_mdd,
        b_final=b_final,
        b_mdd=b_mdd,
    )
    logger.info(
        "  Case %s 완료: 거래 %d회 / 승률 %.1f%% / EV %+.2f%% / SNIPER ROI %+.2f%%",
        cfg.name, result.total, result.win_rate, result.expectancy, result.s_roi,
    )
    return result


# ──────────────────────────────────────────────────────────────────────────────
# 케이스별 상세 출력
# ──────────────────────────────────────────────────────────────────────────────

def _pnl_color(pnl: float) -> str:
    return _G if pnl > 0 else (_R if pnl < 0 else "")


def print_case_summary(res: CaseResult) -> None:
    """단일 CaseResult 의 상세 지표를 터미널에 출력한다."""
    cfg   = res.cfg
    total = res.total

    sep  = "─" * 64
    sep2 = "═" * 64
    rr   = cfg.tp_pct / cfg.sl_pct

    print(f"\n{_C}{_B}{'=' * 64}{_RS}")
    print(
        f"{_C}{_B}  🌊 Case {cfg.name} │ {cfg.label}{_RS}"
    )
    print(f"{_C}{sep2}{_RS}")
    print(f"  타임프레임  : {cfg.timeframe}봉")
    ema_str = "  &  Close > EMA200" if cfg.use_ema200 else ""
    print(
        f"  진입 조건   : BB({BB_WINDOW}, {cfg.bb_std:.1f}σ) 하단 터치"
        f"  &  RSI < {cfg.rsi_max:.0f}{ema_str}"
    )
    print(
        f"  손익비 설정 : TP +{cfg.tp_pct:.1f}%  |  SL -{cfg.sl_pct:.1f}%"
        f"  (R:R = {rr:.2f}:1)"
    )
    print(f"  초기 시드   : {INITIAL_BALANCE:,.0f} KRW")
    print(f"{_C}{sep}{_RS}")

    if total == 0:
        print(f"\n{_Y}  [경고] 체결된 거래 없음.{_RS}")
        print(f"  진입 조건을 만족하는 구간이 현재 캐시에 없습니다.\n")
        return

    # ── 손익분기 승률 계산 (R:R 기준) ─────────────────────────────────────
    # R:R = TP/SL 이면 손익분기 승률 = SL / (TP + SL)
    breakeven_wr = cfg.sl_pct / (cfg.tp_pct + cfg.sl_pct) * 100
    wc = _G if res.win_rate >= breakeven_wr else _R

    print(f"\n{_B}  [ 🎯 핵심 지표 ]{_RS}")
    print(f"  {'총 거래 횟수 :':<22} {total}회")
    print(f"  {'승 (WIN)     :':<22} {res.wins}회")
    print(f"  {'패 (LOSS)    :':<22} {res.losses}회")
    print(f"  {'타임아웃     :':<22} {res.timeouts}회")
    print(
        f"  {'승률         :':<22} {wc}{res.win_rate:.1f}%{_RS}"
        f"  {'✅ 수익 구조' if res.win_rate >= breakeven_wr else '❌ 목표 미달'}"
        f" (손익분기 ≥{breakeven_wr:.0f}%)"
    )
    ac = _pnl_color(res.avg_pnl)
    ec = _pnl_color(res.expectancy)
    avg_hold = sum(t["candles_held"] for t in res.trades) / total
    cpb      = 4 if cfg.timeframe == "4h" else 1
    print(f"  {'평균 수익률  :':<22} {ac}{res.avg_pnl:+.2f}%{_RS}")
    print(
        f"  {'기대값(EV)   :':<22} {ec}{res.expectancy:+.2f}%{_RS}"
        f"  ({'양수 → 장기 우위' if res.expectancy > 0 else '음수 → 장기 손실 구조'})"
    )
    print(
        f"  {'평균 보유    :':<22} {avg_hold:.1f}봉"
        f" ({avg_hold * cpb:.0f}시간 / {avg_hold * cpb / 24:.1f}일)"
    )

    # ── SNIPER / BEAST 잔고 비교 ────────────────────────────────────────────
    print(f"\n{_B}  [ 🛡️ SNIPER vs 🔥 BEAST 가상 시드 비교 ]{_RS}")
    sc = _G if res.s_roi >= 0 else _R
    bc = _G if res.b_roi >= 0 else _R
    s_sign = "+" if res.s_roi >= 0 else ""
    b_sign = "+" if res.b_roi >= 0 else ""
    print(f"  {'초기 시드    :':<22} {INITIAL_BALANCE:,.0f} KRW")
    print(
        f"  🛡️ SNIPER ({SNIPER_WEIGHT_PCT:.0f}%)  "
        f"{INITIAL_BALANCE:>12,.0f} → {res.s_final:>12,.0f} KRW"
        f"  ({sc}{s_sign}{res.s_roi:.2f}%{_RS}  MDD -{res.s_mdd:.1f}%)"
    )
    print(
        f"  🔥 BEAST  ({BEAST_WEIGHT_PCT:.0f}%)  "
        f"{INITIAL_BALANCE:>12,.0f} → {res.b_final:>12,.0f} KRW"
        f"  ({bc}{b_sign}{res.b_roi:.2f}%{_RS}  MDD -{res.b_mdd:.1f}%)"
    )

    # ── 심볼별 상세 ─────────────────────────────────────────────────────────
    sym_stats: dict[str, dict] = {}
    for t in res.trades:
        s = t["symbol"]
        if s not in sym_stats:
            sym_stats[s] = {"total": 0, "wins": 0, "pnl_sum": 0.0}
        sym_stats[s]["total"]   += 1
        sym_stats[s]["wins"]    += 1 if t["result"] == "WIN" else 0
        sym_stats[s]["pnl_sum"] += t["pnl_pct"]

    filtered = {s: v for s, v in sym_stats.items() if v["total"] >= MIN_TRADES_FOR_DETAIL}
    if filtered:
        print(f"\n{_B}  [ 심볼별 성적 (≥{MIN_TRADES_FOR_DETAIL}회 거래만 표시) ]{_RS}")
        print(f"  {'심볼':<16} {'거래':>5} {'승률':>7} {'평균PnL':>9} {'합계PnL':>10}")
        print(f"  {'─' * 52}")
        for sym, s in sorted(filtered.items(), key=lambda x: -x[1]["pnl_sum"]):
            wr    = s["wins"] / s["total"] * 100
            avg_p = s["pnl_sum"] / s["total"]
            c     = _pnl_color(avg_p)
            print(
                f"  {sym:<16} {s['total']:>4}회  {wr:>5.1f}%"
                f"  {c}{avg_p:>+7.2f}%{_RS}"
                f"  {c}{s['pnl_sum']:>+8.2f}%{_RS}"
            )

    print(f"\n{_C}{sep2}{_RS}\n")


# ──────────────────────────────────────────────────────────────────────────────
# 통합 비교 테이블 출력
# ──────────────────────────────────────────────────────────────────────────────

def print_comparison_table(results: list[CaseResult]) -> None:
    """3가지 가설의 핵심 지표를 한눈에 비교하는 통합 요약 테이블을 출력한다.

    포함 항목:
      케이스 레이블 / 거래 횟수 / 승률 / 기대값(EV) / 평균 수익률 / SNIPER 최종 잔고

    Args:
        results: run_case() 반환 CaseResult 리스트 (Case A → B → C 순서).
    """
    sep2 = "═" * 80

    print(f"\n{_C}{_B}{'=' * 80}{_RS}")
    print(f"{_C}{_B}  📊 V2 가설 비교 테이블 — 볼린저 핑퐁 (Bollinger Ping-Pong){_RS}")
    print(f"{_C}{sep2}{_RS}\n")

    # ── 헤더 ──────────────────────────────────────────────────────────────
    h_case  = "Case"
    h_cnt   = "거래"
    h_wr    = "승률"
    h_ev    = "기대값(EV)"
    h_avg   = "평균PnL"
    h_sniper= "SNIPER 잔고 (ROI)"

    print(
        f"  {_B}{'':2} {'케이스 설명':<36} {h_cnt:>5} {h_wr:>7} "
        f"{h_ev:>10} {h_avg:>8} {h_sniper:>22}{_RS}"
    )
    print(f"  {'─' * 76}")

    for res in results:
        cfg = res.cfg

        # 손익분기 승률 계산
        breakeven_wr = cfg.sl_pct / (cfg.tp_pct + cfg.sl_pct) * 100
        is_profitable = res.win_rate >= breakeven_wr

        wr_mark = f"{_G}✅{_RS}" if is_profitable else f"{_R}❌{_RS}"
        wrc     = _G if is_profitable else _R
        evc     = _pnl_color(res.expectancy)
        ac      = _pnl_color(res.avg_pnl)
        sc      = _G if res.s_roi >= 0 else _R
        s_sign  = "+" if res.s_roi >= 0 else ""

        wr_str      = f"{wrc}{res.win_rate:>5.1f}%{_RS}"
        ev_str      = f"{evc}{res.expectancy:>+8.2f}%{_RS}"
        avg_str     = f"{ac}{res.avg_pnl:>+6.2f}%{_RS}"
        sniper_str  = (
            f"{sc}{res.s_final:>10,.0f} KRW ({s_sign}{res.s_roi:.1f}%){_RS}"
        )

        trade_str = f"{res.total:>4}회" if res.total else f"{_Y}   0회{_RS}"

        print(
            f"  {_B}{cfg.name}{_RS} "
            f"{cfg.label:<36} "
            f"{trade_str} "
            f"{wr_str} {wr_mark} "
            f"{ev_str} "
            f"{avg_str} "
            f"{sniper_str}"
        )

    print(f"\n  {'─' * 76}")

    # ── 최적 케이스 자동 선정 (EV 기준) ───────────────────────────────────
    valid = [r for r in results if r.total > 0]
    if valid:
        best = max(valid, key=lambda r: r.expectancy)
        print(
            f"\n  {_G}{_B}🏆 최고 기대값(EV): Case {best.cfg.name} "
            f"— {best.cfg.label}{_RS}"
        )
        print(f"     EV {best.expectancy:+.2f}%  |  승률 {best.win_rate:.1f}%  |"
              f"  SNIPER ROI {best.s_roi:+.1f}%\n")

    print(f"{_C}{sep2}{_RS}\n")


# ──────────────────────────────────────────────────────────────────────────────
# CSV 저장
# ──────────────────────────────────────────────────────────────────────────────

def save_csv_case(res: CaseResult) -> Path:
    """단일 CaseResult 의 거래 내역을 CSV로 저장한다.

    Args:
        res: 저장할 CaseResult.

    Returns:
        저장된 CSV 파일 경로.
    """
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    ts_str   = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    cfg      = res.cfg
    out_path = (
        RESULT_DIR
        / f"bb_v2_case{cfg.name}_tp{cfg.tp_pct:.0f}_sl{cfg.sl_pct:.0f}_{ts_str}.csv"
    )

    fieldnames = [
        "Case", "Symbol", "Timeframe",
        "Entry_Time_KST", "Exit_Time_KST",
        "Entry_Price", "Result", "PnL_Pct", "Candles_Held",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for t in res.trades:
            entry_dt = datetime.fromtimestamp(
                t["entry_ts"] / 1000, tz=KST
            ).strftime("%Y-%m-%d %H:%M")
            exit_dt  = datetime.fromtimestamp(
                t["exit_ts"] / 1000, tz=KST
            ).strftime("%Y-%m-%d %H:%M")
            writer.writerow({
                "Case":           cfg.name,
                "Symbol":         t["symbol"],
                "Timeframe":      cfg.timeframe,
                "Entry_Time_KST": entry_dt,
                "Exit_Time_KST":  exit_dt,
                "Entry_Price":    round(t["entry_price"], 4),
                "Result":         t["result"],
                "PnL_Pct":        round(t["pnl_pct"], 4),
                "Candles_Held":   t["candles_held"],
            })

    return out_path


# ──────────────────────────────────────────────────────────────────────────────
# CLI 진입점
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "메이저 코인 볼린저 핑퐁 V2 — 3가지 개선 가설 동시 검증 "
            "(Case A: 1h 스캘핑 / Case B: 4h+EMA200 / Case C: 4h 2.8σ)"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--symbol",
        default=None,
        help="특정 심볼만 테스트 (예: BTC_KRW). 미지정 시 WHITELIST 전체",
    )
    parser.add_argument(
        "--case",
        default=None,
        choices=["A", "B", "C"],
        help="특정 케이스만 실행. 미지정 시 A·B·C 모두 실행",
    )
    parser.add_argument(
        "--no-detail",
        action="store_true",
        default=False,
        help="케이스별 상세 출력 생략 (비교 테이블만 출력)",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        default=False,
        help="각 케이스의 거래 내역을 .result/ 디렉터리에 CSV로 저장",
    )
    args = parser.parse_args()

    # 실행 대상 케이스 필터
    target_cases = [c for c in CASES if args.case is None or c.name == args.case]

    logger.info(
        "볼린저 핑퐁 V2 시작 | 케이스: %s | 심볼: %s | 상세출력: %s",
        [c.name for c in target_cases],
        args.symbol or "WHITELIST 전체",
        not args.no_detail,
    )

    # ── 가설별 백테스트 실행 ─────────────────────────────────────────────
    results: list[CaseResult] = []
    for cfg in target_cases:
        res = run_case(cfg, symbol_filter=args.symbol)
        results.append(res)

        # 케이스별 상세 출력
        if not args.no_detail:
            print_case_summary(res)

        # CSV 저장 (옵션)
        if args.csv and res.total > 0:
            csv_path = save_csv_case(res)
            print(f"  💾 Case {cfg.name} CSV 저장 완료: {csv_path}\n")

    # ── 통합 비교 테이블 (항상 출력) ─────────────────────────────────────
    if len(results) > 1 or args.no_detail:
        print_comparison_table(results)


if __name__ == "__main__":
    main()
