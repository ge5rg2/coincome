#!/usr/bin/env python3
"""
scripts/backtester.py — AI 퀀트 매니저 백테스팅 파이프라인.

3가지 AI 모델(OpenAI / Anthropic / Gemini)의 종목 선택 성능을 검증한다.

전략: SCALPING (1h 봉 기준, app/services/ai_trader.py 동일 프롬프트 사용)
데이터: 최근 7일 BTC/ETH/SOL/XRP/DOGE (업비트 KRW 마켓, CCXT fetch_ohlcv)

동작 흐름:
  1. CCXT(upbit)로 각 심볼의 1h OHLCV 데이터를 수집 (최근 ~300봉)
  2. 슬라이딩 윈도우(60봉)로 ATR/RSI/MA 지표 계산
  3. 4시간마다 LLM에게 시장 분석 요청 → score≥80 픽 추출
  4. 픽된 코인: 다음 봉 시가(open)로 진입 → 익절/손절 도달 또는 최대 48시간 보유 후 청산
  5. 콘솔 요약 출력 + backtest_results.csv Append 모드 저장

Usage:
    python scripts/backtester.py --model openai
    python scripts/backtester.py --model anthropic
    python scripts/backtester.py --model gemini
    python scripts/backtester.py --model all     # 3개 모델 순차 실행

Environment Variables:
    OPENAI_API_KEY    — OpenAI API 키 (OpenAI 어댑터용)
    ANTHROPIC_API_KEY — Anthropic API 키 (Anthropic 어댑터용)
    GEMINI_API_KEY    — Google Gemini API 키 (Gemini 어댑터용)

Models Used:
    OpenAI    : gpt-4.1-mini   (2026 최신 경량 모델)
    Anthropic : claude-sonnet-4-6  (2026.02 최신 밸런스 모델)
    Gemini    : gemini-2.5-flash   (2026 최신 안정 고속 모델)
"""

from __future__ import annotations

import abc
import argparse
import asyncio
import csv
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from dotenv import load_dotenv


import ccxt
import pandas as pd

# ── 로깅 설정 ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backtester")
load_dotenv()


# ──────────────────────────────────────────────────────────────────────────
# 백테스트 파라미터
# ──────────────────────────────────────────────────────────────────────────

BACKTEST_SYMBOLS = ["BTC/KRW", "ETH/KRW", "SOL/KRW", "XRP/KRW", "DOGE/KRW"]

ANALYSIS_WINDOW = 60    # 지표 계산용 최소 봉 수 (RSI14 + MA20 워밍업)
STEP_HOURS      = 4     # 분석 사이클 간격 (1h 봉 기준)
MAX_HOLD_HOURS  = 48    # 최대 보유 시간 (봉 수)
FETCH_LIMIT     = 300   # 수집할 1h 봉 수 (분석 기간 + 워밍업 + 여유)

MIN_PRICE       = 100.0         # 동전주 하드 필터 — 100 KRW 미만 제외
CCXT_SLEEP      = 0.8           # 심볼별 CCXT 요청 간 딜레이 (초)
LLM_SLEEP       = 1.0           # LLM 호출 간 딜레이 (Rate-Limit 방어, 초)
VIRTUAL_KRW     = 1_000_000.0   # 가상 가용 예산 (KRW)

# 프로젝트 루트 기준으로 CSV 경로 지정
CSV_FILE = Path(__file__).parent.parent / "backtest_results.csv"
CSV_FIELDS = [
    "run_at",
    "model_name",
    "model_id",
    "signal_ts",
    "symbol",
    "score",
    "weight_pct",
    "entry_price",
    "target_pct",
    "stop_pct",
    "exit_price",
    "profit_pct",
    "result",
    "hold_hours",
    "reason",
]


# ──────────────────────────────────────────────────────────────────────────
# 지표 계산 함수 — app/services/market_data.py 동일 로직
# ──────────────────────────────────────────────────────────────────────────


def _calc_rsi(close: pd.Series, period: int = 14) -> float | None:
    """RSI(Relative Strength Index) 계산 후 마지막 값 반환.

    Args:
        close:  종가 Series.
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
    rsi   = 100 - (100 / (1 + rs))
    val   = rsi.iloc[-1]
    return float(val) if pd.notna(val) else None


def _calc_ma(close: pd.Series, period: int = 20) -> float | None:
    """단순 이동평균(MA) 계산 후 마지막 값 반환.

    Args:
        close:  종가 Series.
        period: MA 기간 (기본 20).

    Returns:
        마지막 봉의 MA 값, 또는 데이터 부족 시 None.
    """
    if len(close) < period:
        return None
    ma  = close.rolling(period).mean()
    val = ma.iloc[-1]
    return float(val) if pd.notna(val) else None


def _calc_atr(df: pd.DataFrame, period: int = 14) -> float | None:
    """ATR(Average True Range) 계산 후 마지막 값 반환.

    True Range = max(H-L, |H-prev_C|, |L-prev_C|)
    ATR = TR의 period기간 단순 이동평균.

    Args:
        df:     OHLCV DataFrame (timestamp, open, high, low, close, volume 컬럼).
        period: ATR 기간 (기본 14).

    Returns:
        마지막 봉의 ATR 값, 또는 데이터 부족 시 None.
    """
    if len(df) < period + 1:
        return None
    high       = df["high"]
    low        = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period).mean()
    val = atr.iloc[-1]
    return float(val) if pd.notna(val) else None


# ──────────────────────────────────────────────────────────────────────────
# AI 분석용 시스템 프롬프트 (ai_trader.py _SCALPING_SYSTEM_PROMPT 동일)
# ──────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
너는 1시간 봉 스캘핑·단타 전문 AI 퀀트 트레이더야.
보수적인 접근은 버려라. 작은 모멘텀이라도 포착되면 즉각 매수를 실행하는 것이 너의 역할이다.
단, 고정된 손절가(1.5%)는 버려라. 변동성(ATR%)에 맞는 동적 손절이 휩쏘를 방지하는 유일한 방법이다.

제공된 코인의 1h 지표, 변동성(ATR%), 24h 거래대금, 가용 예산을 분석해서
지금 당장 모멘텀 돌파 진입하기 좋은 코인을 최대 2개만 골라.
이미 유저가 보유 중인 코인은 반드시 제외해.
※ 이번 분석에서는 15m 봉 지표가 제공되지 않으므로, 1h 지표만으로 판단해라.

전략 기준:
- 1h RSI14가 55~70 구간에서 상승 모멘텀이 강하고 거래대금이 급증하면 돌파 매수
- RSI가 60 이상이더라도 거래대금이 폭발적으로 몰리며 추세를 탄다면 진입 가능
- ATR%가 2.5% 초과(고변동성): stop_loss_pct = ATR%의 1.5배 수준, weight_pct는 25 이하로 낮춰 리스크 제한
- ATR%가 1.5% ~ 2.5%(중변동성): stop_loss_pct = 1.8~2.5%, weight_pct 보통 수준
- ATR%가 1.5% 미만(저변동성): stop_loss_pct = 1.2~1.8%로 타이트하게, weight_pct 높게 배분 가능
- target_profit_pct는 stop_loss_pct의 1.5배 이상 유지 (단타이므로 R/R ≥ 1.5:1)
- 모멘텀이 완전히 없거나 거래대금이 매우 적을 때만 관망

반드시 아래 JSON 형식으로만 응답해 (다른 텍스트 없음):
{
  "market_summary": "현재 전체 시장 상황에 대한 분석 및 판단 근거를 2~3문장으로 요약.",
  "picks": [
    {
      "symbol":            "ETH/KRW",
      "score":             88,
      "weight_pct":        50,
      "reason":            "1h RSI 62, 거래대금 급증하며 1h MA20 돌파. ATR 1.8%(중변동) — 동적 손절 적용",
      "target_profit_pct": 3.0,
      "stop_loss_pct":     2.0
    }
  ]
}

score 기준:
- 80점 이상: 강력한 모멘텀 신호 — picks에 반드시 포함
- 79점 이하: 모멘텀 부족 — picks에 절대 포함하지 말 것

weight_pct 기준:
- 가용 예산 대비 투자 비중 (예: 50 → 가용 예산의 50% 투자)
- 두 종목 합산 총합이 100을 넘지 않도록 설정

규칙:
- market_summary는 관망을 선택했더라도 반드시 작성
- target_profit_pct: 양수, stop_loss_pct의 1.5배 이상으로 설정
- stop_loss_pct: 양수, ATR% 기반으로 동적 결정 (범위: 1.2 ~ 5.0)
- [절대 규칙 1] market_summary에서 긍정 평가한 종목은 핑계 대지 말고 반드시 picks에 포함
- [절대 규칙 2] picks를 비울 경우 market_summary에도 완전 부정적으로만 작성
- [절대 규칙 3] symbol은 반드시 "코인명/KRW" 형태로 작성 (예: ETH/KRW, BTC/KRW)
- [절대 규칙 4] 모든 숫자 필드는 % 기호나 +/- 부호 없이 순수 숫자만 작성
- [절대 규칙 5] 현재가(price) 100 KRW 미만 동전주는 아무리 지표가 좋아도 절대 picks에 포함하지 말 것
"""


def _build_user_prompt(
    snapshot: dict[str, dict],
    holding_symbols: set[str],
    available_krw: float,
) -> str:
    """마켓 스냅샷에서 LLM에게 전달할 유저 프롬프트를 생성한다.

    Args:
        snapshot:       _compute_snapshot_at() 반환값.
        holding_symbols: 현재 가상 보유 중인 심볼 집합 (AI 픽에서 제외).
        available_krw:  이번 사이클 가용 예산 (KRW).

    Returns:
        유저 프롬프트 문자열.
    """
    lines: list[str] = ["# 코인 시장 데이터 (1h 봉 기준)\n"]

    for symbol, data in snapshot.items():
        price   = data.get("price")
        chg     = data.get("change_pct")
        vol     = data.get("volume_krw")
        atr_pct = data.get("atr_pct")
        rsi_1h  = data.get("rsi14_1h")
        ma_1h   = data.get("ma20_1h")

        price_str = f"{price:,.0f} KRW" if price is not None else "N/A"
        atr_str   = f"{atr_pct:.2f}%"   if atr_pct is not None else "N/A"
        chg_str   = f"{chg:+.2f}%"      if chg is not None else "N/A"
        vol_str   = f"{vol / 1e8:.1f}억" if vol else "N/A"
        rsi_str   = f"{rsi_1h:.1f}"     if rsi_1h is not None else "N/A"
        ma_str    = f"{ma_1h:,.0f}"     if ma_1h is not None else "N/A"

        lines.append(
            f"- {symbol}: 현재가={price_str} | 변동성(ATR)={atr_str}"
            f" | 1h(RSI={rsi_str}, MA={ma_str})"
            f" | 24h변동={chg_str} | 24h대금={vol_str}"
        )

    if holding_symbols:
        lines.append(
            f"\n# 이미 보유 중 — 반드시 제외: {', '.join(sorted(holding_symbols))}"
        )

    if available_krw > 0:
        lines.append(f"\n# 이번 사이클 가용 예산: {available_krw:,.0f} KRW")

    return "\n".join(lines)


def _safe_pct(value: object, default: float) -> float:
    """AI 응답의 수익/손절률 값을 안전하게 float로 변환한다.

    Args:
        value:   원시 값 (str·int·float 혼용).
        default: 변환 불가 시 기본값.

    Returns:
        항상 양수 float.
    """
    try:
        cleaned = str(value).replace("%", "").replace("+", "").strip()
        return abs(float(cleaned))
    except (ValueError, TypeError):
        return default


def _parse_picks(raw: str, holding_symbols: set[str]) -> tuple[list[dict], bool]:
    """LLM 응답 문자열에서 픽 리스트를 파싱하고 유효성을 검증한다.

    JSON이 아닌 텍스트가 섞인 경우 중괄호 블록 추출을 시도한다 (Anthropic 대응).

    Args:
        raw:             LLM 원본 응답 문자열.
        holding_symbols: 보유 중 심볼 집합 (픽에서 제외).

    Returns:
        (validated_picks, is_parse_error) 튜플.
        is_parse_error=True 이면 JSON 파싱 자체가 실패한 경우.
    """
    # ── JSON 파싱 시도 ──────────────────────────────────────────────
    result: dict = {}
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # JSON 블록 추출 시도 (Anthropic이 텍스트 앞뒤에 설명을 붙이는 경우 대응)
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start != -1 and end > start:
            try:
                result = json.loads(raw[start:end])
            except json.JSONDecodeError:
                logger.warning("JSON 파싱 완전 실패: %s...", raw[:120])
                return [], True
        else:
            logger.warning("JSON 블록 미발견: %s...", raw[:120])
            return [], True

    # ── picks 검증 ──────────────────────────────────────────────────
    picks_raw: list = result.get("picks", [])
    validated: list[dict] = []

    for p in picks_raw:
        if not isinstance(p, dict):
            continue

        # symbol 정규화
        symbol = str(p.get("symbol", "")).strip().upper()
        if "/" not in symbol:
            symbol = f"{symbol}/KRW"
        else:
            base, quote = symbol.split("/", 1)
            symbol = f"{base.upper()}/{quote.upper()}"

        if not symbol.endswith("/KRW"):
            continue
        if symbol in holding_symbols:
            continue

        # score 파싱 및 80점 미만 필터
        try:
            score = max(0, min(100, int(p.get("score", 0) or 0)))
        except (ValueError, TypeError):
            score = 0

        if score < 80:
            continue

        # weight_pct 파싱
        try:
            weight_pct = max(0.0, float(p.get("weight_pct", 0) or 0))
        except (ValueError, TypeError):
            weight_pct = 0.0

        validated.append(
            {
                "symbol":            symbol,
                "score":             score,
                "weight_pct":        weight_pct,
                "reason":            str(p.get("reason", "")),
                "target_profit_pct": _safe_pct(p.get("target_profit_pct", 3.0), 3.0),
                "stop_loss_pct":     _safe_pct(p.get("stop_loss_pct",     2.0), 2.0),
            }
        )
        if len(validated) == 2:
            break

    return validated, False


# ──────────────────────────────────────────────────────────────────────────
# 어댑터 패턴 — BaseModelAdapter → OpenAI / Anthropic / Gemini
# ──────────────────────────────────────────────────────────────────────────


class BaseModelAdapter(abc.ABC):
    """AI 모델 어댑터 추상 기본 클래스.

    각 서브클래스는 서로 다른 AI API를 통해 동일한 인터페이스로 시장을 분석한다.

    Attributes:
        name:     어댑터 이름 (예: "openai", "anthropic", "gemini").
        model_id: 실제 사용 모델 ID (예: "gpt-4.1-mini").
    """

    name:     str
    model_id: str

    @abc.abstractmethod
    async def analyze(
        self,
        snapshot: dict[str, dict],
        holding_symbols: set[str],
        available_krw: float = VIRTUAL_KRW,
    ) -> tuple[list[dict], bool]:
        """마켓 스냅샷을 분석해 픽 리스트를 반환한다.

        Args:
            snapshot:       심볼 → 지표 딕셔너리 (MarketDataManager.get_all() 동일 구조).
            holding_symbols: 현재 가상 보유 중인 심볼 집합 (픽에서 제외).
            available_krw:  이번 사이클 가용 예산 (KRW).

        Returns:
            (picks, is_parse_error) 튜플.
            picks: [{"symbol", "score", "weight_pct", "reason",
                     "target_profit_pct", "stop_loss_pct"}, ...]
            is_parse_error: JSON 파싱 자체가 실패한 경우 True.
        """


class OpenAIAdapter(BaseModelAdapter):
    """OpenAI GPT 모델 어댑터.

    JSON 출력 강제: response_format={"type": "json_object"}
    기본 모델: gpt-4.1-mini (2026 최신 경량 모델)
    """

    name     = "openai"
    model_id = "gpt-4.1-mini"

    def __init__(self, api_key: str) -> None:
        """AsyncOpenAI 클라이언트를 초기화한다.

        Args:
            api_key: OpenAI API 키.
        """
        from openai import AsyncOpenAI  # 선택적 임포트 — 패키지 미설치 시 ImportError

        self._client = AsyncOpenAI(api_key=api_key)
        logger.info("[OpenAI] 어댑터 초기화 완료 (model=%s)", self.model_id)

    async def analyze(
        self,
        snapshot: dict[str, dict],
        holding_symbols: set[str],
        available_krw: float = VIRTUAL_KRW,
    ) -> tuple[list[dict], bool]:
        """OpenAI API로 시장을 분석해 픽을 반환한다."""
        user_prompt = _build_user_prompt(snapshot, holding_symbols, available_krw)
        try:
            resp = await self._client.chat.completions.create(
                model=self.model_id,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_prompt},
                ],
                temperature=0.5,
                max_tokens=700,
            )
            raw = resp.choices[0].message.content or "{}"
        except Exception as exc:
            logger.error("[OpenAI] API 호출 실패: %s", exc)
            return [], True

        return _parse_picks(raw, holding_symbols)


class AnthropicAdapter(BaseModelAdapter):
    """Anthropic Claude 모델 어댑터.

    JSON 출력 유도: 유저 프롬프트 끝에 "순수 JSON으로만 응답하라" 지시 추가.
    기본 모델: claude-sonnet-4-6 (2026.02 최신 밸런스 모델)
    """

    name     = "anthropic"
    model_id = "claude-sonnet-4-6"

    def __init__(self, api_key: str) -> None:
        """AsyncAnthropic 클라이언트를 초기화한다.

        Args:
            api_key: Anthropic API 키.

        Raises:
            ImportError: anthropic 패키지 미설치 시.
        """
        try:
            import anthropic as anthropic_sdk  # noqa: PLC0415

            self._client = anthropic_sdk.AsyncAnthropic(api_key=api_key)
            logger.info("[Anthropic] 어댑터 초기화 완료 (model=%s)", self.model_id)
        except ImportError as exc:
            raise ImportError(
                "anthropic 패키지가 설치되지 않았습니다. "
                "pip install anthropic 으로 설치하세요."
            ) from exc

    async def analyze(
        self,
        snapshot: dict[str, dict],
        holding_symbols: set[str],
        available_krw: float = VIRTUAL_KRW,
    ) -> tuple[list[dict], bool]:
        """Anthropic API로 시장을 분석해 픽을 반환한다."""
        user_prompt = _build_user_prompt(snapshot, holding_symbols, available_krw)
        # Claude는 system 파라미터와 user 메시지를 분리 전달.
        # 응답 끝에 JSON 힌트를 추가해 순수 JSON 응답을 유도한다.
        try:
            resp = await self._client.messages.create(
                model=self.model_id,
                max_tokens=700,
                system=_SYSTEM_PROMPT,
                messages=[
                    {
                        "role":    "user",
                        "content": user_prompt + "\n\n반드시 순수 JSON으로만 응답하라. 다른 텍스트 없음.",
                    }
                ],
                temperature=0.5,
            )
            raw = resp.content[0].text if resp.content else "{}"
        except Exception as exc:
            logger.error("[Anthropic] API 호출 실패: %s", exc)
            return [], True

        return _parse_picks(raw, holding_symbols)


class GeminiAdapter(BaseModelAdapter):
    """Google Gemini 모델 어댑터.

    JSON 출력 강제: generation_config={"response_mime_type": "application/json"}
    동기 SDK를 asyncio executor로 래핑해 비동기로 실행한다.
    기본 모델: gemini-2.5-flash (2026 최신 안정 고속 모델)
    """

    name     = "gemini"
    model_id = "gemini-2.5-flash"

    def __init__(self, api_key: str) -> None:
        """google-generativeai SDK를 초기화하고 모델 인스턴스를 생성한다.

        Args:
            api_key: Google Gemini API 키 (GEMINI_API_KEY 환경변수).

        Raises:
            ImportError: google-generativeai 패키지 미설치 시.
        """
        try:
            import google.generativeai as genai_sdk  # noqa: PLC0415

            genai_sdk.configure(api_key=api_key)
            self._model = genai_sdk.GenerativeModel(
                model_name=self.model_id,
                # JSON 출력 MIME 타입을 직접 지정해 마크다운 감싸기 방지
                generation_config={"response_mime_type": "application/json"},
                system_instruction=_SYSTEM_PROMPT,
            )
            logger.info("[Gemini] 어댑터 초기화 완료 (model=%s)", self.model_id)
        except ImportError as exc:
            raise ImportError(
                "google-generativeai 패키지가 설치되지 않았습니다. "
                "pip install google-generativeai 으로 설치하세요."
            ) from exc

    async def analyze(
        self,
        snapshot: dict[str, dict],
        holding_symbols: set[str],
        available_krw: float = VIRTUAL_KRW,
    ) -> tuple[list[dict], bool]:
        """Gemini API로 시장을 분석해 픽을 반환한다.

        동기 generate_content()를 run_in_executor로 래핑해 비동기 호환성을 확보한다.
        """
        user_prompt = _build_user_prompt(snapshot, holding_symbols, available_krw)
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: self._model.generate_content(user_prompt),
            )
            raw = resp.text if resp.text else "{}"
        except Exception as exc:
            logger.error("[Gemini] API 호출 실패: %s", exc)
            return [], True

        return _parse_picks(raw, holding_symbols)


# ──────────────────────────────────────────────────────────────────────────
# OHLCV 데이터 수집
# ──────────────────────────────────────────────────────────────────────────


async def fetch_ohlcv_data() -> dict[str, list[list]]:
    """업비트에서 백테스트 대상 코인의 1h OHLCV 데이터를 수집한다.

    CCXT upbit 공용 인스턴스로 각 심볼의 최근 FETCH_LIMIT 봉 데이터를 수집한다.
    심볼 간 CCXT_SLEEP 간격을 두어 Rate-Limit 오류를 방지한다.

    Returns:
        {심볼: [[timestamp_ms, open, high, low, close, volume], ...]} 딕셔너리.
        오래된 봉부터 최신 봉 순서로 정렬 (ccxt 기본 정렬).
    """
    exchange = ccxt.upbit({"enableRateLimit": True})
    loop     = asyncio.get_event_loop()
    result:  dict[str, list[list]] = {}

    for symbol in BACKTEST_SYMBOLS:
        logger.info("OHLCV 수집: %s  (1h × %d봉)", symbol, FETCH_LIMIT)
        try:
            fetch_fn  = partial(exchange.fetch_ohlcv, symbol, "1h", None, FETCH_LIMIT)
            ohlcv: list[list] = await loop.run_in_executor(None, fetch_fn)
            if ohlcv:
                result[symbol] = ohlcv
                first_dt = datetime.fromtimestamp(
                    ohlcv[0][0] / 1000, tz=timezone.utc
                ).strftime("%m/%d %H:%M")
                last_dt  = datetime.fromtimestamp(
                    ohlcv[-1][0] / 1000, tz=timezone.utc
                ).strftime("%m/%d %H:%M")
                logger.info(
                    "  ✓ %s: %d봉 수집 완료 (%s ~ %s)",
                    symbol, len(ohlcv), first_dt, last_dt,
                )
            else:
                logger.warning("  ✗ %s: 데이터 없음", symbol)
        except Exception as exc:
            logger.error("  ✗ %s OHLCV 수집 실패: %s", symbol, exc)

        await asyncio.sleep(CCXT_SLEEP)

    return result


# ──────────────────────────────────────────────────────────────────────────
# 슬라이딩 윈도우 → 마켓 스냅샷 계산
# ──────────────────────────────────────────────────────────────────────────


def _compute_snapshot_at(
    ohlcv_map: dict[str, list[list]],
    t: int,
) -> dict[str, dict]:
    """시각 t(인덱스)에서 분석 윈도우 내 지표를 계산해 마켓 스냅샷을 반환한다.

    윈도우: ohlcv[t-ANALYSIS_WINDOW : t] (60봉)로 RSI/MA/ATR을 계산한다.
    동전주 하드 필터(price < MIN_PRICE)가 적용되어 100 KRW 미만 코인은 스냅샷에서 제외된다.

    Args:
        ohlcv_map: 심볼 → 1h OHLCV 리스트.
        t:         분석 기준 인덱스 (봉 번호, 0-indexed).

    Returns:
        MarketDataManager.get_all()과 호환 가능한 딕셔너리.
    """
    snapshot: dict[str, dict] = {}

    for symbol, ohlcv in ohlcv_map.items():
        window = ohlcv[max(0, t - ANALYSIS_WINDOW): t]
        if len(window) < ANALYSIS_WINDOW:
            continue  # 데이터 부족: 워밍업 기간 아직 채워지지 않음

        df = pd.DataFrame(
            window,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        close = df["close"]
        price = float(window[-1][4])  # 마지막 봉 종가

        # ── 동전주 하드 필터 ─────────────────────────────────────────
        if price < MIN_PRICE:
            logger.debug("동전주 스킵 (백테스트): %s (가격=%.4f KRW)", symbol, price)
            continue

        rsi14_1h = _calc_rsi(close, 14)
        ma20_1h  = _calc_ma(close, 20)
        atr_val  = _calc_atr(df, 14)
        atr_pct  = (atr_val / price * 100) if (atr_val and price > 0) else None

        # 24h 변동률 (24봉 전 종가 대비)
        change_pct: float | None = None
        if len(window) >= 24:
            prev_price = float(window[-24][4])
            if prev_price > 0:
                change_pct = (price - prev_price) / prev_price * 100

        # 24h 거래대금 (최근 24봉: volume × close 합산)
        volume_krw: float | None = None
        if len(window) >= 24:
            volume_krw = sum(float(c[5]) * float(c[4]) for c in window[-24:])

        snapshot[symbol] = {
            "price":      price,
            "change_pct": change_pct,
            "volume_krw": volume_krw,
            "atr_pct":    atr_pct,
            "rsi14_1h":   rsi14_1h,
            "ma20_1h":    ma20_1h,
            # 4h·15m 지표는 1h only 백테스트에서 사용하지 않음
            "rsi14":      None,
            "ma20":       None,
            "rsi14_15m":  None,
            "ma20_15m":   None,
        }
        del df

    return snapshot


# ──────────────────────────────────────────────────────────────────────────
# 가상 매매 시뮬레이션
# ──────────────────────────────────────────────────────────────────────────


def _simulate_trade(
    ohlcv:      list[list],
    entry_idx:  int,
    target_pct: float,
    stop_pct:   float,
) -> tuple[float, float, str, int]:
    """가상 진입 후 익절/손절/타임아웃을 시뮬레이션한다.

    진입 가격은 entry_idx 봉의 open 가격이다.
    이후 각 봉의 high/low를 순서대로 체크하며 익절(target) 또는 손절(stop)을 판정한다.
    동일 봉에서 두 조건이 모두 충족되면 target(WIN)을 우선한다.

    Args:
        ohlcv:      전체 1h OHLCV 리스트.
        entry_idx:  진입 봉 인덱스.
        target_pct: 목표 익절률 (양수 %, 예: 3.0).
        stop_pct:   손절률 (양수 %, 예: 2.0).

    Returns:
        (exit_price, profit_pct, result, hold_hours) 튜플.
        result: "WIN" | "LOSS" | "TIMEOUT"
        hold_hours: 보유 시간 (봉 수 = 시간).
    """
    if entry_idx >= len(ohlcv):
        return 0.0, 0.0, "TIMEOUT", 0

    entry_price = float(ohlcv[entry_idx][1])  # 진입봉 open
    if entry_price <= 0:
        return 0.0, 0.0, "TIMEOUT", 0

    target_price = entry_price * (1.0 + target_pct / 100.0)
    stop_price   = entry_price * (1.0 - stop_pct / 100.0)
    end_idx      = min(entry_idx + MAX_HOLD_HOURS, len(ohlcv))

    for i in range(entry_idx, end_idx):
        candle = ohlcv[i]
        high   = float(candle[2])
        low    = float(candle[3])
        hold   = i - entry_idx

        # 익절 먼저 체크 (유리한 조건 우선)
        if high >= target_price:
            profit = (target_price - entry_price) / entry_price * 100.0
            return target_price, profit, "WIN", hold

        if low <= stop_price:
            profit = (stop_price - entry_price) / entry_price * 100.0
            return stop_price, profit, "LOSS", hold

    # 타임아웃: 마지막 봉 종가로 청산
    last_idx   = end_idx - 1
    exit_price = float(ohlcv[last_idx][4])  # 마지막 봉 close
    profit     = (exit_price - entry_price) / entry_price * 100.0
    hold       = last_idx - entry_idx
    return exit_price, profit, "TIMEOUT", hold


# ──────────────────────────────────────────────────────────────────────────
# 거래 결과 데이터클래스
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class TradeResult:
    """단일 가상 거래 결과를 저장하는 데이터클래스."""

    run_at:      str    # 백테스트 실행 시각 (UTC ISO 8601)
    model_name:  str    # 어댑터 이름 (openai / anthropic / gemini)
    model_id:    str    # 실제 사용 모델 ID
    signal_ts:   int    # 분석 기준 봉 타임스탬프 (Unix ms)
    symbol:      str    # 코인 심볼 (예: BTC/KRW)
    score:       int    # AI 부여 점수 (0~100)
    weight_pct:  float  # AI 부여 투자 비중 (%)
    entry_price: float  # 진입 가격 (KRW)
    target_pct:  float  # 목표 익절률 (%)
    stop_pct:    float  # 손절률 (%)
    exit_price:  float  # 청산 가격 (KRW)
    profit_pct:  float  # 실현 수익률 (%)
    result:      str    # "WIN" | "LOSS" | "TIMEOUT"
    hold_hours:  int    # 보유 시간 (시간)
    reason:      str    # AI 매수 근거

    def to_csv_row(self) -> dict:
        """CSV 출력용 딕셔너리로 변환한다."""
        return {
            "run_at":      self.run_at,
            "model_name":  self.model_name,
            "model_id":    self.model_id,
            "signal_ts":   self.signal_ts,
            "symbol":      self.symbol,
            "score":       self.score,
            "weight_pct":  self.weight_pct,
            "entry_price": self.entry_price,
            "target_pct":  self.target_pct,
            "stop_pct":    self.stop_pct,
            "exit_price":  self.exit_price,
            "profit_pct":  round(self.profit_pct, 4),
            "result":      self.result,
            "hold_hours":  self.hold_hours,
            "reason":      self.reason,
        }


# ──────────────────────────────────────────────────────────────────────────
# Forward-pass 백테스트 실행
# ──────────────────────────────────────────────────────────────────────────


async def run_backtest(
    adapter:   BaseModelAdapter,
    ohlcv_map: dict[str, list[list]],
    run_at:    str,
) -> tuple[list[TradeResult], int]:
    """단일 어댑터로 전체 Forward-pass 백테스트를 실행한다.

    슬라이딩 윈도우 방식으로 STEP_HOURS마다 LLM 분석을 수행하고,
    score≥80 픽에 대해 _simulate_trade()로 가상 매매 결과를 계산한다.

    중복 진입 방지: 동일 심볼이 이미 가상 진입 중이면 (미청산 상태) 추가 진입 스킵.

    Args:
        adapter:   AI 모델 어댑터 인스턴스.
        ohlcv_map: 심볼 → 1h OHLCV 리스트 딕셔너리.
        run_at:    백테스트 실행 시각 문자열 (CSV 기록용).

    Returns:
        (trades, parse_errors) 튜플.
        trades:       완료된 TradeResult 리스트.
        parse_errors: LLM JSON 파싱 실패 횟수.
    """
    if not ohlcv_map:
        logger.error("[%s] OHLCV 데이터 없음", adapter.name)
        return [], 0

    min_len = min(len(v) for v in ohlcv_map.values())
    if min_len < ANALYSIS_WINDOW + 2:
        logger.error(
            "[%s] 데이터 부족: 최소 %d봉 필요, 현재 %d봉",
            adapter.name, ANALYSIS_WINDOW + 2, min_len,
        )
        return [], 0

    # 분석 가능 범위: ANALYSIS_WINDOW ~ (min_len - MAX_HOLD_HOURS)
    # Forward scan을 위해 뒷부분 MAX_HOLD_HOURS 봉은 분석 제외
    end_idx     = min_len - MAX_HOLD_HOURS
    total_steps = (end_idx - ANALYSIS_WINDOW) // STEP_HOURS

    logger.info(
        "[%s] 백테스트 시작 — 총 분석 스텝: %d회 (윈도우=%d봉, 스텝=%d봉, 최대보유=%d봉)",
        adapter.name.upper(), total_steps, ANALYSIS_WINDOW, STEP_HOURS, MAX_HOLD_HOURS,
    )

    trades:       list[TradeResult] = []
    parse_errors: int               = 0

    # (심볼, 청산 인덱스) 추적용 — 중복 진입 방지
    # 청산 인덱스(exit_idx)가 현재 t보다 크면 아직 포지션이 열려 있다고 간주
    active_positions: dict[str, int] = {}  # symbol → 예상 exit_idx

    for step, t in enumerate(range(ANALYSIS_WINDOW, end_idx, STEP_HOURS), start=1):
        # 현재 t 기준으로 이미 청산된 포지션 정리
        active_positions = {sym: ei for sym, ei in active_positions.items() if ei > t}

        signal_ts = int(ohlcv_map[next(iter(ohlcv_map))][t - 1][0])  # 분석 기준 봉 타임스탬프 (ms)
        signal_dt = datetime.fromtimestamp(signal_ts / 1000, tz=timezone.utc).strftime(
            "%m/%d %H:%M"
        )

        # ── 마켓 스냅샷 계산 ─────────────────────────────────────────
        snapshot = _compute_snapshot_at(ohlcv_map, t)
        if not snapshot:
            logger.debug("[%s] step=%d (%s): 유효 스냅샷 없음", adapter.name, step, signal_dt)
            continue

        # ── LLM 분석 요청 ────────────────────────────────────────────
        holding_symbols = set(active_positions.keys())
        picks, is_error = await adapter.analyze(snapshot, holding_symbols, VIRTUAL_KRW)

        if is_error:
            parse_errors += 1
            logger.warning("[%s] step=%d (%s): JSON 파싱 에러", adapter.name, step, signal_dt)
            await asyncio.sleep(LLM_SLEEP)
            continue

        if not picks:
            logger.debug("[%s] step=%d (%s): 픽 없음 (관망)", adapter.name, step, signal_dt)
            await asyncio.sleep(LLM_SLEEP)
            continue

        logger.info(
            "[%s] step=%d/%d (%s): %d개 픽 → %s",
            adapter.name.upper(), step, total_steps, signal_dt,
            len(picks), [(p["symbol"], p["score"]) for p in picks],
        )

        # ── 가상 매매 시뮬레이션 ─────────────────────────────────────
        entry_idx = t  # 다음 봉(t)의 open 가격으로 진입

        for pick in picks:
            symbol = pick["symbol"]

            # OHLCV 데이터 존재 여부 확인
            if symbol not in ohlcv_map:
                logger.debug("[%s] %s: OHLCV 없음, 스킵", adapter.name, symbol)
                continue

            # 진입봉 범위 초과 확인
            if entry_idx >= len(ohlcv_map[symbol]):
                continue

            # 동전주 이중 방어 (스냅샷 계산 시에도 필터링되지만 진입 시 재확인)
            entry_open = float(ohlcv_map[symbol][entry_idx][1])
            if entry_open < MIN_PRICE:
                logger.debug(
                    "[%s] %s: 진입가 %.4f < %.0f KRW (동전주), 스킵",
                    adapter.name, symbol, entry_open, MIN_PRICE,
                )
                continue

            # 포지션 시뮬레이션
            exit_price, profit_pct, sim_result, hold = _simulate_trade(
                ohlcv_map[symbol],
                entry_idx,
                pick["target_profit_pct"],
                pick["stop_loss_pct"],
            )

            # 청산 인덱스 기록 (중복 진입 방지용)
            active_positions[symbol] = entry_idx + hold

            trade = TradeResult(
                run_at      = run_at,
                model_name  = adapter.name,
                model_id    = adapter.model_id,
                signal_ts   = signal_ts,
                symbol      = symbol,
                score       = pick["score"],
                weight_pct  = pick["weight_pct"],
                entry_price = entry_open,
                target_pct  = pick["target_profit_pct"],
                stop_pct    = pick["stop_loss_pct"],
                exit_price  = exit_price,
                profit_pct  = profit_pct,
                result      = sim_result,
                hold_hours  = hold,
                reason      = pick["reason"],
            )
            trades.append(trade)

            logger.info(
                "  ↳ %s: 진입=%.0f KRW → %s (%.2f%%, %dh)",
                symbol, entry_open, sim_result, profit_pct, hold,
            )

        await asyncio.sleep(LLM_SLEEP)

    logger.info(
        "[%s] 백테스트 완료: %d 스텝 처리, %d 거래 생성, 파싱에러=%d",
        adapter.name.upper(), step if total_steps > 0 else 0,
        len(trades), parse_errors,
    )
    return trades, parse_errors


# ──────────────────────────────────────────────────────────────────────────
# 결과 출력 및 CSV 저장
# ──────────────────────────────────────────────────────────────────────────


def _print_summary(
    adapter:      BaseModelAdapter,
    trades:       list[TradeResult],
    parse_errors: int,
) -> None:
    """백테스트 결과를 콘솔에 출력한다.

    Args:
        adapter:      실행한 어댑터 인스턴스.
        trades:       해당 모델의 거래 결과 리스트.
        parse_errors: JSON 파싱 실패 횟수.
    """
    total    = len(trades)
    wins     = sum(1 for t in trades if t.result == "WIN")
    losses   = sum(1 for t in trades if t.result == "LOSS")
    timeouts = sum(1 for t in trades if t.result == "TIMEOUT")
    win_rate     = wins / total * 100.0 if total > 0 else 0.0
    total_profit = sum(t.profit_pct for t in trades)
    avg_profit   = total_profit / total if total > 0 else 0.0

    print("\n" + "=" * 62)
    print(f"  📊 백테스트 결과 — {adapter.name.upper()}  ({adapter.model_id})")
    print("=" * 62)
    print(f"  총 매매 횟수   : {total}회")
    print(f"  승/패/타임아웃 : {wins}승 / {losses}패 / {timeouts}타임아웃")
    print(f"  승률           : {win_rate:.1f}%")
    print(f"  누적 수익률    : {total_profit:+.2f}%")
    print(f"  평균 거래 수익 : {avg_profit:+.2f}%")
    print(f"  파싱 에러      : {parse_errors}회")

    if trades:
        best  = max(trades, key=lambda t: t.profit_pct)
        worst = min(trades, key=lambda t: t.profit_pct)
        print(
            f"\n  최고 수익 : {best.symbol}  {best.profit_pct:+.2f}%"
            f"  ({best.result}, {best.hold_hours}h)"
        )
        print(
            f"  최대 손실 : {worst.symbol}  {worst.profit_pct:+.2f}%"
            f"  ({worst.result}, {worst.hold_hours}h)"
        )

        # 종목별 평균 수익 집계
        symbol_profits: dict[str, list[float]] = {}
        for t in trades:
            symbol_profits.setdefault(t.symbol, []).append(t.profit_pct)

        print("\n  종목별 평균 수익:")
        for sym, profits in sorted(symbol_profits.items()):
            avg = sum(profits) / len(profits)
            print(f"    {sym:12s}: {avg:+.2f}%  ({len(profits)}회)")

    print("=" * 62)


def _append_to_csv(trades: list[TradeResult]) -> None:
    """거래 결과를 backtest_results.csv에 Append 모드로 저장한다.

    파일이 존재하지 않으면 헤더를 먼저 기록한다.

    Args:
        trades: 저장할 거래 결과 리스트.
    """
    if not trades:
        return

    file_exists = CSV_FILE.exists()
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        for trade in trades:
            writer.writerow(trade.to_csv_row())

    logger.info("CSV 저장 완료: %s  (%d행 추가)", CSV_FILE, len(trades))


# ──────────────────────────────────────────────────────────────────────────
# 어댑터 팩토리
# ──────────────────────────────────────────────────────────────────────────


def _build_adapter(name: str) -> BaseModelAdapter:
    """환경변수에서 API 키를 읽어 어댑터 인스턴스를 생성한다.

    Args:
        name: 어댑터 이름 ("openai" | "anthropic" | "gemini").

    Returns:
        초기화된 BaseModelAdapter 서브클래스 인스턴스.

    Raises:
        EnvironmentError: 필수 API 키 환경변수가 미설정된 경우.
        ImportError:      필요한 패키지가 설치되지 않은 경우.
        ValueError:       알 수 없는 어댑터 이름이 전달된 경우.
    """
    if name == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise EnvironmentError(
                "OPENAI_API_KEY 환경변수가 설정되지 않았습니다. "
                ".env 또는 export OPENAI_API_KEY=... 로 설정하세요."
            )
        return OpenAIAdapter(api_key)

    if name == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다. "
                ".env 또는 export ANTHROPIC_API_KEY=... 로 설정하세요."
            )
        return AnthropicAdapter(api_key)

    if name == "gemini":
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise EnvironmentError(
                "GEMINI_API_KEY 환경변수가 설정되지 않았습니다. "
                ".env 또는 export GEMINI_API_KEY=... 로 설정하세요."
            )
        return GeminiAdapter(api_key)

    raise ValueError(
        f"알 수 없는 모델: '{name}'. "
        "'openai' / 'anthropic' / 'gemini' 중 하나를 선택하세요."
    )


# ──────────────────────────────────────────────────────────────────────────
# 진입점
# ──────────────────────────────────────────────────────────────────────────


async def main() -> None:
    """백테스팅 파이프라인의 메인 진입점.

    argparse로 --model 인자를 파싱하고, OHLCV 데이터를 수집한 뒤
    지정된 어댑터로 순차적으로 Forward-pass 백테스트를 실행한다.
    각 모델 결과는 콘솔에 출력하고 backtest_results.csv에 Append 저장한다.
    """
    parser = argparse.ArgumentParser(
        description="AI 퀀트 매니저 백테스팅 파이프라인 (OpenAI / Anthropic / Gemini)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
사용 예시:
  python scripts/backtester.py --model openai
  python scripts/backtester.py --model anthropic
  python scripts/backtester.py --model gemini
  python scripts/backtester.py --model all   # 3개 모델 순차 실행

환경변수 (필요한 모델만 설정):
  OPENAI_API_KEY    — OpenAI API 키
  ANTHROPIC_API_KEY — Anthropic API 키
  GEMINI_API_KEY    — Google Gemini API 키
        """,
    )
    parser.add_argument(
        "--model",
        choices=["openai", "anthropic", "gemini", "all"],
        required=True,
        help="사용할 AI 모델 선택 (all = 3개 모델 순차 실행)",
    )
    args = parser.parse_args()

    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("백테스트 파이프라인 시작 — model=%s | run_at=%s", args.model, run_at)

    # ── 어댑터 목록 구성 ──────────────────────────────────────────────
    model_names = ["openai", "anthropic", "gemini"] if args.model == "all" else [args.model]
    adapters: list[BaseModelAdapter] = []

    for name in model_names:
        try:
            adapters.append(_build_adapter(name))
        except (EnvironmentError, ImportError) as exc:
            logger.error("어댑터 초기화 실패 (%s): %s", name, exc)
            if args.model != "all":
                raise  # 단일 모델 지정 시 즉시 종료

    if not adapters:
        logger.error("실행 가능한 어댑터가 없습니다. 종료합니다.")
        return

    # ── OHLCV 데이터 수집 ─────────────────────────────────────────────
    print(f"\n📡 업비트 OHLCV 데이터 수집 중 (심볼: {', '.join(BACKTEST_SYMBOLS)})...")
    ohlcv_map = await fetch_ohlcv_data()

    if not ohlcv_map:
        logger.error("OHLCV 데이터 수집 실패. 종료합니다.")
        return

    logger.info(
        "데이터 수집 완료: %d개 심볼  봉 수: %s",
        len(ohlcv_map),
        {sym: len(v) for sym, v in ohlcv_map.items()},
    )

    # ── 모델별 백테스트 순차 실행 ─────────────────────────────────────
    all_trades: list[TradeResult] = []

    for adapter in adapters:
        print(f"\n🤖 [{adapter.name.upper()}] {adapter.model_id} 백테스트 실행 중...")
        start_time = time.time()

        trades, parse_errors = await run_backtest(adapter, ohlcv_map, run_at)

        elapsed = time.time() - start_time
        all_trades.extend(trades)

        _print_summary(adapter, trades, parse_errors)
        _append_to_csv(trades)

        logger.info("[%s] 완료: %.1f초 소요", adapter.name.upper(), elapsed)

    # ── 전체 모델 비교 요약 (all 모드) ───────────────────────────────
    if len(adapters) > 1:
        print("\n" + "=" * 62)
        print("  🏆 전체 모델 비교 요약")
        print("=" * 62)
        for adapter in adapters:
            model_trades  = [t for t in all_trades if t.model_name == adapter.name]
            total         = len(model_trades)
            wins          = sum(1 for t in model_trades if t.result == "WIN")
            win_rate      = wins / total * 100.0 if total > 0 else 0.0
            total_profit  = sum(t.profit_pct for t in model_trades)
            print(
                f"  {adapter.name.upper():10s} ({adapter.model_id:20s}): "
                f"{total:3d}회 | 승률 {win_rate:5.1f}% | 누적 {total_profit:+.2f}%"
            )
        print("=" * 62)

    print(f"\n✅ 백테스트 완료! 상세 결과: {CSV_FILE}\n")


if __name__ == "__main__":
    asyncio.run(main())
