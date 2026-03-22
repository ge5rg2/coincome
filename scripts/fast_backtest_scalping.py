"""
fast_backtest_scalping.py — 로컬 OHLCV 캐시 기반 초고속 단타 전략 백테스트.

LLM · 업비트 API 호출 없이 .cache/ohlcv/ JSON 파일만 읽어
순수 수학 연산으로 1h 봉 기반 단타 모멘텀(Scalping Momentum) 전략을 검증한다.

[전략 — 1h Scalping Momentum v1: 단기 모멘텀 포착]
  진입: Close > MA20  AND  RSI 60~75
        ─ MA20: 단기 상승 돌파 확인 (MA50 제거 — 1h 봉 워밍업 단축)
        ─ RSI 60~75: 강한 상승 모멘텀 진입 구간 (과매수 직전, 추세 형성 초입)
  블랙리스트: BLACKLIST 등록 코인 자동 제외
              (BTC/ETH/XRP 등 휩쏘 취약 메이저 코인 배제 → 알트코인 집중)
  익절: 진입가 대비 +TAKE_PROFIT_PCT% 도달 (기본 +2.0%, R:R 1.33:1)
  손절: 진입가 대비 -STOP_LOSS_PCT%  도달 (기본 -1.5%)
  우선순위: WIN > LOSS (동일 봉에서 TP·SL 동시 충족 시 WIN 적용)
  겹침 방지: 현재 포지션 청산 전까지 신규 진입 신호 무시 (per-symbol)

[v1 설계 의도]
  4h 봉 스나이퍼 전략(fast_backtest.py)의 단타 버전.
  MA50 없이 MA20 단독 + RSI 60~75 고강도 모멘텀 필터로 단기 급등 초입 포착.
  TP 2% / SL 1.5% 좁은 폭으로 빠른 회전율 추구 (승률 ≥45% 목표).

[의존성]
  표준 라이브러리만 사용 (pandas / requests 불필요)

실행 예시:
  python scripts/fast_backtest_scalping.py
  python scripts/fast_backtest_scalping.py --symbol LINK_KRW
  python scripts/fast_backtest_scalping.py --tp 2.0 --sl 1.5
  python scripts/fast_backtest_scalping.py --timeframe 1h --csv
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ──────────────────────────────────────────────────────────────────────────────
# ★ 전략 파라미터 — 여기를 수정해 전략을 튜닝하세요
# ──────────────────────────────────────────────────────────────────────────────
TAKE_PROFIT_PCT: float = 2.0   # 익절률 (%) — 단타 빠른 익절 (R:R 1.33:1)
STOP_LOSS_PCT:   float = 1.5   # 손절률 (%) — 좁은 손절로 리스크 최소화

# RSI 60~75: 강한 상승 모멘텀 초입 포착 (중립 구간 제외, 과매수 진입 금지)
RSI_ENTRY_MIN:   float = 60.0  # 진입 허용 RSI 최솟값
RSI_ENTRY_MAX:   float = 75.0  # 진입 허용 RSI 최댓값

MA_PERIOD:  int = 20   # 단기 이동평균선 기간 (봉) — Close>MA20 돌파 확인
RSI_PERIOD: int = 14   # RSI 계산 기간 (봉)

# 모드별 투입 비중
SNIPER_WEIGHT_PCT: float = 20.0       # 🛡️ SNIPER — 잔고의 20% 투입
BEAST_WEIGHT_PCT:  float = 70.0       # 🔥 BEAST  — 잔고의 70% 투입
INITIAL_BALANCE:   float = 1_000_000  # 초기 시드 (KRW)

# 심볼별 요약: 최소 이 거래 횟수 이상만 표시
MIN_TRADES_FOR_DETAIL: int = 3

# 메이저 코인 블랙리스트 — 휩쏘 취약성 검증으로 제외 (알트코인 집중 전략)
BLACKLIST: list[str] = [
    "BTC_KRW",   # 메이저 코인 — 대형 매물대 휩쏘 빈발
    "ETH_KRW",   # 메이저 코인 — 고변동성 추세 추종 취약
    "XRP_KRW",   # 뉴스·고래 매도 휩쏘 빈발
    "DOGE_KRW",  # 밈코인 — 모멘텀 지속성 낮음
    "ADA_KRW",   # 무거운 메이저 알트 — 돌파 실패율 높음
    "SOL_KRW",   # 고변동성 L1 — 목표가 도달 전 되돌림 잦음
    "SUI_KRW",   # 신흥 L1 — 매물대 취약
    "PEPE_KRW",  # 고변동성 밈코인 — 예측 불가 급등락
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


# ──────────────────────────────────────────────────────────────────────────────
# 지표 계산 — 순수 Python (외부 라이브러리 불필요)
# ──────────────────────────────────────────────────────────────────────────────

def calc_ma(closes: list[float], period: int = MA_PERIOD) -> list[float | None]:
    """단순 이동평균(SMA)을 계산한다.

    워밍업 구간(period - 1개 미만) 은 None으로 채워 미준비 상태를 명시한다.

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


def calc_rsi(closes: list[float], period: int = RSI_PERIOD) -> list[float | None]:
    """Wilder's Smoothed RSI(상대강도지수)를 계산한다.

    초기 avg_gain / avg_loss 는 단순 평균으로 시드(seed)하고,
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

    # ── [1] 초기 시드: 처음 period 개 변화량으로 단순 평균 계산 ─────────────
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

    # ── [2] Wilder's Smoothing 적용 (index = period+1 ~ 끝) ─────────────────
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = diff       if diff > 0 else 0.0
        loss = abs(diff)  if diff < 0 else 0.0

        # 지수 가중 평균 갱신
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0:
            result[i] = 100.0
        else:
            result[i] = 100 - 100 / (1 + avg_gain / avg_loss)

    return result


# ──────────────────────────────────────────────────────────────────────────────
# 데이터 로더
# ──────────────────────────────────────────────────────────────────────────────

def load_ohlcv_files(
    timeframe: str = "1h",
    symbol_filter: str | None = None,
) -> dict[str, list[list]]:
    """CACHE_DIR 내 JSON 파일들을 로드해 심볼별 OHLCV 딕셔너리로 반환한다.

    파일 포맷: [[timestamp_ms, open, high, low, close, volume], ...] (오름차순)

    Args:
        timeframe:     타임프레임 접미사 필터 (예: "1h" → *_1h.json 만 로드).
        symbol_filter: 특정 심볼만 로드 (예: "LINK_KRW"). None 이면 전체 로드.

    Returns:
        {"LINK_KRW": [[ts, o, h, l, c, v], ...], ...}
    """
    if not CACHE_DIR.exists():
        logger.error("캐시 디렉터리 없음: %s", CACHE_DIR)
        logger.error("먼저 `python scripts/backtester.py` 를 실행해 캐시를 생성하세요.")
        sys.exit(1)

    files = sorted(CACHE_DIR.glob(f"*_{timeframe}.json"))
    if not files:
        logger.error("캐시 파일 없음 (패턴: *_%s.json)", timeframe)
        sys.exit(1)

    # 최소 데이터 요건: MA20 워밍업 + RSI 워밍업 + 여유
    min_candles = MA_PERIOD + RSI_PERIOD + 5

    result: dict[str, list[list]] = {}
    for path in files:
        # 심볼 추출: "LINK_KRW_1h.json" → "LINK_KRW"
        sym = path.stem[: -(len(timeframe) + 1)]    # 접미사 "_1h" 제거
        if symbol_filter and sym.upper() != symbol_filter.upper():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list) or len(data) < min_candles:
                logger.warning("데이터 부족 스킵 (%s): %d봉 < 최소 %d봉", sym, len(data), min_candles)
                continue
            result[sym] = data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("파일 로드 실패 (%s): %s", path.name, exc)

    if not result:
        logger.error("로드된 심볼 없음. symbol_filter=%s, timeframe=%s", symbol_filter, timeframe)
        sys.exit(1)

    logger.info("캐시 로드 완료: %d개 심볼 (%s)", len(result), timeframe)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# 단일 심볼 백테스트
# ──────────────────────────────────────────────────────────────────────────────

def backtest_symbol(
    sym: str,
    ohlcv: list[list],
    tp_pct: float = TAKE_PROFIT_PCT,
    sl_pct: float = STOP_LOSS_PCT,
    rsi_min: float = RSI_ENTRY_MIN,
    rsi_max: float = RSI_ENTRY_MAX,
) -> list[dict[str, Any]]:
    """단일 심볼 OHLCV 데이터로 1h 단타 모멘텀 전략 백테스트를 실행한다.

    [포지션 겹침 방지]
    현재 포지션이 청산(WIN/LOSS/TIMEOUT)되기 전까지 신규 진입 신호를 무시한다.

    [청산 우선순위 — 동일 봉에서 TP·SL 동시 충족 시]
    실제 거래에서 상방 이동이 먼저 발생한다고 가정해 WIN 우선 적용.

    Args:
        sym:    심볼 식별자 (예: "LINK_KRW")
        ohlcv:  [[ts_ms, open, high, low, close, volume], ...] (오름차순)
        tp_pct: 익절률 (%)
        sl_pct: 손절률 (%)
        rsi_min: 진입 허용 RSI 최솟값
        rsi_max: 진입 허용 RSI 최댓값

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

    ma_vals  = calc_ma(closes,  MA_PERIOD)
    rsi_vals = calc_rsi(closes, RSI_PERIOD)

    trades: list[dict[str, Any]] = []
    in_position  = False
    entry_price  = 0.0
    entry_ts_val = 0
    entry_idx    = 0

    for i in range(1, len(ohlcv)):

        # ── [보유 중] TP / SL 청산 조건 확인 ─────────────────────────────────
        if in_position:
            h        = highs[i]
            l        = lows[i]
            tp_price = entry_price * (1 + tp_pct / 100)
            sl_price = entry_price * (1 - sl_pct / 100)

            # 우선순위 1: WIN — TP 도달
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

            # TP·SL 미도달 → 포지션 유지. 신규 진입 체크는 생략
            continue

        # ── [미보유] 진입 조건 확인 ──────────────────────────────────────────
        ma_curr  = ma_vals[i]
        rsi_curr = rsi_vals[i]

        # 지표 미준비 구간 (워밍업) → 스킵
        if ma_curr is None or rsi_curr is None:
            continue

        close_curr = closes[i]

        # 조건 A: 현재 종가 > MA20  (단기 상승 돌파 확인)
        above_ma = close_curr > ma_curr

        # 조건 B: RSI 진입 범위  (강한 상승 모멘텀, 과매수 진입 금지)
        rsi_ok = rsi_min <= rsi_curr <= rsi_max

        if above_ma and rsi_ok:
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

        # 최고점 갱신
        if balance > peak:
            peak = balance

        # 현재 낙폭 계산
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
    out_path = RESULT_DIR / f"fast_backtest_scalping_tp{tp_pct:.1f}_sl{sl_pct:.1f}_{ts_str}.csv"

    fieldnames = [
        "Symbol", "Entry_Time_KST", "Exit_Time_KST",
        "Entry_Price", "Result", "PnL_Pct", "Candles_Held",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for t in trades:
            entry_dt = datetime.fromtimestamp(t["entry_ts"] / 1000, tz=KST).strftime("%Y-%m-%d %H:%M")
            exit_dt  = datetime.fromtimestamp(t["exit_ts"]  / 1000, tz=KST).strftime("%Y-%m-%d %H:%M")
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
    rsi_min: float = RSI_ENTRY_MIN,
    rsi_max: float = RSI_ENTRY_MAX,
    candles_per_bar: int = 1,
    label: str = "",
) -> None:
    """전체 백테스트 결과를 터미널에 출력한다.

    Args:
        all_trades:      entry_ts 기준 정렬된 전체 거래 내역.
        timeframe:       타임프레임 문자열 (표기용).
        tp_pct:          익절률 (%).
        sl_pct:          손절률 (%).
        candles_per_bar: 1봉당 실제 시간 (1h 봉 → 1).
        label:           헤더에 추가할 부가 레이블 (예: " (블랙리스트 제외)").
    """
    total = len(all_trades)

    sep  = "─" * 64
    sep2 = "═" * 64

    print(f"\n{_C}{_B}{'=' * 64}{_RS}")
    print(f"{_C}{_B}  ⚡ 1h Scalping Momentum v1{label}{_RS}")
    print(f"{_C}{sep2}{_RS}")
    print(f"  타임프레임  : {timeframe}봉")
    print(f"  진입 파라미터: MA{MA_PERIOD} (단기 돌파 확인)  |  RSI {rsi_min:.0f}~{rsi_max:.0f}")
    print(f"  손익비 설정 : TP +{tp_pct:.1f}%  |  SL -{sl_pct:.1f}%"
          f"  (R:R = {tp_pct / sl_pct:.2f}:1)")
    print(f"  초기 시드   : {INITIAL_BALANCE:,.0f} KRW")
    print(f"{_C}{sep}{_RS}")

    # ── 거래 없음 경고 ─────────────────────────────────────────────────────
    if total == 0:
        print(f"\n{_Y}  [경고] 체결된 거래 없음.{_RS}")
        print(f"  진입 조건(RSI {rsi_min:.0f}~{rsi_max:.0f}, Close>MA{MA_PERIOD})을")
        print(f"  만족하는 구간이 현재 캐시 데이터에 없습니다.")
        print(f"  --rsi-min / --rsi-max 를 조정하거나 캐시 봉 수를 늘려보세요.\n")
        return

    # ── 집계 ──────────────────────────────────────────────────────────────
    wins      = sum(1 for t in all_trades if t["result"] == "WIN")
    losses    = sum(1 for t in all_trades if t["result"] == "LOSS")
    timeouts  = sum(1 for t in all_trades if t["result"] == "TIMEOUT")
    win_rate  = wins / total * 100
    avg_pnl   = sum(t["pnl_pct"] for t in all_trades) / total
    avg_hold  = sum(t["candles_held"] for t in all_trades) / total

    # 기대값 = (승률 × TP) - (패율 × SL)  (타임아웃은 별도 계산)
    loss_rate  = losses / total * 100
    expectancy = (win_rate / 100) * tp_pct - (loss_rate / 100) * sl_pct

    # ── SNIPER / BEAST 잔고 시뮬레이션 ────────────────────────────────────
    s_final, s_mdd = simulate_balance(all_trades, SNIPER_WEIGHT_PCT)
    b_final, b_mdd = simulate_balance(all_trades, BEAST_WEIGHT_PCT)
    s_roi   = (s_final - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    b_roi   = (b_final - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    s_sign  = "+" if s_roi >= 0 else ""
    b_sign  = "+" if b_roi >= 0 else ""

    # ── 심볼별 집계 ─────────────────────────────────────────────────────
    sym_stats: dict[str, dict] = {}
    for t in all_trades:
        s = t["symbol"]
        if s not in sym_stats:
            sym_stats[s] = {"total": 0, "wins": 0, "pnl_sum": 0.0}
        sym_stats[s]["total"]   += 1
        sym_stats[s]["wins"]    += 1 if t["result"] == "WIN" else 0
        sym_stats[s]["pnl_sum"] += t["pnl_pct"]

    # ── 핵심 지표 출력 ─────────────────────────────────────────────────────
    wc = _G if win_rate >= 45 else _R
    ac = _pnl_color(avg_pnl)
    ec = _pnl_color(expectancy)

    print(f"\n{_B}  [ 🎯 핵심 지표 ]{_RS}")
    print(f"  {'총 거래 횟수 :':<22} {total}회")
    print(f"  {'승 (WIN)     :':<22} {wins}회")
    print(f"  {'패 (LOSS)    :':<22} {losses}회")
    print(f"  {'타임아웃     :':<22} {timeouts}회")
    print(
        f"  {'승률         :':<22} {wc}{win_rate:.1f}%{_RS}"
        f"  {'✅ 목표 달성 (≥45%)' if win_rate >= 45 else '❌ 목표 미달 (<45%)'}"
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

    # ── SNIPER / BEAST 비교 ───────────────────────────────────────────────
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

    # ── 심볼별 상세 ─────────────────────────────────────────────────────────
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
# 블랙리스트 필터 효과 비교 출력
# ──────────────────────────────────────────────────────────────────────────────

def print_comparison(
    full_trades: list[dict],
    clean_trades: list[dict],
    blacklist_syms: list[str],
) -> None:
    """블랙리스트 적용 전후 핵심 지표 비교표를 터미널에 출력한다.

    전체 결과(블랙리스트 포함)와 정제 결과(블랙리스트 제외)의
    승률·MDD·ROI를 나란히 표시해 필터 효과를 한눈에 파악할 수 있게 한다.

    Args:
        full_trades:    블랙리스트 포함 전체 거래 내역 (entry_ts 오름차순).
        clean_trades:   블랙리스트 제외 정제 거래 내역 (entry_ts 오름차순).
        blacklist_syms: 실제 캐시에서 발견된 블랙리스트 심볼 목록.
    """
    if not full_trades:
        return

    sep  = "─" * 64
    sep2 = "═" * 64

    # ── 전체(블랙리스트 포함) 통계 계산 ──────────────────────────────────
    f_total         = len(full_trades)
    f_wins          = sum(1 for t in full_trades if t["result"] == "WIN")
    f_wr            = f_wins / f_total * 100
    f_s_final, f_s_mdd = simulate_balance(full_trades, SNIPER_WEIGHT_PCT)
    f_b_final, f_b_mdd = simulate_balance(full_trades, BEAST_WEIGHT_PCT)
    f_s_roi         = (f_s_final - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    f_b_roi         = (f_b_final - INITIAL_BALANCE) / INITIAL_BALANCE * 100

    # ── 정제(블랙리스트 제외) 통계 계산 ──────────────────────────────────
    c_total         = len(clean_trades)
    c_wins          = sum(1 for t in clean_trades if t["result"] == "WIN")
    c_wr            = c_wins / c_total * 100 if c_total else 0.0
    c_s_final, c_s_mdd = simulate_balance(clean_trades, SNIPER_WEIGHT_PCT)
    c_b_final, c_b_mdd = simulate_balance(clean_trades, BEAST_WEIGHT_PCT)
    c_s_roi         = (c_s_final - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    c_b_roi         = (c_b_final - INITIAL_BALANCE) / INITIAL_BALANCE * 100

    print(f"\n{_C}{_B}{'=' * 64}{_RS}")
    print(f"{_C}{_B}  🚫 블랙리스트 필터 효과 비교 ({len(blacklist_syms)}개 제외){_RS}")
    print(f"{_C}{sep2}{_RS}")
    print(f"  제외 심볼 : {', '.join(blacklist_syms)}")
    print(f"  {sep}")
    print(
        f"  {'항목':<20} {'블랙리스트 포함':>13} {'블랙리스트 제외':>13}  {'변화':>7}"
    )
    print(f"  {'─' * 60}")

    # ── 총 거래 횟수 ──────────────────────────────────────────────────────
    trade_delta = c_total - f_total   # 음수 (줄어든 거래)
    print(
        f"  {'총 거래 횟수':<20} {f_total:>11}회 {c_total:>11}회  "
        f"{_Y}{trade_delta:>+6}회{_RS}"
    )

    # ── 승률 ──────────────────────────────────────────────────────────────
    wr_delta = c_wr - f_wr
    wr_c = _G if wr_delta >= 0 else _R
    wr_arrow = "▲" if wr_delta > 0 else ("▼" if wr_delta < 0 else "─")
    print(
        f"  {'승률':<21} {f_wr:>11.1f}% {c_wr:>11.1f}%  "
        f"{wr_c}{wr_arrow}{abs(wr_delta):.1f}%p{_RS}"
    )

    # ── SNIPER MDD (감소할수록 개선 — 부호 반전) ──────────────────────────
    s_mdd_delta = f_s_mdd - c_s_mdd   # 양수 = 개선 (MDD 감소)
    s_mdd_c = _G if s_mdd_delta >= 0 else _R
    s_mdd_arrow = "▼" if s_mdd_delta >= 0 else "▲"   # ▼ MDD 감소 = 좋음
    print(
        f"  {'🛡️ SNIPER MDD':<20} {-f_s_mdd:>11.1f}% {-c_s_mdd:>11.1f}%  "
        f"{s_mdd_c}{s_mdd_arrow}{abs(s_mdd_delta):.1f}%p{_RS}"
    )

    # ── BEAST MDD ─────────────────────────────────────────────────────────
    b_mdd_delta = f_b_mdd - c_b_mdd   # 양수 = 개선 (MDD 감소)
    b_mdd_c = _G if b_mdd_delta >= 0 else _R
    b_mdd_arrow = "▼" if b_mdd_delta >= 0 else "▲"
    print(
        f"  {'🔥 BEAST MDD':<20} {-f_b_mdd:>11.1f}% {-c_b_mdd:>11.1f}%  "
        f"{b_mdd_c}{b_mdd_arrow}{abs(b_mdd_delta):.1f}%p{_RS}"
    )

    # ── SNIPER ROI ────────────────────────────────────────────────────────
    s_roi_delta = c_s_roi - f_s_roi
    s_roi_c = _G if s_roi_delta >= 0 else _R
    s_roi_arrow = "▲" if s_roi_delta >= 0 else "▼"
    f_s_sign = "+" if f_s_roi >= 0 else ""
    c_s_sign = "+" if c_s_roi >= 0 else ""
    print(
        f"  {'🛡️ SNIPER ROI':<20} {f_s_sign}{f_s_roi:>10.2f}% {c_s_sign}{c_s_roi:>10.2f}%  "
        f"{s_roi_c}{s_roi_arrow}{abs(s_roi_delta):.2f}%p{_RS}"
    )

    # ── BEAST ROI ─────────────────────────────────────────────────────────
    b_roi_delta = c_b_roi - f_b_roi
    b_roi_c = _G if b_roi_delta >= 0 else _R
    b_roi_arrow = "▲" if b_roi_delta >= 0 else "▼"
    f_b_sign = "+" if f_b_roi >= 0 else ""
    c_b_sign = "+" if c_b_roi >= 0 else ""
    print(
        f"  {'🔥 BEAST ROI':<20} {f_b_sign}{f_b_roi:>10.2f}% {c_b_sign}{c_b_roi:>10.2f}%  "
        f"{b_roi_c}{b_roi_arrow}{abs(b_roi_delta):.2f}%p{_RS}"
    )

    print(f"\n{_C}{sep2}{_RS}\n")


# ──────────────────────────────────────────────────────────────────────────────
# CLI 진입점
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="로컬 OHLCV 캐시 기반 초고속 1h 단타 전략 백테스트 (Scalping Momentum)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--symbol",
        default=None,
        help="특정 심볼만 테스트 (예: LINK_KRW). 미지정 시 전체 심볼",
    )
    parser.add_argument(
        "--timeframe",
        default="1h",
        help="캐시 타임프레임 (파일명 접미사 기준, 예: 1h)",
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
        "--rsi-min",
        type=float,
        default=RSI_ENTRY_MIN,
        help="진입 허용 RSI 최솟값",
    )
    parser.add_argument(
        "--rsi-max",
        type=float,
        default=RSI_ENTRY_MAX,
        help="진입 허용 RSI 최댓값",
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
    rsi_min = args.rsi_min
    rsi_max = args.rsi_max

    # ── [1단계] 캐시 로드 ─────────────────────────────────────────────────
    ohlcv_map = load_ohlcv_files(timeframe=args.timeframe, symbol_filter=args.symbol)

    # ── [2단계] 블랙리스트 식별 ───────────────────────────────────────────
    blacklist_upper = {s.upper() for s in BLACKLIST}
    found_blacklist = sorted(
        s for s in ohlcv_map if s.upper() in blacklist_upper
    )
    if found_blacklist:
        logger.info(
            "[블랙리스트] 제외 대상 %d개: %s",
            len(found_blacklist), ", ".join(found_blacklist),
        )

    logger.info(
        "[전략 v1] TP +%.1f%%  SL -%.1f%%  RSI %.0f~%.0f  MA%d  블랙리스트 %d개",
        tp_pct, sl_pct, rsi_min, rsi_max, MA_PERIOD, len(found_blacklist),
    )

    # ── [3단계] 심볼별 백테스트 실행 (전체 — 비교 데이터 수집) ────────────
    all_trades: list[dict] = []
    for sym, ohlcv in sorted(ohlcv_map.items()):
        is_bl = sym.upper() in blacklist_upper
        trades = backtest_symbol(
            sym, ohlcv, tp_pct=tp_pct, sl_pct=sl_pct,
            rsi_min=rsi_min, rsi_max=rsi_max,
        )
        tag = " 🚫[BL]" if is_bl else ""
        logger.info(
            "  %-14s | %3d봉 | 거래 %2d회 (WIN %d / LOSS %d / TO %d)%s",
            sym, len(ohlcv), len(trades),
            sum(1 for t in trades if t["result"] == "WIN"),
            sum(1 for t in trades if t["result"] == "LOSS"),
            sum(1 for t in trades if t["result"] == "TIMEOUT"),
            tag,
        )
        all_trades.extend(trades)

    # 시간순 정렬
    all_trades.sort(key=lambda t: t["entry_ts"])

    # 블랙리스트 제외 정제 거래 내역
    clean_trades = [
        t for t in all_trades if t["symbol"].upper() not in blacklist_upper
    ]

    # ── [4단계] 결과 출력 ──────────────────────────────────────────────────
    candles_per_bar = int(args.timeframe.replace("h", "")) if args.timeframe.endswith("h") else 1

    # 메인 결과: 블랙리스트 제외 정제 결과 출력
    print_summary(
        clean_trades, args.timeframe, tp_pct, sl_pct,
        rsi_min=rsi_min, rsi_max=rsi_max,
        candles_per_bar=candles_per_bar,
        label=" (블랙리스트 제외)",
    )

    # 블랙리스트 필터 효과 비교표 (before vs after)
    if found_blacklist:
        print_comparison(all_trades, clean_trades, found_blacklist)

    # ── [5단계] CSV 저장 (옵션) — 블랙리스트 제외 정제 데이터 기준 ──────────
    if args.csv and clean_trades:
        csv_path = save_csv(clean_trades, tp_pct, sl_pct)
        print(f"  💾 CSV 저장 완료 (블랙리스트 제외): {csv_path}\n")


if __name__ == "__main__":
    main()
