"""
fast_backtest_trend.py — 메이저 코인 정배열 추세 돌파(Trend Catcher) 3-Case 백테스트.

전략 피벗 배경:
  볼린저 핑퐁 V2(역추세) 백테스트 결과, 메이저 코인에서 확정적 Negative EV 기록.
  무거운 시가총액의 관성적 특성 → 하락 시 줍는 역추세는 '떨어지는 칼날' 패턴.
  → 역추세 로직 폐기, 상승 모멘텀 발생 시 따라붙는 추세 추종으로 피벗.

공통 진입 조건 (4h 봉, 메이저 8종):
  ① Close > EMA_200  — 장기 상승장 필터 (추세 방향성 확인)
  ② EMA_20 > EMA_50  — 단기 정배열 필터 (단기 모멘텀 우위 확인)
  ③ Close > BB Upper (20, 2.0σ) — 볼린저 상단 돌파 (모멘텀 폭발 신호)

손익비 최적화 3가지 Case:
  Case A │ 안전 추세   │ TP +4.0% / SL -2.0% (R:R 2.0:1)
  Case B │ 빅 트렌드   │ TP +6.0% / SL -3.0% (R:R 2.0:1)
  Case C │ 트레일링 모사│ TP +8.0% / SL -3.0% (R:R 2.6:1)

데이터 처리:
  - 로컬 JSON 캐시(.cache/ohlcv/) 에서만 읽음 — 업비트 API 미호출
  - 4h 데이터는 프로세스 내 메모리 캐시로 1회만 로드 (3 Case 전체 공유)
  - EMA200 워밍업 200봉 + EMA50 50봉 → 최소 215봉 기준 설정
  - 진입 신호 배열을 심볼별 1회 계산 후 3 Case 공유 (지표 중복 연산 방지)

실행 예시:
  python scripts/fast_backtest_trend.py
  python scripts/fast_backtest_trend.py --symbol BTC_KRW
  python scripts/fast_backtest_trend.py --case A
  python scripts/fast_backtest_trend.py --no-detail
  python scripts/fast_backtest_trend.py --csv
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
EMA_SHORT_PERIOD:  int   = 20    # 정배열 필터용 단기 EMA
EMA_MID_PERIOD:    int   = 50    # 정배열 필터용 중기 EMA
EMA_LONG_PERIOD:   int   = 200   # 장기 상승장 필터용 EMA

BB_WINDOW:         int   = 20    # 볼린저 밴드 기간
BB_STD:            float = 2.0   # 볼린저 밴드 표준편차 배수 (모멘텀 폭발 돌파 기준)

TIMEFRAME:         str   = "4h"  # 4시간 봉 — 모든 Case 공통

# 최소봉 = EMA200 워밍업(200) + EMA50 워밍업(50) + 여유
_MIN_CANDLES: int = EMA_LONG_PERIOD + EMA_MID_PERIOD + 15

SNIPER_WEIGHT_PCT: float = 20.0       # 🛡️ SNIPER — 잔고의 20% 투입
BEAST_WEIGHT_PCT:  float = 70.0       # 🔥 BEAST  — 잔고의 70% 투입
INITIAL_BALANCE:   float = 1_000_000  # 초기 시드 (KRW)

MIN_TRADES_FOR_DETAIL: int = 3

# 테스트 대상: 메이저 8종 (추세 추종 전략 — 시가총액 관성이 있는 코인)
# 이전 볼린저 핑퐁과 달리 SUI/PEPE 대신 DOT/TRX 편입 (유동성·관성 우선)
WHITELIST: list[str] = [
    "BTC_KRW",
    "ETH_KRW",
    "SOL_KRW",
    "XRP_KRW",
    "ADA_KRW",
    "DOGE_KRW",
    "DOT_KRW",
    "TRX_KRW",
]

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

# pandas 임포트 (볼린저 밴드 rolling std)
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
# Case 설정 데이터클래스
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class CaseConfig:
    """단일 백테스트 케이스의 파라미터.

    Attributes:
        name:    케이스 식별자 ("A" / "B" / "C").
        label:   터미널 출력용 레이블.
        tp_pct:  익절률 (%).
        sl_pct:  손절률 (%).
    """
    name:   str
    label:  str
    tp_pct: float
    sl_pct: float

    @property
    def rr(self) -> float:
        return self.tp_pct / self.sl_pct

    @property
    def breakeven_wr(self) -> float:
        """손익분기 승률: SL / (TP + SL) × 100."""
        return self.sl_pct / (self.tp_pct + self.sl_pct) * 100


@dataclass
class CaseResult:
    """단일 케이스의 집계 결과.

    Attributes:
        cfg:     해당 케이스의 CaseConfig.
        trades:  entry_ts 오름차순 정렬된 전체 거래 내역.
        s_final: SNIPER(20%) 시뮬레이션 최종 잔고.
        s_mdd:   SNIPER 최대 낙폭 (%).
        b_final: BEAST(70%) 시뮬레이션 최종 잔고.
        b_mdd:   BEAST 최대 낙폭 (%).
    """
    cfg:     CaseConfig
    trades:  list[dict[str, Any]] = field(default_factory=list)
    s_final: float = INITIAL_BALANCE
    s_mdd:   float = 0.0
    b_final: float = INITIAL_BALANCE
    b_mdd:   float = 0.0

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
        loss_rate = self.losses / self.total
        return (self.win_rate / 100) * self.cfg.tp_pct - loss_rate * self.cfg.sl_pct

    @property
    def s_roi(self) -> float:
        return (self.s_final - INITIAL_BALANCE) / INITIAL_BALANCE * 100

    @property
    def b_roi(self) -> float:
        return (self.b_final - INITIAL_BALANCE) / INITIAL_BALANCE * 100


# ──────────────────────────────────────────────────────────────────────────────
# 3가지 케이스 정의
# ──────────────────────────────────────────────────────────────────────────────

CASES: list[CaseConfig] = [
    CaseConfig(
        name="A",
        label="안전 추세   (TP +4.0% / SL -2.0%  R:R 2.0:1)",
        tp_pct=4.0,
        sl_pct=2.0,
    ),
    CaseConfig(
        name="B",
        label="빅 트렌드   (TP +6.0% / SL -3.0%  R:R 2.0:1)",
        tp_pct=6.0,
        sl_pct=3.0,
    ),
    CaseConfig(
        name="C",
        label="트레일링 모사 (TP +8.0% / SL -3.0%  R:R 2.6:1)",
        tp_pct=8.0,
        sl_pct=3.0,
    ),
]


# ──────────────────────────────────────────────────────────────────────────────
# 지표 계산
# ──────────────────────────────────────────────────────────────────────────────

def calc_ema(closes: list[float], period: int) -> list[float | None]:
    """지수이동평균(EMA)을 계산한다.

    초기 period 봉의 SMA를 시드로 사용하고
    이후 EMA 승수(2 / (period+1))를 적용한다.

    Args:
        closes: 종가 리스트 (오름차순, 과거→현재).
        period: EMA 기간 (봉 수).

    Returns:
        각 봉의 EMA 값 리스트. 워밍업 구간은 None.
    """
    result: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return result

    seed = sum(closes[:period]) / period
    result[period - 1] = seed

    multiplier = 2.0 / (period + 1)
    prev = seed
    for i in range(period, len(closes)):
        ema = closes[i] * multiplier + prev * (1.0 - multiplier)
        result[i] = ema
        prev = ema

    return result


def calc_bb_upper(
    closes: list[float],
    window: int = BB_WINDOW,
    num_std: float = BB_STD,
) -> list[float | None]:
    """볼린저 밴드 상단(Upper Band)만 계산한다.

    pandas가 있으면 rolling(ddof=1) std를 사용하고,
    없으면 순수 Python 표본 표준편차(ddof=1)로 폴백한다.

    Args:
        closes:  종가 리스트 (오름차순, 과거→현재).
        window:  SMA·표준편차 계산 기간 (봉). 기본 20.
        num_std: 표준편차 배수. 기본 2.0.

    Returns:
        각 봉의 Upper Band 값 리스트. 워밍업 구간은 None.
    """
    n = len(closes)
    upper: list[float | None] = [None] * n

    if _PANDAS_AVAILABLE:
        s    = pd.Series(closes)
        _mid = s.rolling(window).mean()
        _std = s.rolling(window).std(ddof=1)
        for i in range(n):
            mv, sv = _mid.iloc[i], _std.iloc[i]
            if pd.isna(mv) or pd.isna(sv):
                continue
            upper[i] = float(mv) + num_std * float(sv)
    else:
        for i in range(window - 1, n):
            w    = closes[i - window + 1 : i + 1]
            mean = sum(w) / window
            var  = sum((v - mean) ** 2 for v in w) / (window - 1)
            std  = math.sqrt(var)
            upper[i] = mean + num_std * std

    return upper


def build_entry_signals(ohlcv: list[list]) -> list[bool]:
    """공통 진입 조건을 기반으로 봉별 진입 신호 배열을 생성한다.

    공통 조건 (3가지 모두 충족 시 True):
      ① Close > EMA_200  — 장기 상승장 필터
      ② EMA_20 > EMA_50  — 단기 정배열 (골든 크로스 구간)
      ③ Close > BB Upper(20, 2.0σ)  — 볼린저 상단 돌파 (모멘텀 폭발)

    모든 케이스가 동일한 진입 조건을 공유하므로
    이 함수를 1회만 호출하고 결과를 3 Case가 재사용한다.

    Args:
        ohlcv: [[ts_ms, open, high, low, close, volume], ...] (오름차순).

    Returns:
        len(ohlcv)와 동일한 길이의 bool 리스트.
        True = 해당 봉에서 진입 조건 충족.
    """
    closes  = [float(c[4]) for c in ohlcv]
    signals = [False] * len(ohlcv)

    ema20  = calc_ema(closes, EMA_SHORT_PERIOD)
    ema50  = calc_ema(closes, EMA_MID_PERIOD)
    ema200 = calc_ema(closes, EMA_LONG_PERIOD)
    bb_up  = calc_bb_upper(closes, BB_WINDOW, BB_STD)

    for i in range(len(ohlcv)):
        e20 = ema20[i]
        e50 = ema50[i]
        e200 = ema200[i]
        bbu  = bb_up[i]

        # 지표 미준비 구간 스킵
        if e20 is None or e50 is None or e200 is None or bbu is None:
            continue

        close = closes[i]
        cond_trend    = close > e200   # ① 장기 상승장
        cond_aligned  = e20 > e50      # ② 단기 정배열
        cond_breakout = close > bbu    # ③ 볼린저 상단 돌파

        signals[i] = cond_trend and cond_aligned and cond_breakout

    return signals


# ──────────────────────────────────────────────────────────────────────────────
# 단일 심볼 × 단일 Case 백테스트
# ──────────────────────────────────────────────────────────────────────────────

def backtest_symbol_case(
    sym: str,
    ohlcv: list[list],
    entry_signals: list[bool],
    cfg: CaseConfig,
) -> list[dict[str, Any]]:
    """사전 계산된 진입 신호와 CaseConfig의 TP/SL을 적용해 거래 내역을 반환한다.

    [청산 조건]
      - TP: 진입가 × (1 + tp_pct/100) 이상 고가 도달 → WIN
      - SL: 진입가 × (1 - sl_pct/100) 이하 저가 도달 → LOSS
      - 동일 봉 TP·SL 동시 충족: WIN 우선 (상방 이동 먼저 가정)

    [포지션 겹침 방지]
      현재 포지션 청산 전 신규 신호 무시 (per-symbol).

    Args:
        sym:           심볼 식별자 (예: "BTC_KRW").
        ohlcv:         [[ts_ms, open, high, low, close, volume], ...] (오름차순).
        entry_signals: build_entry_signals() 반환값. 봉별 진입 여부.
        cfg:           CaseConfig — TP/SL 적용.

    Returns:
        거래 내역 리스트.
    """
    highs  = [float(c[2]) for c in ohlcv]
    lows   = [float(c[3]) for c in ohlcv]
    closes = [float(c[4]) for c in ohlcv]
    ts_arr = [int(c[0])   for c in ohlcv]

    trades: list[dict[str, Any]] = []
    in_position  = False
    entry_price  = 0.0
    entry_ts_val = 0
    entry_idx    = 0

    for i in range(1, len(ohlcv)):

        # ── 보유 중: TP / SL 확인 ─────────────────────────────────────
        if in_position:
            h        = highs[i]
            l        = lows[i]
            tp_price = entry_price * (1 + cfg.tp_pct / 100)
            sl_price = entry_price * (1 - cfg.sl_pct / 100)

            if h >= tp_price:   # WIN 우선
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

            continue  # 포지션 유지

        # ── 미보유: 진입 신호 확인 ────────────────────────────────────
        if entry_signals[i]:
            in_position  = True
            entry_price  = closes[i]
            entry_ts_val = ts_arr[i]
            entry_idx    = i

    # ── 데이터 소진 후 미청산 포지션 → TIMEOUT ─────────────────────────
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
# 데이터 로더 (in-memory 캐시)
# ──────────────────────────────────────────────────────────────────────────────

_OHLCV_CACHE: dict[str, dict[str, list[list]]] = {}


def get_ohlcv(
    timeframe: str = TIMEFRAME,
    symbol_filter: str | None = None,
) -> dict[str, list[list]]:
    """타임프레임별 OHLCV를 메모리 캐시에서 반환한다.

    첫 호출 시 로컬 JSON 파일을 로드하고 이후 캐시를 재사용한다.
    3 Case 가 모두 동일 4h 캐시를 공유해 중복 I/O를 방지한다.

    Args:
        timeframe:     "4h" (이 스크립트는 모든 케이스 4h 고정).
        symbol_filter: 특정 심볼만 추출. None이면 WHITELIST 전체.

    Returns:
        {심볼: ohlcv} 딕셔너리.
    """
    if timeframe not in _OHLCV_CACHE:
        if not CACHE_DIR.exists():
            logger.error("캐시 디렉터리 없음: %s", CACHE_DIR)
            logger.error("먼저 `python scripts/backtester.py` 를 실행해 캐시를 생성하세요.")
            sys.exit(1)

        files = sorted(CACHE_DIR.glob(f"*_{timeframe}.json"))
        if not files:
            logger.error("캐시 파일 없음 (패턴: *_%s.json)", timeframe)
            sys.exit(1)

        whitelist_upper = {s.upper() for s in WHITELIST}
        loaded: dict[str, list[list]] = {}

        logger.info(
            "[캐시 로드] %s 봉 데이터 로드 시작 (최소 %d봉, API 미호출)",
            timeframe, _MIN_CANDLES,
        )
        for path in files:
            sym = path.stem[: -(len(timeframe) + 1)]
            if sym.upper() not in whitelist_upper:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, list) or len(data) < _MIN_CANDLES:
                    logger.warning(
                        "데이터 부족 스킵 (%s %s): %d봉 < 최소 %d봉",
                        sym, timeframe, len(data), _MIN_CANDLES,
                    )
                    continue
                loaded[sym] = data
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("파일 로드 실패 (%s): %s", path.name, exc)

        if not loaded:
            logger.error(
                "로드된 심볼 없음 (timeframe=%s, min_candles=%d). "
                "캐시를 먼저 생성하세요.",
                timeframe, _MIN_CANDLES,
            )
            sys.exit(1)

        _OHLCV_CACHE[timeframe] = loaded
        logger.info(
            "[캐시 로드] %s 봉 완료: %d개 심볼 (3 Case 공유 캐시)",
            timeframe, len(loaded),
        )
    else:
        logger.info("[캐시 HIT] %s 봉 데이터 재사용 (I/O 스킵)", timeframe)

    data = _OHLCV_CACHE[timeframe]
    if symbol_filter:
        return {k: v for k, v in data.items() if k.upper() == symbol_filter.upper()}
    return data


# ──────────────────────────────────────────────────────────────────────────────
# 잔고 시뮬레이션
# ──────────────────────────────────────────────────────────────────────────────

def simulate_balance(
    trades: list[dict],
    weight_pct: float,
    initial: float = INITIAL_BALANCE,
) -> tuple[float, float]:
    """시간순 거래 내역으로 최종 잔고와 MDD를 계산한다.

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
# 단일 케이스 실행
# ──────────────────────────────────────────────────────────────────────────────

def run_case(
    cfg: CaseConfig,
    ohlcv_map: dict[str, list[list]],
) -> CaseResult:
    """단일 CaseConfig로 WHITELIST 전체 심볼을 백테스트하고 결과를 반환한다.

    진입 신호 배열을 심볼별 1회 계산하고 Case에 재사용해
    동일한 진입 신호에 TP/SL만 다르게 적용하는 구조.

    Args:
        cfg:       CaseConfig 인스턴스.
        ohlcv_map: get_ohlcv() 반환값.

    Returns:
        CaseResult 인스턴스.
    """
    logger.info(
        "===== Case %s: %s (TP %.1f%% / SL %.1f%% / R:R %.1f:1) =====",
        cfg.name, cfg.label, cfg.tp_pct, cfg.sl_pct, cfg.rr,
    )

    all_trades: list[dict] = []

    for sym, ohlcv in sorted(ohlcv_map.items()):
        # 진입 신호 배열: 이 케이스에서 최초 처리하는 심볼이면 계산
        # (동일 ohlcv 참조라 캐싱 없이 연산해도 빠름 — 8심볼 × 265봉 수준)
        entry_signals = build_entry_signals(ohlcv)
        signal_count  = sum(entry_signals)

        trades = backtest_symbol_case(sym, ohlcv, entry_signals, cfg)
        logger.info(
            "  Case %s | %-12s | %5d봉 | 진입신호 %3d개 | 거래 %2d회 (W %d / L %d / TO %d)",
            cfg.name, sym, len(ohlcv), signal_count, len(trades),
            sum(1 for t in trades if t["result"] == "WIN"),
            sum(1 for t in trades if t["result"] == "LOSS"),
            sum(1 for t in trades if t["result"] == "TIMEOUT"),
        )
        all_trades.extend(trades)

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
        "  Case %s 완료: 거래 %d회 / 승률 %.1f%% / EV %+.2f%% / SNIPER ROI %+.2f%% / MDD -%.1f%%",
        cfg.name, result.total, result.win_rate,
        result.expectancy, result.s_roi, result.s_mdd,
    )
    return result


# ──────────────────────────────────────────────────────────────────────────────
# 케이스별 상세 출력
# ──────────────────────────────────────────────────────────────────────────────

def _pnl_color(v: float) -> str:
    return _G if v > 0 else (_R if v < 0 else "")


def print_case_summary(res: CaseResult) -> None:
    """단일 CaseResult 의 상세 지표를 터미널에 출력한다."""
    cfg   = res.cfg
    total = res.total
    sep   = "─" * 68
    sep2  = "═" * 68

    print(f"\n{_C}{_B}{'=' * 68}{_RS}")
    print(f"{_C}{_B}  🚀 Case {cfg.name} │ {cfg.label}{_RS}")
    print(f"{_C}{sep2}{_RS}")
    print(f"  타임프레임  : {TIMEFRAME}봉")
    print(
        f"  공통 진입   : Close > EMA200  &  EMA20 > EMA50  &  Close > BB({BB_WINDOW},{BB_STD:.1f}σ) 상단"
    )
    print(
        f"  손익비 설정 : TP +{cfg.tp_pct:.1f}%  |  SL -{cfg.sl_pct:.1f}%"
        f"  (R:R = {cfg.rr:.1f}:1 | 손익분기 승률 ≥{cfg.breakeven_wr:.0f}%)"
    )
    print(f"  초기 시드   : {INITIAL_BALANCE:,.0f} KRW")
    print(f"{_C}{sep}{_RS}")

    if total == 0:
        print(f"\n{_Y}  [경고] 체결된 거래 없음. 진입 조건이 현재 캐시 기간에 한 번도 충족되지 않았습니다.{_RS}\n")
        return

    wc = _G if res.win_rate >= cfg.breakeven_wr else _R
    wr_mark = "✅ 수익 구조" if res.win_rate >= cfg.breakeven_wr else "❌ 목표 미달"

    avg_hold = sum(t["candles_held"] for t in res.trades) / total

    print(f"\n{_B}  [ 🎯 핵심 지표 ]{_RS}")
    print(f"  {'총 거래 횟수 :':<22} {total}회")
    print(f"  {'승 (WIN)     :':<22} {res.wins}회")
    print(f"  {'패 (LOSS)    :':<22} {res.losses}회")
    print(f"  {'타임아웃     :':<22} {res.timeouts}회")
    print(
        f"  {'승률         :':<22} {wc}{res.win_rate:.1f}%{_RS}"
        f"  {wr_mark} (손익분기 ≥{cfg.breakeven_wr:.0f}%)"
    )
    ac = _pnl_color(res.avg_pnl)
    ec = _pnl_color(res.expectancy)
    print(f"  {'평균 수익률  :':<22} {ac}{res.avg_pnl:+.2f}%{_RS}")
    print(
        f"  {'기대값(EV)   :':<22} {ec}{res.expectancy:+.2f}%{_RS}"
        f"  ({'양수 → 장기 우위' if res.expectancy > 0 else '음수 → 장기 손실 구조'})"
    )
    print(
        f"  {'평균 보유    :':<22} {avg_hold:.1f}봉"
        f" ({avg_hold * 4:.0f}시간 / {avg_hold * 4 / 24:.1f}일)"
    )

    print(f"\n{_B}  [ 🛡️ SNIPER vs 🔥 BEAST 가상 시드 비교 ]{_RS}")
    sc = _G if res.s_roi >= 0 else _R
    bc = _G if res.b_roi >= 0 else _R
    print(f"  {'초기 시드    :':<22} {INITIAL_BALANCE:,.0f} KRW")
    print(
        f"  🛡️ SNIPER ({SNIPER_WEIGHT_PCT:.0f}%)  "
        f"{INITIAL_BALANCE:>12,.0f} → {res.s_final:>12,.0f} KRW"
        f"  ({sc}{'+' if res.s_roi>=0 else ''}{res.s_roi:.2f}%{_RS}  MDD -{res.s_mdd:.1f}%)"
    )
    print(
        f"  🔥 BEAST  ({BEAST_WEIGHT_PCT:.0f}%)  "
        f"{INITIAL_BALANCE:>12,.0f} → {res.b_final:>12,.0f} KRW"
        f"  ({bc}{'+' if res.b_roi>=0 else ''}{res.b_roi:.2f}%{_RS}  MDD -{res.b_mdd:.1f}%)"
    )

    # 심볼별 성적
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
        print(f"  {'심볼':<14} {'거래':>5} {'승률':>7} {'평균PnL':>9} {'합계PnL':>10}")
        print(f"  {'─' * 50}")
        for sym, s in sorted(filtered.items(), key=lambda x: -x[1]["pnl_sum"]):
            wr    = s["wins"] / s["total"] * 100
            avg_p = s["pnl_sum"] / s["total"]
            c     = _pnl_color(avg_p)
            print(
                f"  {sym:<14} {s['total']:>4}회  {wr:>5.1f}%"
                f"  {c}{avg_p:>+7.2f}%{_RS}  {c}{s['pnl_sum']:>+8.2f}%{_RS}"
            )

    print(f"\n{_C}{sep2}{_RS}\n")


# ──────────────────────────────────────────────────────────────────────────────
# 통합 비교 테이블 출력
# ──────────────────────────────────────────────────────────────────────────────

def print_comparison_table(results: list[CaseResult]) -> None:
    """3가지 케이스의 핵심 지표를 비교하는 요약 테이블을 출력한다.

    포함 항목: 거래 횟수 / 승률 / 기대값(EV) / 평균 PnL / MDD (SNIPER 기준)

    Args:
        results: CaseResult 리스트 (Case A → B → C 순서).
    """
    sep2 = "═" * 84

    print(f"\n{_C}{_B}{'=' * 84}{_RS}")
    print(f"{_C}{_B}  📊 추세 돌파(Trend Catcher) 3-Case 비교 테이블 — 메이저 8종 / 4h 봉{_RS}")
    print(f"{_C}{_B}  진입 조건: Close>EMA200  &  EMA20>EMA50  &  Close>BB({BB_WINDOW},{BB_STD:.1f}σ) 상단{_RS}")
    print(f"{_C}{sep2}{_RS}\n")

    print(
        f"  {_B}{'':2} {'케이스 설명':<38} {'거래':>5} {'승률':>8} "
        f"{'EV':>8} {'평균PnL':>8} {'MDD(S)':>8} {'SNIPER 잔고':>18}{_RS}"
    )
    print(f"  {'─' * 82}")

    for res in results:
        cfg = res.cfg

        is_win      = res.win_rate >= cfg.breakeven_wr
        wr_mark     = f"{_G}✅{_RS}" if is_win else f"{_R}❌{_RS}"
        wrc         = _G if is_win else _R
        evc         = _pnl_color(res.expectancy)
        ac          = _pnl_color(res.avg_pnl)
        sc          = _G if res.s_roi >= 0 else _R
        mdd_c       = _G if res.s_mdd < 15 else (_Y if res.s_mdd < 30 else _R)
        s_sign      = "+" if res.s_roi >= 0 else ""

        trade_str  = f"{res.total:>4}회" if res.total else f"{_Y}   0회{_RS}"
        wr_str     = f"{wrc}{res.win_rate:>5.1f}%{_RS}"
        ev_str     = f"{evc}{res.expectancy:>+6.2f}%{_RS}"
        avg_str    = f"{ac}{res.avg_pnl:>+5.2f}%{_RS}"
        mdd_str    = f"{mdd_c}-{res.s_mdd:.1f}%{_RS}"
        sniper_str = f"{sc}{res.s_final:>10,.0f} KRW ({s_sign}{res.s_roi:.1f}%){_RS}"

        print(
            f"  {_B}{cfg.name}{_RS} "
            f"{cfg.label:<38} "
            f"{trade_str} "
            f"{wr_str} {wr_mark} "
            f"{ev_str} "
            f"{avg_str} "
            f"{mdd_str:>8} "
            f"{sniper_str}"
        )

    print(f"\n  {'─' * 82}")

    # ── 최적 케이스 자동 선정 ─────────────────────────────────────────────
    valid = [r for r in results if r.total > 0]
    if valid:
        best_ev  = max(valid, key=lambda r: r.expectancy)
        best_mdd = min(valid, key=lambda r: r.s_mdd)

        print(f"\n  {_G}{_B}🏆 최고 기대값(EV)  : Case {best_ev.cfg.name} "
              f"— EV {best_ev.expectancy:+.2f}% / 승률 {best_ev.win_rate:.1f}% / MDD -{best_ev.s_mdd:.1f}%{_RS}")
        print(f"  {_C}{_B}🛡️ 최저 MDD       : Case {best_mdd.cfg.name} "
              f"— MDD -{best_mdd.s_mdd:.1f}% / EV {best_mdd.expectancy:+.2f}%{_RS}\n")

    print(f"{_C}{sep2}{_RS}\n")


# ──────────────────────────────────────────────────────────────────────────────
# CSV 저장
# ──────────────────────────────────────────────────────────────────────────────

def save_csv_case(res: CaseResult) -> Path:
    """단일 CaseResult 의 거래 내역을 CSV로 저장한다."""
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    ts_str   = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    cfg      = res.cfg
    out_path = (
        RESULT_DIR
        / f"trend_case{cfg.name}_tp{cfg.tp_pct:.0f}_sl{cfg.sl_pct:.0f}_{ts_str}.csv"
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
            exit_dt = datetime.fromtimestamp(
                t["exit_ts"] / 1000, tz=KST
            ).strftime("%Y-%m-%d %H:%M")
            writer.writerow({
                "Case":           cfg.name,
                "Symbol":         t["symbol"],
                "Timeframe":      TIMEFRAME,
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
            "메이저 코인 정배열 추세 돌파 3-Case 백테스트 "
            "(Case A: TP4/SL2 / Case B: TP6/SL3 / Case C: TP8/SL3)"
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
        help="거래 내역을 .result/ 디렉터리에 CSV로 저장",
    )
    args = parser.parse_args()

    target_cases = [c for c in CASES if args.case is None or c.name == args.case]

    logger.info(
        "추세 돌파 Trend Catcher 백테스트 시작 | 전략 피벗: 역추세 폐기 → 추세 추종 채택\n"
        "  진입 조건: Close>EMA200 & EMA20>EMA50 & Close>BB(%d,%.1fσ) 상단 | 케이스: %s",
        BB_WINDOW, BB_STD, [c.name for c in target_cases],
    )

    # ── 4h 데이터 1회 로드 — 3 Case 공유 ─────────────────────────────
    ohlcv_map = get_ohlcv(TIMEFRAME, args.symbol)

    # ── 케이스별 실행 ─────────────────────────────────────────────────
    results: list[CaseResult] = []
    for cfg in target_cases:
        res = run_case(cfg, ohlcv_map)
        results.append(res)

        if not args.no_detail:
            print_case_summary(res)

        if args.csv and res.total > 0:
            csv_path = save_csv_case(res)
            print(f"  💾 Case {cfg.name} CSV 저장 완료: {csv_path}\n")

    # ── 통합 비교 테이블 (항상 출력) ─────────────────────────────────
    if len(results) > 1 or args.no_detail:
        print_comparison_table(results)


if __name__ == "__main__":
    main()
