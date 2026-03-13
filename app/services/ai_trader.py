"""
AITraderService: OpenAI GPT-4o-mini 기반 코인 종목 분석·픽 서비스.

analyze_market() 호출 흐름:
  1. MarketDataManager 캐시 데이터 → 유저 프롬프트 텍스트로 직렬화
  2. GPT-4o-mini 에 system·user 프롬프트 전송 (JSON 출력 강제)
  3. 응답 JSON 파싱 → 보유 코인 제외 · 최대 2개 제한 후 반환

반환 형식:
  [
    {
      "symbol":            "BTC/KRW",
      "reason":            "RSI 30 이하 과매도 및 20MA 지지",
      "target_profit_pct": 5.0,   # 양수 (예: 5.0 → 매수가 대비 +5%)
      "stop_loss_pct":     3.0,   # 양수 (예: 3.0 → 매수가 대비 -3%)
    },
    ...
  ]

stop_loss_pct 부호 규칙:
  BotSetting.stop_loss_pct 는 양수로 저장되며,
  Position.stop_price = buy_price * (1 - stop_loss_pct / 100) 로 계산됨.
  AI 응답도 양수로 반환하도록 프롬프트에 명시하고,
  파싱 시 abs() 로 한 번 더 보정함.
"""
from __future__ import annotations

import json
import logging

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 시스템 프롬프트
# ------------------------------------------------------------------

_SYSTEM_PROMPT = """\
너는 10년 차 엘리트 크립토 퀀트 트레이더야.

제공된 Top 10 코인의 4시간 봉 RSI·MA 지표를 분석해서 지금 당장 매수하기 가장 좋은 코인을 최대 2개만 골라.
이미 유저가 보유 중인 코인은 반드시 제외해.
보수적으로 접근하고, 매수할 만한 종목이 없으면 빈 리스트를 반환해.

반드시 아래 JSON 형식으로만 응답해 (다른 텍스트 없음):
{
  "picks": [
    {
      "symbol":            "BTC/KRW",
      "reason":            "RSI 30 이하 과매도 및 20MA 지지선 확인",
      "target_profit_pct": 5.0,
      "stop_loss_pct":     3.0
    }
  ]
}

규칙:
- target_profit_pct: 양수 (예: 5.0 → 매수가 대비 +5% 목표)
- stop_loss_pct: 양수 (예: 3.0 → 매수가 대비 -3% 손절)
- picks가 없으면 반드시: {"picks": []}
"""


# ------------------------------------------------------------------
# 서비스 클래스
# ------------------------------------------------------------------

class AITraderService:
    """OpenAI를 통해 코인 종목을 분석하고 매수 픽을 반환하는 서비스.

    Attributes:
        _client: AsyncOpenAI 클라이언트 인스턴스.
    """

    def __init__(self) -> None:
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def analyze_market(
        self,
        market_data: dict[str, dict],
        holding_symbols: set[str],
    ) -> list[dict]:
        """MarketDataManager 캐시 데이터를 기반으로 최대 2개 코인을 픽한다.

        Args:
            market_data: MarketDataManager.get_all() 반환값.
                         키: 심볼(BTC/KRW), 값: price·rsi14·ma20·change_pct 등.
            holding_symbols: 유저가 현재 감시 중인(is_running=True) 코인 심볼 집합.
                             AI 픽에서 이 코인들은 자동 제외된다.

        Returns:
            픽 리스트 (최대 2개):
            [{"symbol", "reason", "target_profit_pct", "stop_loss_pct"}, ...]
            분석 불가 또는 픽 없으면 빈 리스트 반환.
        """
        if not market_data:
            logger.warning("AITraderService: 마켓 데이터 없음 — MarketDataManager 캐시 초기화 대기 중")
            return []

        if not settings.openai_api_key:
            logger.error("AITraderService: OPENAI_API_KEY 미설정")
            return []

        # ── 유저 프롬프트 구성 ────────────────────────────────────────
        lines: list[str] = ["# Top 코인 시장 데이터 (4h 봉 기준)\n"]
        for symbol, data in market_data.items():
            price    = data.get("price")
            rsi14    = data.get("rsi14")
            ma20     = data.get("ma20")
            chg      = data.get("change_pct")
            vol      = data.get("volume_krw")

            price_str = f"{price:,.0f} KRW" if price is not None else "N/A"
            rsi_str   = f"{rsi14:.1f}"      if rsi14  is not None else "N/A"
            ma_str    = f"{ma20:,.0f}"      if ma20   is not None else "N/A"
            chg_str   = f"{chg:+.2f}%"     if chg    is not None else "N/A"
            vol_str   = f"{vol / 1e8:.1f}억" if vol   is not None else "N/A"

            lines.append(
                f"- {symbol}: 현재가={price_str}  RSI14={rsi_str}  "
                f"MA20={ma_str}  24h변동={chg_str}  24h거래대금={vol_str}"
            )

        if holding_symbols:
            lines.append(
                f"\n# 이미 보유 중 — 반드시 제외: {', '.join(sorted(holding_symbols))}"
            )

        user_prompt = "\n".join(lines)
        logger.debug("AITraderService 프롬프트:\n%s", user_prompt)

        # ── OpenAI 호출 ───────────────────────────────────────────────
        try:
            response = await self._client.chat.completions.create(
                model="gpt-4o-mini",
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_prompt},
                ],
                temperature=0.3,   # 보수적 분석을 위해 낮은 temperature
                max_tokens=512,
            )
        except Exception as exc:
            logger.error("AITraderService OpenAI 호출 실패: %s", exc)
            return []

        raw = response.choices[0].message.content or "{}"
        logger.debug("AITraderService 응답: %s", raw)

        # ── 응답 파싱 및 안전 검증 ────────────────────────────────────
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("AITraderService JSON 파싱 실패: %s | raw=%s", exc, raw)
            return []

        picks: list[dict] = result.get("picks", [])

        validated: list[dict] = []
        for p in picks:
            if not isinstance(p, dict):
                continue
            symbol = p.get("symbol", "")
            if not symbol or symbol in holding_symbols:
                continue
            validated.append(
                {
                    "symbol":            symbol,
                    "reason":            str(p.get("reason", "")),
                    # 양수 강제 (AI가 음수로 반환하는 경우 방어)
                    "target_profit_pct": abs(float(p.get("target_profit_pct", 3.0))),
                    "stop_loss_pct":     abs(float(p.get("stop_loss_pct",     2.0))),
                }
            )
            if len(validated) == 2:
                break

        logger.info(
            "AITraderService 분석 완료: %d 개 픽 %s",
            len(validated),
            [v["symbol"] for v in validated],
        )
        return validated
