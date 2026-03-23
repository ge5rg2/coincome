"""
fast_backtest_bollinger.py — 메이저 코인 볼린저 밴드 핑퐁(박스권) 전략 백테스트.

LLM · 업비트 API 호출 없이 .cache/ohlcv/ JSON 파일만 읽어
순수 수학 연산으로 볼린저 밴드 핑퐁(Bollinger Ping-Pong) 전략을 검증한다.

[전략 — 메이저 코인 볼린저 핑퐁 v1]
  대상: 메이저 8종 (BTC / ETH / XRP / SOL / DOGE / ADA / SUI / PEPE)
        기존 fast_backtest.py 가 제외하던 "블랙리스트 코인"이 여기선 테스트 대상.
        → 메이저 코인은 알트코인 대비 박스권 회귀(mean-reversion) 경향이 강하다는 가설 검증.

  타임프레임: 4h (4시간 봉)

  진입 조건 (두 조건 모두 충족 시 해당 봉 종가에 매수):
    1) Close < 볼린저 밴드 하단 (Lower Band)  — 과매도 하단 이탈·터치
    2) 4h RSI < 40                            — 모멘텀 과매도 확인 (고점 하단 터치 제외)

  볼린저 밴드 설정:
    - Window (SMA 기간): 20봉
    - StdDev 배수      :  2.0
    - Mid Band = MA20, Upper = MA20 + 2σ, Lower = MA20 − 2σ

  청산 조건 (핑퐁 손익비 1:1):
    - 익절(TP): 진입가 대비 +3.0% 도달 → 중단 밴드(MA20) 회귀 구간에서 짧게 청산
    - 손절(SL): 진입가 대비 -3.0% 도달 → 하단을 뚫고 지속 하락 시 칼손절
    - 우선순위: WIN > LOSS (동일 봉에서 TP·SL 동시 충족 시 WIN 적용)
    - 겹침 방지: 현재 포지션 청산 전까지 신규 진입 신호 무시 (per-symbol)

[의존성]
  - 표준 라이브러리 + pandas (볼린저 밴드 rolling std 계산)
  - pandas 없이 실행 시 내장 순수 Python 폴백 자동 사용

실행 예시:
  python scripts/fast_backtest_bollinger.py
  python scripts/fast_backtest_bollinger.py --symbol BTC_KRW
  python scripts/fast_backtest_bollinger.py --tp 3.0 --sl 3.0
  python scripts/fast_backtest_bollinger.py --csv
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ──────────────────────────────────────────────────────────────────────────────
# ★ 전략 파라미터 — 여기를 수정해 전략을 튜닝하세요
# ──────────────────────────────────────────────────────────────────────────────
TAKE_PROFIT_PCT: float = 3.0   # 익절률 (%) — 박스권 회귀 짧은 익절 (R:R 1:1)
STOP_LOSS_PCT:   float = 3.0   # 손절률 (%) — 하단 돌파 시 칼손절 (R:R 1:1)

# 볼린저 밴드 파라미터
BB_WINDOW:   int   = 20    # 이동평균(SMA) 기간 (봉)
BB_STD_DEV:  float = 2.0   # 표준편차 배수 (σ)

# RSI 진입 조건: RSI < RSI_ENTRY_MAX (과매도 구간만 진입)
RSI_ENTRY_MAX: float = 40.0  # 진입 허용 RSI 최댓값 (이 이상이면 진입 금지)
RSI_PERIOD:    int   = 14    # RSI 계산 기간 (봉)

# 모드별 투입 비중 (잔고 시뮬레이션용)
SNIPER_WEIGHT_PCT: float = 20.0       # 🛡️ SNIPER — 잔고의 20% 투입
BEAST_WEIGHT_PCT:  float = 70.0       # 🔥 BEAST  — 잔고의 70% 투입
INITIAL_BALANCE:   float = 1_000_000  # 초기 시드 (KRW)

# 심볼별 요약: 최소 이 거래 횟수 이상만 표시
MIN_TRADES_FOR_DETAIL: int = 3

# 테스트 대상: 메이저 8종 (기존 fast_backtest.py 의 BLACKLIST → 여기선 WHITELIST)
# 이 코인들이 박스권 회귀 특성을 보이는지 검증한다
WHITELIST: list[str] = [
    "BTC_KRW",   # 비트코인    — 글로벌 레퍼런스, 강한 지지·저항 박스권 형성
    "ETH_KRW",   # 이더리움   — 대형 매물대·박스권 회귀 빈도 높음
    "XRP_KRW",   # 리플       — 규제 뉴스 이후 횡보·회귀 경향
    "SOL_KRW",   # 솔라나     — 이슈 후 급락 → 회귀 패턴 잦음
    "DOGE_KRW",  # 도지코인   — 밈코인 과매도 반등 회귀 패턴
    "ADA_KRW",   # 에이다     — 무거운 메이저 알트, 하단 회귀 경향
    "SUI_KRW",   # 수이       — 신흥 L1, 하단 터치 후 반등 패턴
    "PEPE_KRW",  # 페페       — 밈코인 급락 후 단기 반등 빈발
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

# ANSI 색상 코드 (터미널 컬러 출력)
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

# pandas 임포트 (볼린저 밴드 rolling std 사용 — 없으면 순수 Python 폴백)
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
# 지표 계산
# ──────────────────────────────────────────────────────────────────────────────

def calc_ma(closes: list[float], period: int = BB_WINDOW) -> list[float | None]:
    """단순 이동평균(SMA)을 계산한다.

    워밍업 구간(period - 1개 미만)은 None으로 채워 미준비 상태를 명시한다.

    Args:
        closes: 종가 리스트 (오름차순, 과거→현재).
        period: 이동평균 기간 (봉 수).

    Returns:
        각 봉의 SMA값 리스트. 계산 불가 구간은 None.
    """
    result: list[float | None] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        result[i] = sum(closes[i - period + 1 : i + 1]) / period
    return result


def calc_bollinger_bands(
    closes: list[float],
    window: int = BB_WINDOW,
    num_std: float = BB_STD_DEV,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """볼린저 밴드 (Upper / Mid / Lower)를 계산한다.

    pandas가 있으면 rolling(ddof=1) std를 사용하고,
    없으면 순수 Python으로 동일한 표본 표준편차(ddof=1)를 계산한다.

    Args:
        closes:  종가 리스트 (오름차순, 과거→현재).
        window:  이동평균·표준편차 계산 기간 (봉). 기본 20.
        num_std: 표준편차 배수. 기본 2.0.

    Returns:
        (upper, mid, lower) — 각각 봉 수만큼의 float|None 리스트.
        워밍업 구간(window 미만)은 None.
    """
    n = len(closes)
    upper: list[float | None] = [None] * n
    mid:   list[float | None] = [None] * n
    lower: list[float | None] = [None] * n

    if _PANDAS_AVAILABLE:
        # ── pandas rolling 경로 ────────────────────────────────────────────
        s    = pd.Series(closes)
        _mid = s.rolling(window).mean()
        _std = s.rolling(window).std(ddof=1)      # 표본 표준편차 (ddof=1)
        for i in range(n):
            mv = _mid.iloc[i]
            sv = _std.iloc[i]
            if pd.isna(mv) or pd.isna(sv):
                continue
            mid[i]   = float(mv)
            upper[i] = float(mv) + num_std * float(sv)
            lower[i] = float(mv) - num_std * float(sv)
    else:
        # ── 순수 Python 폴백: 표본 표준편차 (ddof=1) ──────────────────────
        for i in range(window - 1, n):
            window_vals = closes[i - window + 1 : i + 1]
            mean_val    = sum(window_vals) / window
            variance    = sum((v - mean_val) ** 2 for v in window_vals) / (window - 1)
            std_val     = math.sqrt(variance)
            mid[i]      = mean_val
            upper[i]    = mean_val + num_std * std_val
            lower[i]    = mean_val - num_std * std_val

    return upper, mid, lower


def calc_rsi(closes: list[float], period: int = RSI_PERIOD) -> list[float | None]:
    """Wilder's Smoothed RSI(상대강도지수)를 계산한다.

    초기 avg_gain / avg_loss는 단순 평균으로 시드(seed)하고,
    이후 Wilder's 지수 가중 이동평균(EMA)을 적용한다.

    RSI 공식:
      RS      = avg_gain / avg_loss
      RSI     = 100 - 100 / (1 + RS)
      avg_new = (avg_old × (period-1) + current_change) / period

    Args:
        closes: 종가 리스트 (오름차순, 과거→현재).
        period: RSI 기간 (봉 수).

    Returns:
        각 봉의 RSI값 리스트. 계산 불가 구간은 None.
    """
    result: list[float | None] = [None] * len(closes)
    if len(closes) < period + 1:
        return result

    # ── [1] 초기 시드: 처음 period 개 변화량으로 단순 평균 계산 ──────────
    init_gains:  list[float] = []
    init_losses: list[float] = []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            init_gains.append(diff)
            init_losses.append(0.0)
        else:
            init_gains.append(0.0)
            init_losses.append(abs(diff))

    avg_gain = sum(init_gains)  / period
    avg_loss = sum(init_losses) / period

    # 첫 번째 RSI (index = period)
    if avg_loss == 0:
        result[period] = 100.0
    else:
        result[period] = 100 - 100 / (1 + avg_gain / avg_loss)

    # ── [2] Wilder's Smoothing 적용 (index = period+1 ~ 끝) ──────────────
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = diff       if diff > 0 else 0.0
        loss = abs(diff)  if diff < 0 else 0.0

        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0:
            result[i] = 100.0
        else:
            result[i] = 100 - 100 / (1 + avg_gain / avg_loss)

    return result


# ──────────────────────────────────────────────────────────────────────────────
# 데이터 로더 (WHITELIST 전용)
# ──────────────────────────────────────────────────────────────────────────────

def load_ohlcv_files(
    timeframe: str = "4h",
    symbol_filter: str | None = None,
) -> dict[str, list[list]]:
    """CACHE_DIR 내 JSON 파일들 중 WHITELIST(메이저 8종)만 로드한다.

    파일 포맷: [[timestamp_ms, open, high, low, close, volume], ...] (오름차순)

    Args:
        timeframe:     타임프레임 접미사 필터 (예: "4h" → *_4h.json 만 로드).
        symbol_filter: 특정 심볼만 로드 (예: "BTC_KRW"). None이면 WHITELIST 전체.

    Returns:
        {"BTC_KRW": [[ts, o, h, l, c, v], ...], ...}
    """
    if not CACHE_DIR.exists():
        logger.error("캐시 디렉터리 없음: %s", CACHE_DIR)
        logger.error("먼저 `python scripts/backtester.py` 를 실행해 캐시를 생성하세요.")
        sys.exit(1)

    files = sorted(CACHE_DIR.glob(f"*_{timeframe}.json"))
    if not files:
        logger.error("캐시 파일 없음 (패턴: *_%s.json)", timeframe)
        sys.exit(1)

    # 최소 데이터 요건: BB 워밍업(19봉) + RSI 워밍업(14봉) + 여유
    min_candles = BB_WINDOW + RSI_PERIOD + 5

    # WHITELIST 대소문자 무관 매칭용 집합
    whitelist_upper = {s.upper() for s in WHITELIST}

    result: dict[str, list[list]] = {}
    for path in files:
        # 심볼 추출: "BTC_KRW_4h.json" → "BTC_KRW"
        sym = path.stem[: -(len(timeframe) + 1)]

        # ── WHITELIST 필터 (메이저 코인만 로드) ──────────────────────────
        if sym.upper() not in whitelist_upper:
            continue

        # ── 개별 심볼 필터 ────────────────────────────────────────────────
        if symbol_filter and sym.upper() != symbol_filter.upper():
            continue

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list) or len(data) < min_candles:
                logger.warning(
                    "데이터 부족 스킵 (%s): %d봉 < 최소 %d봉", sym, len(data), min_candles
                )
                continue
            result[sym] = data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("파일 로드 실패 (%s): %s", path.name, exc)

    if not result:
        logger.error(
            "로드된 심볼 없음. WHITELIST=%s | timeframe=%s | symbol_filter=%s",
            [s.replace("_KRW", "") for s in WHITELIST], timeframe, symbol_filter,
        )
        sys.exit(1)

    logger.info("캐시 로드 완료: %d개 메이저 심볼 (%s)", len(result), timeframe)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# 단일 심볼 백테스트
# ──────────────────────────────────────────────────────────────────────────────

def backtest_symbol(
    sym: str,
    ohlcv: list[list],
    tp_pct: float = TAKE_PROFIT_PCT,
    sl_pct: float = STOP_LOSS_PCT,
    rsi_max: float = RSI_ENTRY_MAX,
) -> list[dict[str, Any]]:
    """단일 심볼 OHLCV 데이터로 볼린저 핑퐁 전략 백테스트를 실행한다.

    [진입 조건]
      Close < 볼린저 밴드 하단 (Lower)  AND  RSI < rsi_max
      → 해당 봉 종가에 매수 체결

    [청산 조건]
      TP: 진입가 × (1 + tp_pct/100) 이상 고가 도달 → WIN
      SL: 진입가 × (1 - sl_pct/100) 이하 저가 도달 → LOSS
      동일 봉에서 TP·SL 동시 충족 시 WIN 우선 (상방 이동 먼저 가정)

    [포지션 겹침 방지]
      현재 포지션 청산 전까지 신규 진입 신호 무시 (per-symbol).

    Args:
        sym:     심볼 식별자 (예: "BTC_KRW")
        ohlcv:   [[ts_ms, open, high, low, close, volume], ...] (오름차순)
        tp_pct:  익절률 (%)
        sl_pct:  손절률 (%)
        rsi_max: 진입 허용 RSI 최댓값 (이 이상이면 진입 금지)

    Returns:
        거래 내역 리스트. 각 항목:
        {
            "symbol":       str,
            "entry_ts":     int,     # 진입 타임스탬프 (ms)
            "exit_ts":      int,     # 청산 타임스탬프 (ms)
            "entry_price":  float,
            "result":       "WIN" | "LOSS" | "TIMEOUT",
            "pnl_pct":      float,
            "candles_held": int,
        }
    """
    closes = [float(c[4]) for c in ohlcv]
    highs  = [float(c[2]) for c in ohlcv]
    lows   = [float(c[3]) for c in ohlcv]
    ts_arr = [int(c[0])   for c in ohlcv]

    # 볼린저 밴드 + RSI 계산
    bb_upper, bb_mid, bb_lower = calc_bollinger_bands(closes, BB_WINDOW, BB_STD_DEV)
    rsi_vals = calc_rsi(closes, RSI_PERIOD)

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
            tp_price = entry_price * (1 + tp_pct / 100)
            sl_price = entry_price * (1 - sl_pct / 100)

            # 우선순위 1: WIN — TP 도달 (상방 이동 먼저 가정)
            if h >= tp_price:
                trades.append({
                    "symbol":       sym,
                    "entry_ts":     entry_ts_val,
                    "exit_ts":      ts_arr[i],
                    "entry_price":  entry_price,
                    "result":       "WIN",
                    "pnl_pct":      tp_pct,
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
                    "pnl_pct":      -sl_pct,
                    "candles_held": i - entry_idx,
                })
                in_position = False
                continue

            # TP·SL 미도달 → 포지션 유지
            continue

        # ── [미보유] 진입 조건 확인 ──────────────────────────────────────
        lower_curr = bb_lower[i]
        rsi_curr   = rsi_vals[i]

        # 지표 미준비 구간 (워밍업) → 스킵
        if lower_curr is None or rsi_curr is None:
            continue

        close_curr = closes[i]

        # 조건 A: 현재 종가 < 볼린저 밴드 하단 (과매도 하단 이탈·터치)
        below_lower = close_curr < lower_curr

        # 조건 B: RSI < rsi_max (과매도 확인 — 고점 근처 하단 터치 제외)
        rsi_ok = rsi_curr < rsi_max

        if below_lower and rsi_ok:
            # 진입: 해당 봉 종가에 매수 체결
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
# 잔고 시뮬레이션 (SNIPER / BEAST)
# ──────────────────────────────────────────────────────────────────────────────

def simulate_balance(
    trades: list[dict],
    weight_pct: float,
    initial: float = INITIAL_BALANCE,
) -> tuple[float, float]:
    """거래 내역을 시간순으로 순회하며 최종 잔고와 MDD를 계산한다.

    매 거래마다 당시 잔고의 weight_pct% 를 투입하고
    pnl_pct에 따라 잔고를 갱신한다. 잔고가 0 이하이면 파산 처리한다.

    Args:
        trades:     entry_ts 기준 정렬된 거래 내역 리스트.
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
            break   # 파산
        invested = balance * weight_pct / 100
        pnl_krw  = invested * t["pnl_pct"] / 100
        balance += pnl_krw

        if balance > peak:
            peak = balance

        dd = (peak - balance) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    return round(balance, 2), round(max_dd, 2)


# ──────────────────────────────────────────────────────────────────────────────
# CSV 저장
# ──────────────────────────────────────────────────────────────────────────────

def save_csv(trades: list[dict], tp_pct: float, sl_pct: float) -> Path:
    """전체 거래 내역을 CSV로 저장한다.

    Args:
        trades: 전체 거래 내역 (entry_ts 오름차순).
        tp_pct: 익절률 (파일명 표기용).
        sl_pct: 손절률 (파일명 표기용).

    Returns:
        저장된 CSV 파일 경로.
    """
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    ts_str   = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = (
        RESULT_DIR
        / f"bollinger_backtest_tp{tp_pct:.0f}_sl{sl_pct:.0f}_{ts_str}.csv"
    )

    fieldnames = [
        "Symbol", "Entry_Time_KST", "Exit_Time_KST",
        "Entry_Price", "Result", "PnL_Pct", "Candles_Held",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for t in trades:
            entry_dt = datetime.fromtimestamp(
                t["entry_ts"] / 1000, tz=KST
            ).strftime("%Y-%m-%d %H:%M")
            exit_dt  = datetime.fromtimestamp(
                t["exit_ts"] / 1000, tz=KST
            ).strftime("%Y-%m-%d %H:%M")
            writer.writerow({
                "Symbol":         t["symbol"],
                "Entry_Time_KST": entry_dt,
                "Exit_Time_KST":  exit_dt,
                "Entry_Price":    round(t["entry_price"], 4),
                "Result":         t["result"],
                "PnL_Pct":        round(t["pnl_pct"], 4),
                "Candles_Held":   t["candles_held"],
            })

    return out_path


# ──────────────────────────────────────────────────────────────────────────────
# 터미널 출력
# ──────────────────────────────────────────────────────────────────────────────

def _pnl_color(pnl: float) -> str:
    """pnl 부호에 따른 ANSI 색상 코드를 반환한다."""
    return _G if pnl > 0 else (_R if pnl < 0 else "")


def print_summary(
    all_trades: list[dict],
    timeframe: str,
    tp_pct: float,
    sl_pct: float,
    rsi_max: float = RSI_ENTRY_MAX,
    candles_per_bar: int = 4,
) -> None:
    """전체 백테스트 결과를 터미널에 출력한다.

    Args:
        all_trades:      entry_ts 기준 정렬된 전체 거래 내역.
        timeframe:       타임프레임 문자열 (표기용).
        tp_pct:          익절률 (%).
        sl_pct:          손절률 (%).
        rsi_max:         진입 RSI 상한값 (표기용).
        candles_per_bar: 1봉당 실제 시간 (4h 봉 → 4).
    """
    total = len(all_trades)

    sep  = "─" * 64
    sep2 = "═" * 64

    print(f"\n{_C}{_B}{'=' * 64}{_RS}")
    print(f"{_C}{_B}  🌊 Major Coin Bollinger Ping-Pong v1{_RS}")
    print(f"{_C}{sep2}{_RS}")
    print(f"  타임프레임  : {timeframe}봉")
    print(f"  대상 코인   : 메이저 8종 ({', '.join(s.replace('_KRW','') for s in WHITELIST)})")
    print(f"  진입 조건   : BB{BB_WINDOW} 하단 터치 (StdDev {BB_STD_DEV:.1f}σ)  &  RSI < {rsi_max:.0f}")
    print(f"  손익비 설정 : TP +{tp_pct:.1f}%  |  SL -{sl_pct:.1f}%"
          f"  (R:R = {tp_pct / sl_pct:.2f}:1)")
    print(f"  초기 시드   : {INITIAL_BALANCE:,.0f} KRW")
    print(f"{_C}{sep}{_RS}")

    # ── 거래 없음 경고 ──────────────────────────────────────────────────────
    if total == 0:
        print(f"\n{_Y}  [경고] 체결된 거래 없음.{_RS}")
        print(f"  진입 조건(Close < BB하단, RSI < {rsi_max:.0f})을")
        print(f"  만족하는 구간이 현재 캐시 데이터에 없습니다.")
        print(f"  --rsi-max 를 높이거나 캐시 봉 수를 늘려보세요.\n")
        return

    # ── 집계 ────────────────────────────────────────────────────────────────
    wins      = sum(1 for t in all_trades if t["result"] == "WIN")
    losses    = sum(1 for t in all_trades if t["result"] == "LOSS")
    timeouts  = sum(1 for t in all_trades if t["result"] == "TIMEOUT")
    win_rate  = wins / total * 100
    avg_pnl   = sum(t["pnl_pct"] for t in all_trades) / total
    avg_hold  = sum(t["candles_held"] for t in all_trades) / total

    # 기대값 = (승률 × TP) - (패율 × SL)  (타임아웃은 별도 계산)
    # R:R 1:1이므로 승률이 50% 이상이면 양수 기대값
    loss_rate  = losses / total * 100
    expectancy = (win_rate / 100) * tp_pct - (loss_rate / 100) * sl_pct

    # ── SNIPER / BEAST 잔고 시뮬레이션 ────────────────────────────────────
    s_final, s_mdd = simulate_balance(all_trades, SNIPER_WEIGHT_PCT)
    b_final, b_mdd = simulate_balance(all_trades, BEAST_WEIGHT_PCT)
    s_roi   = (s_final - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    b_roi   = (b_final - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    s_sign  = "+" if s_roi >= 0 else ""
    b_sign  = "+" if b_roi >= 0 else ""

    # ── 심볼별 집계 ─────────────────────────────────────────────────────────
    sym_stats: dict[str, dict] = {}
    for t in all_trades:
        s = t["symbol"]
        if s not in sym_stats:
            sym_stats[s] = {"total": 0, "wins": 0, "pnl_sum": 0.0}
        sym_stats[s]["total"]   += 1
        sym_stats[s]["wins"]    += 1 if t["result"] == "WIN" else 0
        sym_stats[s]["pnl_sum"] += t["pnl_pct"]

    # ── 핵심 지표 출력 ──────────────────────────────────────────────────────
    # R:R 1:1이므로 손익분기 승률은 50%
    WIN_RATE_THRESHOLD = 50.0
    wc = _G if win_rate >= WIN_RATE_THRESHOLD else _R
    ac = _pnl_color(avg_pnl)
    ec = _pnl_color(expectancy)

    print(f"\n{_B}  [ 🎯 핵심 지표 ]{_RS}")
    print(f"  {'총 거래 횟수 :':<22} {total}회")
    print(f"  {'승 (WIN)     :':<22} {wins}회")
    print(f"  {'패 (LOSS)    :':<22} {losses}회")
    print(f"  {'타임아웃     :':<22} {timeouts}회")
    print(
        f"  {'승률         :':<22} {wc}{win_rate:.1f}%{_RS}"
        f"  {'✅ 수익 구조 (≥50%)' if win_rate >= WIN_RATE_THRESHOLD else '❌ 목표 미달 (<50%)'}"
    )
    print(f"  {'평균 수익률  :':<22} {ac}{avg_pnl:+.2f}%{_RS}")
    print(
        f"  {'기대값(EV)   :':<22} {ec}{expectancy:+.2f}%{_RS}"
        f"  ({'양수 → 장기 우위' if expectancy > 0 else '음수 → 장기 손실 구조'})"
    )
    print(
        f"  {'평균 보유    :':<22} {avg_hold:.1f}봉"
        f" ({avg_hold * candles_per_bar:.0f}시간 / "
        f"{avg_hold * candles_per_bar / 24:.1f}일)"
    )

    # ── SNIPER / BEAST 비교 ──────────────────────────────────────────────────
    print(f"\n{_B}  [ 🛡️ SNIPER vs 🔥 BEAST 가상 시드 비교 ]{_RS}")
    print(f"  {'초기 시드    :':<22} {INITIAL_BALANCE:,.0f} KRW")
    sc = _G if s_roi >= 0 else _R
    bc = _G if b_roi >= 0 else _R
    print(
        f"  🛡️ SNIPER ({SNIPER_WEIGHT_PCT:.0f}%)  "
        f"{INITIAL_BALANCE:>12,.0f} → {s_final:>12,.0f} KRW"
        f"  ({sc}{s_sign}{s_roi:.2f}%{_RS}  MDD -{s_mdd:.1f}%)"
    )
    print(
        f"  🔥 BEAST  ({BEAST_WEIGHT_PCT:.0f}%)  "
        f"{INITIAL_BALANCE:>12,.0f} → {b_final:>12,.0f} KRW"
        f"  ({bc}{b_sign}{b_roi:.2f}%{_RS}  MDD -{b_mdd:.1f}%)"
    )

    # ── 심볼별 상세 ────────────────────────────────────────────────────────
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
# CLI 진입점
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="메이저 코인 볼린저 밴드 핑퐁 전략 백테스트 (BB 하단 터치 & RSI < 40)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--symbol",
        default=None,
        help="특정 심볼만 테스트 (예: BTC_KRW). 미지정 시 WHITELIST 전체",
    )
    parser.add_argument(
        "--timeframe",
        default="4h",
        help="캐시 타임프레임 (파일명 접미사 기준, 예: 4h)",
    )
    parser.add_argument(
        "--tp",
        type=float,
        default=TAKE_PROFIT_PCT,
        help="익절률 (%%)",
    )
    parser.add_argument(
        "--sl",
        type=float,
        default=STOP_LOSS_PCT,
        help="손절률 (%%)",
    )
    parser.add_argument(
        "--rsi-max",
        type=float,
        default=RSI_ENTRY_MAX,
        help="진입 허용 RSI 최댓값 (이 이상이면 진입 금지)",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        default=False,
        help="전체 거래 내역을 .result/ 디렉터리에 CSV로 저장",
    )
    args = parser.parse_args()

    tp_pct  = args.tp
    sl_pct  = args.sl
    rsi_max = args.rsi_max

    logger.info(
        "[볼린저 핑퐁 v1] TP +%.1f%%  SL -%.1f%%  RSI < %.0f  BB(%d, %.1fσ)  대상 %d개",
        tp_pct, sl_pct, rsi_max, BB_WINDOW, BB_STD_DEV, len(WHITELIST),
    )

    # ── [1단계] 캐시 로드 (WHITELIST 전용) ──────────────────────────────────
    ohlcv_map = load_ohlcv_files(timeframe=args.timeframe, symbol_filter=args.symbol)

    # ── [2단계] 심볼별 백테스트 실행 ────────────────────────────────────────
    all_trades: list[dict] = []
    for sym, ohlcv in sorted(ohlcv_map.items()):
        trades = backtest_symbol(
            sym, ohlcv,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            rsi_max=rsi_max,
        )
        logger.info(
            "  %-14s | %3d봉 | 거래 %2d회 (WIN %d / LOSS %d / TO %d)",
            sym, len(ohlcv), len(trades),
            sum(1 for t in trades if t["result"] == "WIN"),
            sum(1 for t in trades if t["result"] == "LOSS"),
            sum(1 for t in trades if t["result"] == "TIMEOUT"),
        )
        all_trades.extend(trades)

    # 시간순 정렬 (SNIPER/BEAST 잔고 시뮬레이션 누적 정확도 보장)
    all_trades.sort(key=lambda t: t["entry_ts"])

    # ── [3단계] 결과 출력 ────────────────────────────────────────────────────
    candles_per_bar = (
        int(args.timeframe.replace("h", "")) if args.timeframe.endswith("h") else 1
    )
    print_summary(
        all_trades,
        args.timeframe,
        tp_pct,
        sl_pct,
        rsi_max=rsi_max,
        candles_per_bar=candles_per_bar,
    )

    # ── [4단계] CSV 저장 (옵션) ──────────────────────────────────────────────
    if args.csv and all_trades:
        csv_path = save_csv(all_trades, tp_pct, sl_pct)
        print(f"  💾 CSV 저장 완료: {csv_path}\n")


if __name__ == "__main__":
    main()
