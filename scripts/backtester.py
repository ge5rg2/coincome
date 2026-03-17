"""
backtester.py — AI 모델 기반 코인 매매 전략 백테스트 스크립트.

지원 모델:
  - OpenAI   : gpt-5.4
  - Anthropic: claude-sonnet-4-6
  - Gemini   : gemini-3.1-pro-preview

실행 예시:
  python scripts/backtester.py --model openai   --candles 500 --top 30
  python scripts/backtester.py --model anthropic --candles 500
  python scripts/backtester.py --model gemini   --candles 500

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

import ccxt.async_support as ccxt

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
            max_tokens=700,
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
# AI 어댑터 — Gemini
# ──────────────────────────────────────────────────────────────────────────────

class GeminiAdapter:
    """Google Gemini GenerativeAI API 어댑터."""

    def __init__(self, api_key: str) -> None:
        import google.generativeai as genai  # 런타임 임포트 (선택적 의존성)
        genai.configure(api_key=api_key)
        self._genai   = genai
        self.model    = MODEL_GEMINI

    async def pick(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        model = self._genai.GenerativeModel(
            model_name=self.model,
            system_instruction=system_prompt,
            generation_config={"temperature": 0.3, "max_output_tokens": 700},
        )
        # Gemini SDK 비동기 호출
        response = await model.generate_content_async(user_prompt)
        content      = response.text or "{}"
        usage        = getattr(response, "usage_metadata", None)
        input_tokens  = getattr(usage, "prompt_token_count",     0) if usage else 0
        output_tokens = getattr(usage, "candidates_token_count", 0) if usage else 0
        return {
            "raw":           content,
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
        }


# ──────────────────────────────────────────────────────────────────────────────
# 시스템 프롬프트 (SWING 전략 기준)
# ──────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
너는 4시간 봉 기반의 스윙 트레이더야.
제공된 Top 코인의 RSI·MA 지표, 가용 예산을 분석해서 지금 당장 매수하기 가장 좋은 코인을 최대 2개만 골라.

반드시 아래 JSON 형식으로만 응답해 (다른 텍스트 없음):
{
  "market_summary": "시장 분석 2~3문장.",
  "picks": [
    {
      "symbol":            "BTC/KRW",
      "score":             92,
      "weight_pct":        55,
      "reason":            "RSI 42 반등 및 4h 20MA 지지 확인",
      "target_profit_pct": 4.0,
      "stop_loss_pct":     2.0
    }
  ]
}

규칙:
- score 80 미만 종목은 picks에 절대 포함하지 말 것
- symbol은 "코인명/KRW" 형태로 작성 (예: BTC/KRW)
- 모든 숫자 필드는 순수 숫자만 (%, +/- 없음)
- 현재가 100 KRW 미만 동전주는 스킵
"""


# ──────────────────────────────────────────────────────────────────────────────
# 시장 데이터 수집 (Upbit via ccxt)
# ──────────────────────────────────────────────────────────────────────────────

async def fetch_top_symbols(exchange: ccxt.Exchange, top_n: int = 30) -> list[str]:
    """Upbit KRW 마켓에서 24h 거래대금 기준 상위 N개 심볼을 반환한다."""
    tickers = await exchange.fetch_tickers()
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


async def fetch_ohlcv_indicators(
    exchange: ccxt.Exchange,
    symbol: str,
    candles: int = 100,
) -> dict[str, Any]:
    """4h 봉 OHLCV를 가져와 RSI14·MA20을 계산한다."""
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe="4h", limit=candles)
        if len(ohlcv) < 21:
            return {}

        closes = [c[4] for c in ohlcv]
        current_price = closes[-1]

        # MA20
        ma20 = sum(closes[-20:]) / 20

        # RSI14
        gains, losses = [], []
        for i in range(1, 15):
            diff = closes[-i] - closes[-i - 1]
            (gains if diff > 0 else losses).append(abs(diff))
        avg_gain = sum(gains) / 14 if gains else 0.0
        avg_loss = sum(losses) / 14 if losses else 1e-9
        rsi14 = 100 - 100 / (1 + avg_gain / avg_loss)

        # ATR% (14-period)
        highs = [c[2] for c in ohlcv]
        lows  = [c[3] for c in ohlcv]
        trs   = []
        for i in range(1, 15):
            tr = max(highs[-i] - lows[-i], abs(highs[-i] - closes[-i - 1]), abs(lows[-i] - closes[-i - 1]))
            trs.append(tr)
        atr_pct = (sum(trs) / 14 / current_price * 100) if current_price else 0.0

        ticker = await exchange.fetch_ticker(symbol)
        volume_krw = ticker.get("quoteVolume", 0) or 0

        return {
            "price":      current_price,
            "ma20":       ma20,
            "rsi14":      rsi14,
            "atr_pct":    atr_pct,
            "volume_krw": volume_krw,
            "change_pct": ticker.get("percentage", 0) or 0,
        }
    except Exception as exc:
        logger.debug("지표 수집 실패 %s: %s", symbol, exc)
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# 유저 프롬프트 빌더
# ──────────────────────────────────────────────────────────────────────────────

def build_user_prompt(market_data: dict[str, dict], budget_krw: float = 1_000_000) -> str:
    lines = ["# Top 코인 시장 데이터 (4h 봉 기준)\n"]
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
        if score < 80:
            continue
        validated.append({
            "symbol":            symbol,
            "score":             score,
            "weight_pct":        float(p.get("weight_pct", 0) or 0),
            "reason":            str(p.get("reason", "")),
            "target_profit_pct": abs(float(p.get("target_profit_pct", 3.0) or 3.0)),
            "stop_loss_pct":     abs(float(p.get("stop_loss_pct",     2.0) or 2.0)),
        })
        if len(validated) == 2:
            break
    return validated


# ──────────────────────────────────────────────────────────────────────────────
# 간단 수익률 시뮬레이션
# ──────────────────────────────────────────────────────────────────────────────

async def simulate_trade(
    exchange: ccxt.Exchange,
    symbol: str,
    entry_price: float,
    target_pct: float,
    stop_pct: float,
    future_candles: int = 20,
) -> dict[str, Any]:
    """
    entry_price 기준으로 이후 `future_candles`개의 4h 봉에서
    목표가 또는 손절가에 먼저 도달하는지 확인한다.

    Returns:
        {
            "result":   "WIN" | "LOSS" | "TIMEOUT",
            "pnl_pct":  float,  # 실제 수익률 (%)
            "candles_held": int,
        }
    """
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe="4h", limit=future_candles + 1)
        # 마지막 봉 제외 (진입 시점 이후 봉만 사용)
        future = ohlcv[1:]
    except Exception as exc:
        logger.debug("시뮬레이션 데이터 수집 실패 %s: %s", symbol, exc)
        return {"result": "ERROR", "pnl_pct": 0.0, "candles_held": 0}

    target_price = entry_price * (1 + target_pct / 100)
    stop_price   = entry_price * (1 - stop_pct / 100)

    for i, candle in enumerate(future):
        high = candle[2]
        low  = candle[3]
        if high >= target_price:
            return {"result": "WIN",  "pnl_pct": target_pct,  "candles_held": i + 1}
        if low <= stop_price:
            return {"result": "LOSS", "pnl_pct": -stop_pct,   "candles_held": i + 1}

    # 타임아웃 — 마지막 종가 기준 수익률
    last_close = future[-1][4] if future else entry_price
    pnl = (last_close - entry_price) / entry_price * 100
    return {"result": "TIMEOUT", "pnl_pct": pnl, "candles_held": len(future)}


# ──────────────────────────────────────────────────────────────────────────────
# 메인 백테스트 루프
# ──────────────────────────────────────────────────────────────────────────────

async def run_backtest(args: argparse.Namespace) -> None:
    # ── 결과 디렉터리 준비 ────────────────────────────────────────────────────
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp  = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    result_csv = RESULT_DIR / f"backtest_results_{timestamp}.csv"
    logger.info("결과 저장 경로: %s", result_csv)

    # ── AI 어댑터 초기화 ──────────────────────────────────────────────────────
    api_key = args.api_key
    if not api_key:
        env_map = {
            "openai":    "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "gemini":    "GEMINI_API_KEY",
        }
        api_key = os.getenv(env_map.get(args.model, ""), "")
    if not api_key:
        logger.error(
            "API 키가 없습니다. --api-key 옵션 또는 환경변수로 설정하세요. "
            "(OPENAI_API_KEY / ANTHROPIC_API_KEY / GEMINI_API_KEY)"
        )
        sys.exit(1)

    adapter_cls = {"openai": OpenAIAdapter, "anthropic": AnthropicAdapter, "gemini": GeminiAdapter}[args.model]
    adapter = adapter_cls(api_key)
    model_id = adapter.model
    logger.info("사용 모델: %s", model_id)

    # ── 거래소 초기화 ──────────────────────────────────────────────────────────
    exchange = ccxt.upbit()
    try:
        logger.info("Upbit 마켓 데이터 로딩 중...")
        top_symbols = await fetch_top_symbols(exchange, top_n=args.top)
        logger.info("상위 %d개 심볼 선정: %s", len(top_symbols), top_symbols[:5])

        logger.info("지표 수집 중 (약 %d개 심볼)...", len(top_symbols))
        tasks = [fetch_ohlcv_indicators(exchange, sym, candles=args.candles) for sym in top_symbols]
        results_raw = await asyncio.gather(*tasks)

        market_data: dict[str, dict] = {}
        for sym, data in zip(top_symbols, results_raw):
            if data and data.get("price", 0) >= 100:  # 동전주 제외
                market_data[sym] = data
        logger.info("유효 심볼 %d개 확보", len(market_data))

        if not market_data:
            logger.error("유효한 시장 데이터가 없습니다.")
            return

        # ── AI 픽 요청 ────────────────────────────────────────────────────────
        user_prompt = build_user_prompt(market_data, budget_krw=args.budget)
        logger.info("AI에게 매수 픽 요청 중 (model=%s)...", model_id)

        try:
            ai_result = await adapter.pick(_SYSTEM_PROMPT, user_prompt)
        except Exception as exc:
            logger.error("AI 호출 실패: %s", exc)
            return

        raw_response  = ai_result["raw"]
        input_tokens  = ai_result["input_tokens"]
        output_tokens = ai_result["output_tokens"]
        call_cost     = calc_cost(model_id, input_tokens, output_tokens)

        logger.info(
            "AI 응답 수신 — 입력 토큰: %d, 출력 토큰: %d, 비용: $%.6f",
            input_tokens, output_tokens, call_cost,
        )

        picks = parse_picks(raw_response)
        logger.info("파싱된 픽: %s", [(p["symbol"], p["score"]) for p in picks])

        if not picks:
            logger.warning("유효한 픽이 없습니다. 결과 없이 종료합니다.")
            return

        # ── 누적 토큰·비용 집계 ───────────────────────────────────────────────
        total_input_tokens  = input_tokens
        total_output_tokens = output_tokens
        total_cost          = call_cost

        # ── 시뮬레이션 + CSV 작성 ─────────────────────────────────────────────
        csv_rows: list[dict] = []

        for pick in picks:
            symbol = pick["symbol"]
            entry  = market_data[symbol]["price"] if symbol in market_data else None
            if entry is None:
                logger.warning("엔트리 가격 없음: %s — 스킵", symbol)
                continue

            logger.info(
                "시뮬레이션: %s  진입가=%.0f  목표=+%.1f%%  손절=-%.1f%%",
                symbol, entry, pick["target_profit_pct"], pick["stop_loss_pct"],
            )
            sim = await simulate_trade(
                exchange, symbol, entry,
                target_pct=pick["target_profit_pct"],
                stop_pct=pick["stop_loss_pct"],
                future_candles=args.future_candles,
            )

            pick_cost = calc_cost(model_id, input_tokens, output_tokens) / max(len(picks), 1)
            # 주: 픽 1개당 비용은 API 호출 비용을 픽 수로 나눠 배분

            csv_rows.append({
                "Timestamp":           timestamp,
                "Model":               model_id,
                "Symbol":              symbol,
                "Score":               pick["score"],
                "Weight_Pct":          pick["weight_pct"],
                "Entry_Price":         entry,
                "Target_Profit_Pct":   pick["target_profit_pct"],
                "Stop_Loss_Pct":       pick["stop_loss_pct"],
                "Reason":              pick["reason"],
                "Sim_Result":          sim["result"],
                "Sim_PnL_Pct":         round(sim["pnl_pct"], 4),
                "Candles_Held":        sim["candles_held"],
                "Input_Tokens":        input_tokens,
                "Output_Tokens":       output_tokens,
                "Estimated_Cost_USD":  round(pick_cost, 6),
            })

        # ── CSV 파일 저장 ─────────────────────────────────────────────────────
        if csv_rows:
            fieldnames = list(csv_rows[0].keys())
            with result_csv.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(csv_rows)
            logger.info("결과 저장 완료: %s", result_csv)

        # ── 콘솔 요약 리포트 ──────────────────────────────────────────────────
        wins     = sum(1 for r in csv_rows if r["Sim_Result"] == "WIN")
        losses   = sum(1 for r in csv_rows if r["Sim_Result"] == "LOSS")
        timeouts = sum(1 for r in csv_rows if r["Sim_Result"] == "TIMEOUT")
        total    = len(csv_rows)
        win_rate = wins / total * 100 if total else 0.0
        avg_pnl  = sum(r["Sim_PnL_Pct"] for r in csv_rows) / total if total else 0.0

        sep = "─" * 55
        print(f"\n{'=' * 55}")
        print(f"  백테스트 결과 요약")
        print(f"{'=' * 55}")
        print(f"  모델          : {model_id}")
        print(f"  사용 심볼 수  : {len(market_data)}개")
        print(f"  AI 픽 수      : {total}개")
        print(f"{sep}")
        print(f"  매매 결과")
        print(f"  {'승 (WIN)  :':<20} {wins}회")
        print(f"  {'패 (LOSS) :':<20} {losses}회")
        print(f"  {'타임아웃  :':<20} {timeouts}회")
        print(f"  {'승률      :':<20} {win_rate:.1f}%")
        print(f"  {'평균 수익률:':<20} {avg_pnl:+.2f}%")
        print(f"{sep}")
        print(f"  AI 토큰 사용량")
        print(f"  {'총 입력 토큰 :':<20} {total_input_tokens:,}")
        print(f"  {'총 출력 토큰 :':<20} {total_output_tokens:,}")
        print(f"  {'총 발생 비용 :':<20} ${total_cost:.6f}")
        print(f"{sep}")
        print(f"  결과 파일: {result_csv}")
        print(f"{'=' * 55}\n")

    finally:
        await exchange.close()


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI 모델 기반 코인 매매 전략 백테스터",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--model",
        choices=["openai", "anthropic", "gemini"],
        default="openai",
        help="사용할 AI 모델 플랫폼",
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
        default=100,
        help="지표 계산용 4h 봉 수집 개수",
    )
    parser.add_argument(
        "--future-candles",
        type=int,
        default=20,
        help="매매 시뮬레이션용 미래 4h 봉 수",
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
