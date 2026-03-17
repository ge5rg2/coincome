"""
backtester.py — AI 모델 기반 코인 매매 전략 백테스트 스크립트.

지원 모델:
  - OpenAI   : gpt-5.4
  - Anthropic: claude-sonnet-4-6
  - Gemini   : gemini-3.1-pro-preview (google-genai SDK 사용)

실행 예시:
  python scripts/backtester.py --model openai   --candles 200 --top 30
  python scripts/backtester.py --model anthropic --candles 200 --step 6
  python scripts/backtester.py --model gemini   --candles 200
  python scripts/backtester.py --model all      --candles 200

의존성:
  pip install google-genai      # Gemini 어댑터 (최신 SDK)
  pip install openai            # OpenAI 어댑터
  pip install anthropic         # Anthropic 어댑터

결과:
  .result/backtest_results_YYYYMMDD_HHMMSS.csv  (실행마다 새 파일 생성)
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import ccxt.async_support as ccxt

# KST 타임존 설정
try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except ImportError:
    from datetime import timedelta
    KST = timezone(timedelta(hours=9))  # type: ignore[assignment]

# ──────────────────────────────────────────────────────────────────────────────
# 로깅 설정
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# 모델 상수 및 가격표 (USD / 1M tokens)
# ──────────────────────────────────────────────────────────────────────────────
MODEL_OPENAI    = "gpt-5.4"
MODEL_ANTHROPIC = "claude-sonnet-4-6"
MODEL_GEMINI    = "gemini-3.1-pro-preview"

# { model_id: (input_usd_per_1m, output_usd_per_1m) }
TOKEN_PRICE_TABLE: dict[str, tuple[float, float]] = {
    MODEL_OPENAI:    (2.50, 15.00),
    MODEL_ANTHROPIC: (3.00, 15.00),
    MODEL_GEMINI:    (2.00, 12.00),
}

# 결과 저장 디렉터리
RESULT_DIR = Path(__file__).parent.parent / ".result"

# Time-Stepping 최소 워밍업 봉 수 (RSI14 + MA20 계산을 위한 최소 데이터)
WARMUP_CANDLES = 21

# 시뮬레이션에 필요한 미래 봉 최솟값 (이 미만이면 SKIP 처리 — 신규 상장·거래 정지 방어)
MIN_FUTURE_CANDLES = 5


# ──────────────────────────────────────────────────────────────────────────────
# 비용 계산 헬퍼
# ──────────────────────────────────────────────────────────────────────────────

def calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """주어진 토큰 수에 대한 예상 비용(USD)을 반환한다."""
    price_in, price_out = TOKEN_PRICE_TABLE.get(model, (0.0, 0.0))
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


# ──────────────────────────────────────────────────────────────────────────────
# AI 어댑터 — OpenAI
# ──────────────────────────────────────────────────────────────────────────────

class OpenAIAdapter:
    """OpenAI Chat Completions API 어댑터."""

    def __init__(self, api_key: str) -> None:
        from openai import AsyncOpenAI  # 런타임 임포트 (선택적 의존성)
        self._client = AsyncOpenAI(api_key=api_key)
        self.model = MODEL_OPENAI

    async def pick(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """AI에게 매수 픽을 요청하고 결과 + 토큰 사용량을 반환한다."""
        response = await self._client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.3,
            max_completion_tokens=700,  # gpt-5+ 계열: max_tokens → max_completion_tokens
        )
        content = response.choices[0].message.content or "{}"
        usage   = response.usage
        input_tokens  = usage.prompt_tokens     if usage else 0
        output_tokens = usage.completion_tokens if usage else 0
        return {
            "raw":           content,
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
        }


# ──────────────────────────────────────────────────────────────────────────────
# AI 어댑터 — Anthropic
# ──────────────────────────────────────────────────────────────────────────────

class AnthropicAdapter:
    """Anthropic Messages API 어댑터."""

    def __init__(self, api_key: str) -> None:
        import anthropic  # 런타임 임포트 (선택적 의존성)
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = MODEL_ANTHROPIC

    async def pick(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        response = await self._client.messages.create(
            model=self.model,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            temperature=0.3,
            max_tokens=700,
        )
        # content 블록 중 text 타입만 추출
        content = ""
        for block in response.content:
            if hasattr(block, "text"):
                content = block.text
                break
        usage         = response.usage
        input_tokens  = usage.input_tokens  if usage else 0
        output_tokens = usage.output_tokens if usage else 0
        return {
            "raw":           content,
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
        }


# ──────────────────────────────────────────────────────────────────────────────
# AI 어댑터 — Gemini (google-genai 최신 SDK)
# ──────────────────────────────────────────────────────────────────────────────

class GeminiAdapter:
    """Google Gemini GenerativeAI API 어댑터.

    의존성: pip install google-genai  (구 google-generativeai와 다른 패키지)
    비동기 클라이언트: client.aio.models.generate_content()
    JSON 출력 강제: GenerateContentConfig.response_mime_type="application/json"
    """

    def __init__(self, api_key: str) -> None:
        try:
            from google import genai          # 런타임 임포트 (선택적 의존성)
            from google.genai import types    # 설정 타입 클래스
        except ImportError as exc:
            raise ImportError(
                "google-genai 패키지가 설치되지 않았습니다. "
                "'pip install google-genai' 로 설치하세요. "
                "(구 google-generativeai 패키지와 다른 패키지입니다.)"
            ) from exc

        self._client = genai.Client(api_key=api_key)
        self._types  = types
        self.model   = MODEL_GEMINI

    async def pick(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Gemini API 비동기 호출로 AI 픽을 반환한다.

        GenerateContentConfig에 system_instruction과 response_mime_type을 주입해
        JSON 형식 응답을 강제한다.
        """
        config = self._types.GenerateContentConfig(
            response_mime_type="application/json",
            system_instruction=system_prompt,
            temperature=0.3,
        )
        # client.aio.models — 비동기 네임스페이스 (executor 래핑 불필요)
        response = await self._client.aio.models.generate_content(
            model=self.model,
            contents=user_prompt,
            config=config,
        )
        content       = response.text or "{}"
        usage         = getattr(response, "usage_metadata", None)
        input_tokens  = getattr(usage, "prompt_token_count",     0) if usage else 0
        output_tokens = getattr(usage, "candidates_token_count", 0) if usage else 0
        return {
            "raw":           content,
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
        }


# ──────────────────────────────────────────────────────────────────────────────
# 시스템 프롬프트 (스나이퍼 전략 v2 — Score 90+, 손절 7%+, BTC 국면 필터)
# ──────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
너는 4시간 봉 기반의 고승률 스나이퍼 트레이더야.
제공된 Top 코인의 RSI·MA·ATR 지표, 가용 예산을 분석해서 지금 당장 매수하기 가장 좋은 코인을 최대 2개만 골라.
확신이 없으면 picks 배열을 비워서 관망해도 된다 — 관망 자체가 최고의 전략일 수 있다.

반드시 아래 JSON 형식으로만 응답해 (다른 텍스트 없음):
{
  "market_summary": "시장 분석 2~3문장.",
  "picks": [
    {
      "symbol":            "BTC/KRW",
      "score":             91,
      "weight_pct":        25,
      "reason":            "RSI 48 반등, MA20 지지 확인, ATR% 4.2% 반영 손절폭 7.5% 설정",
      "target_profit_pct": 12.0,
      "stop_loss_pct":     7.5
    }
  ]
}

[핵심 매매 원칙 — 고승률 스나이퍼 v2]

1. BTC 시장 국면 필터 (최우선 — 모든 원칙보다 우선):
   - 유저 프롬프트에 "⛔ [BTC 하락장 — 강제 관망 발동]" 표시가 있으면:
     → picks 배열을 반드시 완전히 비울 것 (어떤 알트코인도 절대 진입 금지)
   - 유저 프롬프트에 "⚠️ [BTC 약세 — 극도 보수적 대응]" 표시가 있으면:
     → score 95 이상의 매우 확실한 종목만 고려. 아니면 관망.
   - BTC/KRW RSI14 < 45이면 알트코인 픽 극도로 자제
   - BTC/KRW 현재가가 MA20 아래에 있고 RSI14 < 50이면 알트코인 진입 금지

2. 손절폭 — 휩쏘 방어 (핵심 변경):
   - stop_loss_pct = ATR% × 2~3배 (최소 7% 이상 필수)
   - ATR% 3%대 기준: stop_loss_pct 7~9%
   - ATR% 5% 이상: stop_loss_pct 10~15%
   - stop_loss_pct 7% 미만으로 절대 설정하지 말 것 (좁은 손절 = 휩쏘 직격)

3. 리스크-리워드:
   - target_profit_pct는 stop_loss_pct의 최소 1.5배 이상
   - 예: stop_loss_pct 7% → target_profit_pct 최소 10.5%
   - 예: stop_loss_pct 10% → target_profit_pct 최소 15%

4. 포지션 사이징 — 보수적 비중:
   - weight_pct 최대 30% 이하 (손절폭이 넓으므로 투입 비중을 반드시 줄여야 함)
   - 2개 픽 시 합산 weight_pct가 50% 이하
   - 확신이 낮거나 변동성이 높으면 weight_pct 10~20% 사용

5. 진입 조건 (모두 충족 시에만 픽):
   - score 90 이상 (절대 90 미만 진입 금지 — 진입 빈도를 낮춰야 한다)
   - RSI14 35~60 구간 (상승 여력이 있는 중립~반등 구간)
   - MA20 지지 또는 직전 저항 돌파 확인
   - 24h 거래대금 50억 KRW 이상 (유동성 확보)
   - 과매수 구간(RSI14 > 65) 진입 금지

6. 일반 규칙:
   - symbol은 "코인명/KRW" 형태 (예: BTC/KRW)
   - 모든 숫자 필드는 순수 숫자만 (%, +/- 없음)
   - 현재가 100 KRW 미만 동전주는 스킵
"""


# ──────────────────────────────────────────────────────────────────────────────
# 시장 데이터 수집 (Upbit via ccxt) — 상위 심볼 선정 (1회 실행)
# ──────────────────────────────────────────────────────────────────────────────

async def fetch_top_symbols(exchange: ccxt.Exchange, top_n: int = 30) -> list[str]:
    """Upbit KRW 마켓에서 24h 거래대금 기준 상위 N개 심볼을 반환한다.

    업비트 전체 마켓 심볼 수(700+)를 그대로 fetch_tickers()에 전달하면
    URL 길이 초과(exceeding max URL length) 에러가 발생한다.
    load_markets()로 마켓 목록을 먼저 로드하고, KRW 심볼만 필터링한 뒤
    해당 리스트만 fetch_tickers()에 전달해 URL 길이 초과를 방어한다.
    """
    # 1. 마켓 목록 로드 (이미 로드된 경우 캐시 사용)
    await exchange.load_markets()

    # 2. KRW 마켓 심볼만 필터링
    krw_symbols = [sym for sym in exchange.symbols if sym.endswith("/KRW")]
    logger.info("KRW 마켓 심볼 %d개 필터링 완료", len(krw_symbols))

    # 3. 필터링된 심볼 리스트만 전달 — URL 길이 초과 방어
    tickers = await exchange.fetch_tickers(krw_symbols)

    krw_tickers = {
        sym: t for sym, t in tickers.items()
        if sym.endswith("/KRW") and t.get("quoteVolume")
    }
    sorted_syms = sorted(
        krw_tickers,
        key=lambda s: krw_tickers[s]["quoteVolume"],
        reverse=True,
    )
    return sorted_syms[:top_n]


# ──────────────────────────────────────────────────────────────────────────────
# 지표 계산 — 사전 수집된 OHLCV 슬라이스 기반 (API 호출 없음)
# ──────────────────────────────────────────────────────────────────────────────

def compute_indicators_from_ohlcv(ohlcv: list[list]) -> dict[str, Any]:
    """사전 수집된 OHLCV 슬라이스에서 RSI14·MA20·ATR%·거래대금을 계산한다.

    Live API 호출 없이 순수 데이터 연산으로 지표를 산출한다.
    Time-Stepping 루프에서 각 시점의 window 슬라이스를 받아 사용한다.

    Args:
        ohlcv: [[timestamp_ms, open, high, low, close, volume], ...] 형태의 리스트.
               최소 WARMUP_CANDLES(21)개 이상이어야 한다.

    Returns:
        지표 딕셔너리 또는 데이터 부족·가격 이상 시 빈 딕셔너리.
    """
    if len(ohlcv) < WARMUP_CANDLES:
        return {}

    closes = [c[4] for c in ohlcv]
    highs  = [c[2] for c in ohlcv]
    lows   = [c[3] for c in ohlcv]

    current_price = closes[-1]
    if current_price <= 0:
        return {}

    # MA20 — 최근 20봉 단순 이동평균
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else closes[-1]

    # RSI14 — 최근 14봉 상승/하락 평균 비율
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, min(15, len(closes))):
        diff = closes[-i] - closes[-i - 1]
        (gains if diff > 0 else losses).append(abs(diff))
    avg_gain = sum(gains) / 14 if gains else 0.0
    avg_loss = sum(losses) / 14 if losses else 1e-9
    rsi14    = 100 - 100 / (1 + avg_gain / avg_loss)

    # ATR% — 최근 14봉 True Range 평균 / 현재가 × 100
    trs: list[float] = []
    for i in range(1, min(15, len(ohlcv))):
        tr = max(
            highs[-i] - lows[-i],
            abs(highs[-i] - closes[-i - 1]),
            abs(lows[-i]  - closes[-i - 1]),
        )
        trs.append(tr)
    atr_pct = (sum(trs) / len(trs) / current_price * 100) if trs else 0.0

    # 24h 변동률 — 4h 봉 기준 6봉 전 대비
    change_pct = 0.0
    if len(closes) >= 7:
        change_pct = (closes[-1] - closes[-7]) / closes[-7] * 100

    # 24h 거래대금 — 최근 6봉(24시간) volume × close 합산
    volume_krw = sum(c[5] * c[4] for c in ohlcv[-6:]) if len(ohlcv) >= 6 else 0.0

    return {
        "price":      current_price,
        "ma20":       ma20,
        "rsi14":      rsi14,
        "atr_pct":    atr_pct,
        "volume_krw": volume_krw,
        "change_pct": change_pct,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 유저 프롬프트 빌더
# ──────────────────────────────────────────────────────────────────────────────

def build_user_prompt(market_data: dict[str, dict], budget_krw: float = 1_000_000) -> str:
    """AI에 전달할 유저 프롬프트를 빌드한다.

    BTC 시장 국면(Market Regime)을 가장 먼저 명시해 AI가 하락장 필터를
    즉시 적용할 수 있도록 유도한다. BTC 상태에 따라 강제 관망 / 극도 주의 /
    정상 진입 세 가지 등급으로 구분해 경고 문구를 삽입한다.
    """
    lines: list[str] = []

    # ── BTC 시장 국면(Market Regime) 분석 — 최우선 전달 ──────────────────────
    btc = market_data.get("BTC/KRW", {})
    if btc:
        btc_rsi       = btc.get("rsi14", 50.0)
        btc_price     = btc.get("price", 0.0)
        btc_ma20      = btc.get("ma20",  0.0)
        btc_above_ma20 = (btc_price >= btc_ma20) if btc_ma20 > 0 else True
        ma20_tag      = "MA20 위" if btc_above_ma20 else "MA20 아래"

        # 강제 관망: RSI < 40 이거나, MA20 아래이면서 RSI < 50
        if btc_rsi < 40 or (not btc_above_ma20 and btc_rsi < 50):
            lines.append(
                f"⛔ [BTC 하락장 — 강제 관망 발동]\n"
                f"   BTC RSI14={btc_rsi:.1f} | 현재가 {btc_price:,.0f}KRW ({ma20_tag})\n"
                f"   → picks 배열을 반드시 비울 것 (어떤 알트코인도 진입 금지)\n"
            )
        # 극도 주의: RSI < 45 이거나 MA20 아래
        elif btc_rsi < 45 or not btc_above_ma20:
            lines.append(
                f"⚠️ [BTC 약세 — 극도 보수적 대응]\n"
                f"   BTC RSI14={btc_rsi:.1f} | 현재가 {btc_price:,.0f}KRW ({ma20_tag})\n"
                f"   → 알트코인 픽 극도 자제. score 95 이상 확실한 종목만 고려.\n"
            )
        # 정상 진입 가능
        else:
            lines.append(
                f"✅ [BTC 상승장 — 정상 진입 가능]\n"
                f"   BTC RSI14={btc_rsi:.1f} | 현재가 {btc_price:,.0f}KRW ({ma20_tag})\n"
            )

    # ── 코인별 시장 데이터 ────────────────────────────────────────────────────
    lines.append("# Top 코인 시장 데이터 (4h 봉 기준)\n")
    for symbol, d in market_data.items():
        price   = d.get("price")
        rsi14   = d.get("rsi14")
        ma20    = d.get("ma20")
        atr_pct = d.get("atr_pct")
        chg     = d.get("change_pct")
        vol     = d.get("volume_krw")

        lines.append(
            f"- {symbol}: 현재가={price:,.0f}KRW | RSI14={rsi14:.1f} | MA20={ma20:,.0f}KRW"
            f" | ATR%={atr_pct:.2f}% | 24h변동={chg:+.2f}% | 24h대금={vol/1e8:.1f}억"
            if all(v is not None for v in [price, rsi14, ma20, atr_pct, chg, vol])
            else f"- {symbol}: 지표 없음"
        )
    lines.append(f"\n# 이번 사이클 가용 예산: {budget_krw:,.0f} KRW")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# 픽 파싱 헬퍼
# ──────────────────────────────────────────────────────────────────────────────

def parse_picks(raw: str) -> list[dict]:
    """AI 원시 응답에서 picks 리스트를 추출·검증한다."""
    try:
        # JSON 블록이 마크다운 코드펜스로 감싸진 경우 제거
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("JSON 파싱 실패 — raw: %s", raw[:200])
        return []

    picks_raw = result.get("picks", [])
    validated = []
    for p in picks_raw:
        if not isinstance(p, dict):
            continue
        symbol = str(p.get("symbol", "")).strip().upper()
        if "/" not in symbol:
            symbol = f"{symbol}/KRW"
        if not symbol.endswith("/KRW"):
            continue
        try:
            score = int(p.get("score", 0) or 0)
        except (ValueError, TypeError):
            score = 0
        # 스나이퍼 v2: score 90 미만은 진입 빈도를 줄이기 위해 하드 차단
        if score < 90:
            continue
        raw_stop = abs(float(p.get("stop_loss_pct", 7.5) or 7.5))
        # 스나이퍼 v2 하드 하한: stop_loss_pct 7% 미만이면 강제 보정 (휩쏘 방어)
        stop_loss_pct = max(raw_stop, 7.0)
        raw_weight = float(p.get("weight_pct", 0) or 0)
        # 손절폭이 넓으므로 투입 비중 상한 30% 강제
        weight_pct = min(raw_weight, 30.0)
        validated.append({
            "symbol":            symbol,
            "score":             score,
            "weight_pct":        weight_pct,
            "reason":            str(p.get("reason", "")),
            "target_profit_pct": abs(float(p.get("target_profit_pct", 12.0) or 12.0)),
            "stop_loss_pct":     stop_loss_pct,
        })
        if len(validated) == 2:
            break
    return validated


# ──────────────────────────────────────────────────────────────────────────────
# 매매 시뮬레이션 — 사전 수집된 미래 봉 기반 (API 호출 없음)
# ──────────────────────────────────────────────────────────────────────────────

def simulate_trade_from_data(
    future_ohlcv: list[list],
    entry_price:  float,
    target_pct:   float,
    stop_pct:     float,
) -> dict[str, Any]:
    """사전 수집된 미래 봉 데이터로 익절/손절/타임아웃 시뮬레이션을 실행한다.

    entry_price 기준으로 future_ohlcv의 각 봉 high/low를 순서대로 확인한다.
    동일 봉에서 익절·손절 조건이 모두 충족되면 익절(WIN)을 우선한다.

    신규 상장·거래 정지 등으로 미래 봉이 부족하거나 비정상적인 경우
    "SKIP"을 반환한다. SKIP 결과는 메인 루프에서 CSV에 포함되지 않는다.

    Args:
        future_ohlcv: Time-Stepping 기준 시점 이후의 OHLCV 슬라이스.
        entry_price:  가상 진입 가격 (기준 시점 마지막 봉 종가).
        target_pct:   목표 익절률 (양수 %).
        stop_pct:     손절률 (양수 %).

    Returns:
        {"result": "WIN"|"LOSS"|"TIMEOUT"|"SKIP", "pnl_pct": float, "candles_held": int}
    """
    # ── 기본 유효성 검사 ──────────────────────────────────────────────────────
    if not future_ohlcv or entry_price <= 0:
        return {"result": "SKIP", "pnl_pct": 0.0, "candles_held": 0}

    # ── 봉 데이터 유효성 필터 ────────────────────────────────────────────────
    # 신규 상장·거래 정지 등으로 인한 비정상 봉 (high<low, 음수가 등) 제거
    valid_ohlcv = [
        c for c in future_ohlcv
        if len(c) >= 5
        and c[2] > 0 and c[3] > 0 and c[4] > 0  # high, low, close 모두 양수
        and c[2] >= c[3]                           # high >= low (데이터 무결성)
    ]

    # ── 미래 봉 최소 개수 미달 — 타임아웃 또는 SKIP 처리 ────────────────────
    if len(valid_ohlcv) < MIN_FUTURE_CANDLES:
        if valid_ohlcv:
            # 가용한 봉만으로 종가 기반 PnL 계산 후 TIMEOUT 처리
            pnl = (valid_ohlcv[-1][4] - entry_price) / entry_price * 100
            return {"result": "TIMEOUT", "pnl_pct": round(pnl, 4), "candles_held": len(valid_ohlcv)}
        # 유효 봉이 아예 없으면 SKIP (CSV 제외 대상)
        return {"result": "SKIP", "pnl_pct": 0.0, "candles_held": 0}

    # ── 정상 시뮬레이션 ──────────────────────────────────────────────────────
    target_price = entry_price * (1 + target_pct / 100)
    stop_price   = entry_price * (1 - stop_pct  / 100)

    for i, candle in enumerate(valid_ohlcv):
        high = candle[2]
        low  = candle[3]
        if high >= target_price:
            return {"result": "WIN",  "pnl_pct": target_pct,  "candles_held": i + 1}
        if low <= stop_price:
            return {"result": "LOSS", "pnl_pct": -stop_pct,   "candles_held": i + 1}

    # 타임아웃 — 마지막 봉 종가 기준 수익률
    last_close = valid_ohlcv[-1][4]
    pnl = (last_close - entry_price) / entry_price * 100
    return {"result": "TIMEOUT", "pnl_pct": round(pnl, 4), "candles_held": len(valid_ohlcv)}


# ──────────────────────────────────────────────────────────────────────────────
# 모델별 요약 출력
# ──────────────────────────────────────────────────────────────────────────────

ENV_KEY_MAP = {
    "openai":    "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini":    "GEMINI_API_KEY",
}

ADAPTER_MAP = {
    "openai":    OpenAIAdapter,
    "anthropic": AnthropicAdapter,
    "gemini":    GeminiAdapter,
}


def _print_model_summary(
    model_id: str,
    rows: list[dict],
    input_tokens: int,
    output_tokens: int,
    cost: float,
    initial_balance: float = 1_000_000,
    final_balance: float | None = None,
) -> None:
    wins     = sum(1 for r in rows if r["Sim_Result"] == "WIN")
    losses   = sum(1 for r in rows if r["Sim_Result"] == "LOSS")
    timeouts = sum(1 for r in rows if r["Sim_Result"] == "TIMEOUT")
    total    = len(rows)
    win_rate = wins / total * 100 if total else 0.0
    avg_pnl  = sum(r["Sim_PnL_Pct"] for r in rows) / total if total else 0.0
    print(f"\n  ┌ 모델: {model_id}")
    print(f"  │ 픽 수: {total}  승={wins} 패={losses} 타임아웃={timeouts}  "
          f"승률={win_rate:.1f}%  평균PnL={avg_pnl:+.2f}%")
    if final_balance is not None:
        pnl_total = final_balance - initial_balance
        pnl_sign  = "+" if pnl_total >= 0 else ""
        roi       = pnl_total / initial_balance * 100 if initial_balance else 0.0
        print(f"  │ 잔고: {initial_balance:,.0f} → {final_balance:,.0f} KRW  "
              f"(총 손익금: {pnl_sign}{pnl_total:,.0f} KRW / ROI: {pnl_sign}{roi:.2f}%)")
    print(f"  │ 토큰: 입력={input_tokens:,}  출력={output_tokens:,}  비용=${cost:.6f}")
    print(f"  └{'─' * 53}")


# ──────────────────────────────────────────────────────────────────────────────
# 메인 백테스트 루프 — Time-Stepping (과거 → 현재 순회)
# ──────────────────────────────────────────────────────────────────────────────

async def run_backtest(args: argparse.Namespace) -> None:
    """과거 OHLCV 데이터를 시간 순서대로 순회하며 AI 매매 전략을 검증한다.

    전체 흐름:
      [1단계] 상위 심볼 선정 (현재 거래대금 기준, 1회)
      [2단계] 전체 과거 OHLCV 일괄 수집 (candles + future_candles 봉)
      [3단계] Time-Stepping 루프:
               - step마다 window 슬라이스 → 지표 계산 → AI 픽 요청
               - 미래 봉 슬라이스로 simulate_trade_from_data() 실행
      [4단계] CSV 저장 + 콘솔 요약 리포트
    """
    # ── 결과 디렉터리 준비 ────────────────────────────────────────────────────
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp  = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    result_csv = RESULT_DIR / f"backtest_results_{timestamp}.csv"
    logger.info("결과 저장 경로: %s", result_csv)

    # ── 플랫폼/어댑터 결정 ───────────────────────────────────────────────────
    platforms = ["openai", "anthropic", "gemini"] if args.model == "all" else [args.model]

    api_keys: dict[str, str] = {}
    for platform in platforms:
        key = args.api_key if args.model != "all" else ""
        if not key:
            key = os.getenv(ENV_KEY_MAP[platform], "")
        if key:
            api_keys[platform] = key
        else:
            logger.warning(
                "[%s] API 키 없음 — 환경변수 %s 미설정, 해당 모델 스킵",
                platform.upper(), ENV_KEY_MAP[platform],
            )

    if not api_keys:
        logger.error("실행 가능한 모델이 없습니다. API 키를 확인하세요.")
        sys.exit(1)

    # ── 거래소 초기화 ─────────────────────────────────────────────────────────
    exchange = ccxt.upbit()
    try:
        # ────────────────────────────────────────────────────────────────────
        # [1단계] 상위 심볼 선정
        # ────────────────────────────────────────────────────────────────────
        logger.info("[데이터 수집] 상위 심볼 선정 중 (거래대금 기준 Top %d)...", args.top)
        top_symbols = await fetch_top_symbols(exchange, top_n=args.top)
        logger.info("[데이터 수집] 선정된 심볼 (%d개): %s", len(top_symbols), top_symbols)

        # ────────────────────────────────────────────────────────────────────
        # [2단계] 전체 과거 OHLCV 일괄 수집
        # ────────────────────────────────────────────────────────────────────
        total_fetch = args.candles + args.future_candles
        logger.info(
            "[데이터 수집] %d개 심볼의 과거 %d개 캔들 데이터 로딩 중..."
            " (4h 봉 × %d = 약 %.0f일치)",
            len(top_symbols), total_fetch, total_fetch, total_fetch * 4 / 24,
        )

        all_ohlcv: dict[str, list[list]] = {}
        for i, sym in enumerate(top_symbols, 1):
            try:
                ohlcv = await exchange.fetch_ohlcv(sym, timeframe="4h", limit=total_fetch)
                if len(ohlcv) >= WARMUP_CANDLES + args.future_candles:
                    all_ohlcv[sym] = ohlcv
                    logger.debug(
                        "[데이터 수집]  %2d/%d  %-12s: %d봉",
                        i, len(top_symbols), sym, len(ohlcv),
                    )
            except Exception as exc:
                logger.warning("[데이터 수집] %s OHLCV 수집 실패: %s", sym, exc)

        logger.info(
            "[데이터 수집] 완료 — 유효 심볼 %d개 / 요청 %d개",
            len(all_ohlcv), len(top_symbols),
        )
        if not all_ohlcv:
            logger.error("유효한 OHLCV 데이터가 없습니다. 종료합니다.")
            return

        # ────────────────────────────────────────────────────────────────────
        # [3단계] Time-Stepping 루프 범위 결정
        # ────────────────────────────────────────────────────────────────────
        # 기준 심볼 (BTC/KRW 우선, 없으면 첫 번째 심볼) 의 타임스탬프를 참조
        ref_sym  = "BTC/KRW" if "BTC/KRW" in all_ohlcv else next(iter(all_ohlcv))
        ref_data = all_ohlcv[ref_sym]

        step_start   = WARMUP_CANDLES           # 최소 워밍업 이후부터 분석
        step_end     = len(ref_data) - args.future_candles  # 미래 봉 확보 범위까지
        step_size    = args.step                # --step 인자 (기본 6봉 = 24시간)
        step_indices = list(range(step_start, step_end, step_size))
        total_steps  = len(step_indices)

        logger.info(
            "[백테스트] 시작 — 분석 심볼: %d개 | 총 스텝: %d회 | "
            "스텝 간격: %d봉(%.0f시간) | 미래 봉: %d개",
            len(all_ohlcv), total_steps,
            step_size, step_size * 4,
            args.future_candles,
        )

        # 모델별 누적 통계 초기화 (balance: 가상 시드 1M KRW 누적 잔고)
        per_model: dict[str, dict] = {
            p: {"rows": [], "in_tok": 0, "out_tok": 0, "cost": 0.0, "balance": args.budget}
            for p in platforms if p in api_keys
        }
        all_csv_rows: list[dict] = []

        # ────────────────────────────────────────────────────────────────────
        # [3단계] Time-Stepping 루프 — 과거 시간을 스텝 단위로 전진
        # ────────────────────────────────────────────────────────────────────
        for step_num, step_idx in enumerate(step_indices, start=1):

            # 이 시점의 기준 타임스탬프 (분석 마지막 봉)
            candle_ts = ref_data[step_idx - 1][0]
            dt_kst    = datetime.fromtimestamp(candle_ts / 1000, tz=KST)
            dt_str    = dt_kst.strftime("%Y-%m-%d %H:%M")

            logger.info(
                "=" * 62 + "\n"
                "  [시뮬레이션 시간: %s KST (스텝 %d/%d)]",
                dt_str, step_num, total_steps,
            )

            # 이 시점까지의 데이터 슬라이스로 마켓 스냅샷 계산
            market_data: dict[str, dict] = {}
            for sym, ohlcv in all_ohlcv.items():
                window = ohlcv[:step_idx]  # step_idx 이전 봉만 사용 (미래 데이터 누수 방지)
                data   = compute_indicators_from_ohlcv(window)
                if data and data.get("price", 0) >= 100:   # 동전주 하드 필터
                    market_data[sym] = data

            if not market_data:
                logger.warning("스텝 %d/%d: 유효 심볼 없음, 스킵", step_num, total_steps)
                continue

            user_prompt = build_user_prompt(market_data, budget_krw=args.budget)

            # ── 모델별 AI 픽 요청 → 시뮬레이션 ─────────────────────────────
            for platform in platforms:
                if platform not in api_keys:
                    continue

                adapter  = ADAPTER_MAP[platform](api_keys[platform])
                model_id = adapter.model
                logger.info("[%s] 스냅샷 데이터 분석 요청 중...", model_id)

                try:
                    ai_result = await adapter.pick(_SYSTEM_PROMPT, user_prompt)
                except Exception as exc:
                    logger.error("[%s] AI 호출 실패: %s", model_id, exc)
                    continue

                in_tok  = ai_result["input_tokens"]
                out_tok = ai_result["output_tokens"]
                cost    = calc_cost(model_id, in_tok, out_tok)

                per_model[platform]["in_tok"]  += in_tok
                per_model[platform]["out_tok"]  += out_tok
                per_model[platform]["cost"]     += cost

                logger.info(
                    "[%s] 응답 수신 — 입력: %d tok, 출력: %d tok, 비용: $%.6f",
                    model_id, in_tok, out_tok, cost,
                )

                picks = parse_picks(ai_result["raw"])
                if not picks:
                    logger.info("[%s] 이 시점에서 유효한 픽 없음 (관망)", model_id)
                    continue

                per_pick_cost = cost / max(len(picks), 1)

                for pick in picks:
                    symbol = pick["symbol"]
                    logger.info(
                        "[%s] 픽 발생: %s (Score %d) - 이유: %s",
                        model_id, symbol, pick["score"], pick["reason"],
                    )

                    entry_price = market_data.get(symbol, {}).get("price")
                    if entry_price is None:
                        logger.warning("[%s] 엔트리 가격 없음: %s — 스킵", model_id, symbol)
                        continue

                    # 미래 봉 슬라이스: step_idx 이후 ~ step_idx + future_candles
                    future_ohlcv = all_ohlcv.get(symbol, [])[step_idx: step_idx + args.future_candles]

                    # 미래 봉 사전 검증 — MIN_FUTURE_CANDLES 미달 시 조기 경고
                    if len(future_ohlcv) < MIN_FUTURE_CANDLES:
                        logger.warning(
                            "[%s] 미래 봉 부족 (%d봉 < 최소 %d봉): %s — 시뮬레이션 스킵",
                            model_id, len(future_ohlcv), MIN_FUTURE_CANDLES, symbol,
                        )

                    sim = simulate_trade_from_data(
                        future_ohlcv, entry_price,
                        target_pct=pick["target_profit_pct"],
                        stop_pct=pick["stop_loss_pct"],
                    )

                    # SKIP: 데이터 부족·비정상 봉 → 잔고 변동 없이 CSV에서 제외
                    if sim["result"] == "SKIP":
                        logger.warning(
                            "[%s] 시뮬레이션 스킵: %s — 유효 미래 봉 없음 (신규 상장 또는 거래 정지 의심)",
                            model_id, symbol,
                        )
                        continue

                    # ── 가상 시드 잔고 업데이트 ─────────────────────────────
                    current_balance = per_model[platform]["balance"]
                    invested_krw    = current_balance * pick["weight_pct"] / 100
                    pnl_krw         = invested_krw * sim["pnl_pct"] / 100
                    per_model[platform]["balance"] += pnl_krw
                    new_balance = per_model[platform]["balance"]

                    logger.info(
                        "[결과] %s 시뮬레이션 완료 -> %s (%.2f%%) / 보유시간: %d봉"
                        " | 투자: %s KRW / 손익: %s KRW / 잔고: %s KRW",
                        symbol, sim["result"], sim["pnl_pct"], sim["candles_held"],
                        f"{invested_krw:,.0f}",
                        f"{pnl_krw:+,.0f}",
                        f"{new_balance:,.0f}",
                    )

                    row = {
                        "Timestamp":          dt_str,
                        "Model":              model_id,
                        "Symbol":             symbol,
                        "Score":              pick["score"],
                        "Weight_Pct":         pick["weight_pct"],
                        "Entry_Price":        entry_price,
                        "Target_Profit_Pct":  pick["target_profit_pct"],
                        "Stop_Loss_Pct":      pick["stop_loss_pct"],
                        "Reason":             pick["reason"],
                        "Sim_Result":         sim["result"],
                        "Sim_PnL_Pct":        round(sim["pnl_pct"], 4),
                        "Candles_Held":       sim["candles_held"],
                        "Invested_KRW":       round(invested_krw),
                        "PnL_KRW":            round(pnl_krw),
                        "Balance_KRW":        round(new_balance),
                        "Input_Tokens":       in_tok,
                        "Output_Tokens":      out_tok,
                        "Estimated_Cost_USD": round(per_pick_cost, 6),
                    }
                    all_csv_rows.append(row)
                    per_model[platform]["rows"].append(row)

            # ── 진행률 로깅 (10% 단위 또는 마지막 스텝) ─────────────────────
            if total_steps > 0 and (
                step_num % max(1, total_steps // 10) == 0 or step_num == total_steps
            ):
                logger.info(
                    "[진행 상황] 백테스트 %.0f%% 완료 (%d / %d 스텝)",
                    step_num / total_steps * 100, step_num, total_steps,
                )

        # ────────────────────────────────────────────────────────────────────
        # [4단계] CSV 저장
        # ────────────────────────────────────────────────────────────────────
        if all_csv_rows:
            fieldnames = list(all_csv_rows[0].keys())
            with result_csv.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(all_csv_rows)
            logger.info("결과 저장 완료: %s (%d행)", result_csv, len(all_csv_rows))
        else:
            logger.warning("저장할 거래 내역이 없습니다.")

        # ────────────────────────────────────────────────────────────────────
        # 콘솔 요약 리포트
        # ────────────────────────────────────────────────────────────────────
        sep = "─" * 57
        print(f"\n{'=' * 57}")
        print("  백테스트 결과 요약")
        print(f"{'=' * 57}")
        print(f"  분석 심볼 수  : {len(all_ohlcv)}개")
        print(f"  총 스텝 수    : {total_steps}회 (스텝 간격: {args.step}봉 = {args.step * 4}시간)")
        print(f"  실행 모델     : {', '.join(p for p in platforms if p in api_keys)}")
        print(f"{sep}")

        # 모델별 상세 (2개 이상일 때만)
        if len(per_model) > 1:
            print("  [ 모델별 결과 ]")
            for platform, stats in per_model.items():
                model_id = ADAPTER_MAP[platform](api_keys[platform]).model
                _print_model_summary(
                    model_id, stats["rows"], stats["in_tok"], stats["out_tok"], stats["cost"],
                    initial_balance=args.budget,
                    final_balance=stats["balance"],
                )
            print(f"{sep}")

        # 전체 합산
        total    = len(all_csv_rows)
        wins     = sum(1 for r in all_csv_rows if r["Sim_Result"] == "WIN")
        losses   = sum(1 for r in all_csv_rows if r["Sim_Result"] == "LOSS")
        timeouts = sum(1 for r in all_csv_rows if r["Sim_Result"] == "TIMEOUT")
        win_rate = wins / total * 100 if total else 0.0
        avg_pnl  = sum(r["Sim_PnL_Pct"] for r in all_csv_rows) / total if total else 0.0

        total_in   = sum(s["in_tok"]  for s in per_model.values())
        total_out  = sum(s["out_tok"] for s in per_model.values())
        total_cost = sum(s["cost"]    for s in per_model.values())

        print("  [ 전체 합산 ]")
        print(f"  {'총 AI 픽 수  :':<20} {total}개")
        print(f"  {'승 (WIN)     :':<20} {wins}회")
        print(f"  {'패 (LOSS)    :':<20} {losses}회")
        print(f"  {'타임아웃     :':<20} {timeouts}회")
        print(f"  {'승률         :':<20} {win_rate:.1f}%")
        print(f"  {'평균 수익률  :':<20} {avg_pnl:+.2f}%")
        print(f"{sep}")
        print("  [ 가상 시드 잔고 추적 (모델별) ]")
        print(f"  {'초기 시드    :':<20} {args.budget:,.0f} KRW")
        for platform, stats in per_model.items():
            model_id    = ADAPTER_MAP[platform](api_keys[platform]).model
            final_bal   = stats["balance"]
            pnl_total   = final_bal - args.budget
            pnl_sign    = "+" if pnl_total >= 0 else ""
            roi         = pnl_total / args.budget * 100 if args.budget else 0.0
            print(
                f"  {model_id:<22}  "
                f"{args.budget:>12,.0f} → {final_bal:>12,.0f} KRW  "
                f"({pnl_sign}{pnl_total:,.0f} KRW / ROI {pnl_sign}{roi:.2f}%)"
            )
        print(f"{sep}")
        print("  [ AI 토큰 사용량 ]")
        print(f"  {'총 입력 토큰 :':<20} {total_in:,}")
        print(f"  {'총 출력 토큰 :':<20} {total_out:,}")
        print(f"  {'총 발생 비용 :':<20} ${total_cost:.6f}")
        print(f"{sep}")
        print(f"  결과 파일: {result_csv}")
        print(f"{'=' * 57}\n")

    finally:
        await exchange.close()


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI 모델 기반 코인 매매 전략 백테스터 (Time-Stepping)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--model",
        choices=["openai", "anthropic", "gemini", "all"],
        default="openai",
        help="사용할 AI 모델 플랫폼 (all: 세 모델 모두 실행)",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="AI 플랫폼 API 키 (미지정 시 환경변수 사용)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=30,
        help="분석할 상위 코인 수 (거래대금 기준)",
    )
    parser.add_argument(
        "--candles",
        type=int,
        default=200,
        help="지표 계산용 과거 4h 봉 수 (분석 기간)",
    )
    parser.add_argument(
        "--future-candles",
        type=int,
        default=20,
        help="매매 시뮬레이션용 미래 4h 봉 수",
    )
    parser.add_argument(
        "--step",
        type=int,
        default=6,
        help="AI 분석 사이클 간격 (4h 봉 수, 기본 6 = 24시간)",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=1_000_000,
        help="AI에 전달할 가용 예산 (KRW)",
    )
    args = parser.parse_args()
    asyncio.run(run_backtest(args))


if __name__ == "__main__":
    main()
