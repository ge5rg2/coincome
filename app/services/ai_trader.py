"""
AITraderService: OpenAI GPT-4o-mini 기반 코인 종목 분석·픽 서비스.

analyze_market() 호출 흐름:
  1. MarketDataManager 캐시 데이터 → 유저 프롬프트 텍스트로 직렬화
  2. GPT-4o-mini 에 system·user 프롬프트 전송 (JSON 출력 강제)
  3. 응답 JSON 파싱 → 보유 코인 제외 · 최대 2개 제한 후 반환

반환 형식 (analyze_market):
  {
    "market_summary":  "현재 시장 전반 분석 2~3문장.",  # 관망 이유 포함
    "picks": [
      {
        "symbol":            "BTC/KRW",
        "reason":            "RSI 30 이하 과매도 및 20MA 지지",
        "target_profit_pct": 5.0,   # 양수 (예: 5.0 → 매수가 대비 +5%)
        "stop_loss_pct":     3.0,   # 양수 (예: 3.0 → 매수가 대비 -3%)
      },
      ...
    ]
  }

review_positions() 호출 흐름:
  1. 현재 보유 포지션 데이터 + MarketDataManager 캐시 → 프롬프트 구성
  2. GPT-4o-mini 에 포지션 관리 시스템 프롬프트 + 유저 프롬프트 전송
  3. 응답 JSON 파싱 → MAINTAIN/UPDATE 리뷰 리스트 반환

반환 형식 (review_positions):
  [
    {
      "symbol":              "BTC/KRW",
      "action":              "MAINTAIN" | "UPDATE",
      "new_target_profit_pct": 5.0,   # 양수
      "new_stop_loss_pct":     3.0,   # 양수
      "reason":              "...",
    },
    ...
  ]

stop_loss_pct 부호 규칙:
  BotSetting.stop_loss_pct 는 양수로 저장되며,
  Position.stop_price = buy_price * (1 - stop_loss_pct / 100) 로 계산됨.
  AI 응답도 양수로 반환하도록 프롬프트에 명시하고,
  파싱 시 abs() 로 한 번 더 보정함.

trade_style 분기:
  SWING    — 4시간 봉 RSI·MA 기반 보수적 스윙 매매 (기본값)
             지표: rsi14, ma20 | temperature=0.3
  SCALPING — 1시간 봉 RSI·MA 기반 공격적 모멘텀 단타
             지표: rsi14_1h, ma20_1h | temperature=0.5
"""
from __future__ import annotations

import json
import logging

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 시스템 프롬프트 — SWING (4시간 봉, 보수적 스윙)
# ------------------------------------------------------------------

_SWING_SYSTEM_PROMPT = """\
너는 4시간 봉 기반의 엘리트 스윙 트레이더야.
휩쏘(단기 노이즈)를 걸러내고 묵직한 추세를 포착해 매수하는 것이 목표다.

제공된 Top 코인의 4시간 봉 RSI·MA 지표를 분석해서 지금 당장 매수하기 가장 좋은 코인을 최대 2개만 골라.
이미 유저가 보유 중인 코인은 반드시 제외해.

전략 기준:
- RSI14가 30~50 구간에서 반등 조짐이 보이거나 MA20 지지 확인 시 매수 검토
- 목표 익절(target_profit_pct): +3.0 ~ +7.0 사이에서 변동성에 맞게 설정 (절대 37% 같은 비정상 수치 금지)
- 손절 기준(stop_loss_pct): 2.0 ~ 4.0 사이에서 설정 (절대 24% 같은 비정상 수치 금지)
- 거래대금이 극히 적거나 추세 불명확하면 관망

반드시 아래 JSON 형식으로만 응답해 (다른 텍스트 없음):
{
  "market_summary": "현재 전체 시장 상황에 대한 분석 및 판단 근거를 2~3문장으로 요약. 매수 이유 또는 관망 이유를 명확히 서술.",
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
- market_summary는 관망을 선택했더라도 반드시 작성 (왜 아무것도 안 샀는지 이유 포함)
- target_profit_pct: 양수, 반드시 3.0 ~ 7.0 범위 내 값만 사용
- stop_loss_pct: 양수, 반드시 2.0 ~ 4.0 범위 내 값만 사용
- [절대 규칙 1 - 언행일치] market_summary에서 '지지선 확인', '추세 전환', '진입 적합' 등 긍정 평가한 종목이 있다면 핑계 대지 말고 무조건(MUST) picks 배열에 매수 데이터로 포함해야 한다. 분석만 하고 매수하지 않는 것은 시스템 오류다.
- [절대 규칙 2 - 관망의 조건] picks 배열을 []로 비울 것이라면, market_summary에도 "모든 코인이 과매수이거나 추세가 깨져서 전액 현금 관망한다"라고 철저히 부정적으로만 적어야 한다. 좋다고 해놓고 안 사는 것은 허용하지 않는다.
"""

# ------------------------------------------------------------------
# 시스템 프롬프트 — SCALPING (1시간 봉, 공격적 모멘텀 단타)
# ------------------------------------------------------------------

_SCALPING_SYSTEM_PROMPT = """\
너는 1시간 봉 스캘핑·단타 전문 AI 퀀트 트레이더야.
보수적인 접근은 버려라. 작은 모멘텀이라도 포착되면 즉각 매수를 실행하는 것이 너의 역할이다.

제공된 Top 코인의 1시간 봉 RSI·MA 지표와 24h 거래대금을 분석해서 지금 당장 모멘텀 돌파 진입하기 좋은 코인을 최대 2개만 골라.
이미 유저가 보유 중인 코인은 반드시 제외해.

전략 기준:
- RSI14(1h)가 55~70 구간에서 상승 모멘텀이 강하고 거래대금이 급증하면 돌파 매수
- RSI가 60 이상이더라도 거래대금이 폭발적으로 몰리며 추세를 탄다면 진입 가능
- 목표 익절(target_profit_pct): 1.5 ~ 2.0 사이 고정 (빠른 단타)
- 손절 기준(stop_loss_pct): 1.5 고정 (엄격한 리스크 관리)
- 모멘텀이 완전히 없거나 거래대금이 매우 적을 때만 관망

반드시 아래 JSON 형식으로만 응답해 (다른 텍스트 없음):
{
  "market_summary": "현재 전체 시장 상황에 대한 분석 및 판단 근거를 2~3문장으로 요약. 모멘텀 진입 이유 또는 관망 이유를 명확히 서술.",
  "picks": [
    {
      "symbol":            "ETH/KRW",
      "reason":            "1h RSI 62, 거래대금 급증하며 MA20 돌파 확인 — 모멘텀 진입",
      "target_profit_pct": 1.8,
      "stop_loss_pct":     1.5
    }
  ]
}

규칙:
- market_summary는 관망을 선택했더라도 반드시 작성
- target_profit_pct: 양수, 반드시 1.5 ~ 2.0 범위 내 값만 사용
- stop_loss_pct: 양수, 반드시 1.5 고정
- [절대 규칙 1 - 언행일치] market_summary 텍스트에서 특정 코인에 대해 '상승 모멘텀', '진입 유효', '상승세', '매수 적합' 등 긍정적인 평가를 했다면, 그 코인은 핑계 대지 말고 무조건(MUST) picks 배열에 매수 종목으로 포함해야 한다. 분석만 하고 picks를 비워두는 행동은 절대 금지한다.
- [절대 규칙 2 - 관망의 조건] picks 배열을 []로 비우고 싶다면, market_summary에도 반드시 "모든 코인의 상태가 나빠서 전액 관망한다"라고 부정적으로만 적어야 한다. 좋다고 해놓고 안 사는 것은 시스템 오류로 간주한다.
- [공격적 실행] 조건에 부합하는 종목이 단 1개라도 있다면 주저하지 말고 즉시 picks에 담아 실행에 옮겨라. 1시간 봉 단타 모드에서 관망은 최후의 수단이다.
"""

# ------------------------------------------------------------------
# 포지션 리뷰용 시스템 프롬프트 (SWING·SCALPING 공용)
# ------------------------------------------------------------------

_REVIEW_SYSTEM_PROMPT = """\
너는 현재 보유 중인 코인의 포지션을 관리하는 AI야.

각 코인의 매수가·현재가·수익률·현재 익절/손절 기준과 최신 RSI·MA 지표를 함께 보고,
기존 설정을 유지할지(MAINTAIN) 아니면 현재 추세에 맞춰 변경할지(UPDATE) 결정해.

반드시 아래 JSON 형식으로만 응답해 (다른 텍스트 없음):
{
  "reviews": [
    {
      "symbol":                "BTC/KRW",
      "action":                "MAINTAIN",
      "new_target_profit_pct": 5.0,
      "new_stop_loss_pct":     3.0,
      "reason":                "추세 유지 중, 기존 목표 적절"
    }
  ]
}

규칙:
- action은 반드시 "MAINTAIN" 또는 "UPDATE" 중 하나
- action=MAINTAIN이면 new_target_profit_pct/new_stop_loss_pct는 기존 값 그대로 반환
- action=UPDATE이면 현재 시장 상황에 맞는 새 값을 제안
- new_target_profit_pct: 양수 (예: 5.0 → 매수가 대비 +5% 목표)
- new_stop_loss_pct: 양수 (예: 3.0 → 매수가 대비 -3% 손절)
- 보수적으로 접근하고, 확실한 근거가 없으면 MAINTAIN을 선택해
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

    @staticmethod
    def _empty_analysis(summary: str) -> dict:
        """분석 불가 또는 오류 시 반환하는 빈 결과 딕셔너리."""
        return {"market_summary": summary, "picks": []}

    async def analyze_market(
        self,
        market_data: dict[str, dict],
        holding_symbols: set[str],
        trade_style: str = "SWING",
    ) -> dict:
        """MarketDataManager 캐시 데이터를 기반으로 시장을 분석하고 최대 2개 코인을 픽한다.

        Args:
            market_data: MarketDataManager.get_all() 반환값.
                         키: 심볼(BTC/KRW), 값: price·rsi14·ma20·rsi14_1h·ma20_1h 등.
            holding_symbols: 유저가 현재 감시 중인(is_running=True) 코인 심볼 집합.
                             AI 픽에서 이 코인들은 자동 제외된다.
            trade_style: "SWING" (4h 보수 스윙) 또는 "SCALPING" (1h 공격 단타).
                         지표 키 및 system 프롬프트·temperature 가 분기된다.

        Returns:
            {
              "market_summary": str,       # 현재 시장 전반 분석 2~3문장
              "picks": list[dict],         # 매수 픽 (최대 2개, 관망 시 빈 리스트)
            }
            분석 불가(API 오류, 캐시 없음)일 때도 같은 구조의 dict를 반환한다.
        """
        if not market_data:
            logger.warning("AITraderService: 마켓 데이터 없음 — MarketDataManager 캐시 초기화 대기 중")
            return self._empty_analysis(
                "마켓 데이터 캐시가 아직 초기화되지 않아 분석을 수행하지 못했습니다."
            )

        if not settings.openai_api_key:
            logger.error("AITraderService: OPENAI_API_KEY 미설정")
            return self._empty_analysis("OpenAI API 키가 설정되지 않아 분석을 수행하지 못했습니다.")

        # ── trade_style 분기 설정 ─────────────────────────────────────
        is_scalping = trade_style == "SCALPING"
        system_prompt = _SCALPING_SYSTEM_PROMPT if is_scalping else _SWING_SYSTEM_PROMPT
        temperature   = 0.5 if is_scalping else 0.3
        timeframe_label = "1h 봉 기준" if is_scalping else "4h 봉 기준"

        # ── 유저 프롬프트 구성 ────────────────────────────────────────
        lines: list[str] = [f"# Top 코인 시장 데이터 ({timeframe_label})\n"]
        for symbol, data in market_data.items():
            price = data.get("price")
            chg   = data.get("change_pct")
            vol   = data.get("volume_krw")

            # SCALPING: 1h 지표 / SWING: 4h 지표
            if is_scalping:
                rsi14 = data.get("rsi14_1h")
                ma20  = data.get("ma20_1h")
            else:
                rsi14 = data.get("rsi14")
                ma20  = data.get("ma20")

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

        # ── [AI DEBUG - INPUT] 디버깅용 입력 로그 ────────────────────
        logger.info(
            "[AI DEBUG - INPUT] analyze_market 프롬프트 (style=%s):\n%s",
            trade_style, user_prompt,
        )

        # ── OpenAI 호출 ───────────────────────────────────────────────
        try:
            response = await self._client.chat.completions.create(
                model="gpt-4o-mini",
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=600,
            )
        except Exception as exc:
            logger.error("AITraderService OpenAI 호출 실패: %s", exc)
            return self._empty_analysis(f"AI 분석 중 오류가 발생했습니다: {exc}")

        raw = response.choices[0].message.content or "{}"

        # ── [AI DEBUG - OUTPUT] 디버깅용 원본 응답 로그 ──────────────
        logger.info(
            "[AI DEBUG - OUTPUT] analyze_market 원본 응답 (style=%s):\n%s",
            trade_style, raw,
        )

        # ── 응답 파싱 및 안전 검증 ────────────────────────────────────
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("AITraderService JSON 파싱 실패: %s | raw=%s", exc, raw)
            return self._empty_analysis("AI 응답 파싱에 실패했습니다.")

        market_summary: str = str(result.get("market_summary", "시장 분석 결과를 가져오지 못했습니다."))
        picks_raw: list[dict] = result.get("picks", [])

        validated: list[dict] = []
        for p in picks_raw:
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
            "AITraderService 분석 완료 (style=%s): %d 개 픽 %s",
            trade_style,
            len(validated),
            [v["symbol"] for v in validated],
        )
        return {"market_summary": market_summary, "picks": validated}

    async def review_positions(
        self,
        positions_data: list[dict],
        market_data: dict[str, dict],
        trade_style: str = "SWING",
    ) -> list[dict]:
        """현재 보유 포지션의 익절/손절 기준을 AI가 재검토한다.

        각 포지션의 수익률과 최신 RSI·MA 지표를 함께 AI에 전달해
        기존 설정 유지(MAINTAIN) 또는 갱신(UPDATE) 여부를 결정한다.

        Args:
            positions_data: 보유 포지션 리스트.
                            각 항목: {
                                "symbol":            str,
                                "buy_price":         float,
                                "current_price":     float,
                                "profit_pct":        float,
                                "target_profit_pct": float,
                                "stop_loss_pct":     float,
                            }
            market_data: MarketDataManager.get_all() 반환값 (RSI·MA 지표 포함).
            trade_style: "SWING" 또는 "SCALPING".
                         SCALPING이면 1h 지표(rsi14_1h, ma20_1h)를 참조한다.

        Returns:
            리뷰 리스트:
            [{"symbol", "action", "new_target_profit_pct", "new_stop_loss_pct", "reason"}, ...]
            처리 불가 시 빈 리스트 반환.
        """
        if not positions_data:
            return []

        if not settings.openai_api_key:
            logger.error("AITraderService: OPENAI_API_KEY 미설정")
            return []

        is_scalping = trade_style == "SCALPING"

        # ── 유저 프롬프트 구성 ────────────────────────────────────────
        timeframe_label = "1h 봉 기준" if is_scalping else "4h 봉 기준"
        lines: list[str] = [f"# 현재 보유 포지션 ({timeframe_label})\n"]
        for pos in positions_data:
            symbol     = pos["symbol"]
            buy_price  = pos["buy_price"]
            cur_price  = pos["current_price"]
            profit_pct = pos["profit_pct"]
            tgt        = pos["target_profit_pct"]
            sl         = pos["stop_loss_pct"]

            # SCALPING: 1h 지표 / SWING: 4h 지표
            mdata = market_data.get(symbol, {})
            if is_scalping:
                rsi14 = mdata.get("rsi14_1h")
                ma20  = mdata.get("ma20_1h")
            else:
                rsi14 = mdata.get("rsi14")
                ma20  = mdata.get("ma20")

            rsi_str = f"{rsi14:.1f}" if rsi14 is not None else "N/A"
            ma_str  = f"{ma20:,.0f}" if ma20  is not None else "N/A"

            lines.append(
                f"- {symbol}: "
                f"매수가={buy_price:,.0f}KRW  현재가={cur_price:,.0f}KRW  "
                f"수익률={profit_pct:+.2f}%  "
                f"목표익절={tgt:.1f}%  손절기준={sl:.1f}%  "
                f"RSI14={rsi_str}  MA20={ma_str}"
            )

        user_prompt = "\n".join(lines)
        logger.debug("AITraderService(review) 프롬프트:\n%s", user_prompt)

        # ── OpenAI 호출 ───────────────────────────────────────────────
        try:
            response = await self._client.chat.completions.create(
                model="gpt-4o-mini",
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _REVIEW_SYSTEM_PROMPT},
                    {"role": "user",   "content": user_prompt},
                ],
                temperature=0.2,   # 포지션 관리는 더 보수적으로
                max_tokens=512,
            )
        except Exception as exc:
            logger.error("AITraderService(review) OpenAI 호출 실패: %s", exc)
            return []

        raw = response.choices[0].message.content or "{}"
        logger.debug("AITraderService(review) 응답: %s", raw)

        # ── 응답 파싱 및 안전 검증 ────────────────────────────────────
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("AITraderService(review) JSON 파싱 실패: %s | raw=%s", exc, raw)
            return []

        reviews_raw: list[dict] = result.get("reviews", [])
        valid_symbols = {pos["symbol"] for pos in positions_data}

        validated: list[dict] = []
        for r in reviews_raw:
            if not isinstance(r, dict):
                continue
            symbol = r.get("symbol", "")
            action = str(r.get("action", "MAINTAIN")).upper()
            if not symbol or symbol not in valid_symbols:
                continue
            if action not in ("MAINTAIN", "UPDATE"):
                action = "MAINTAIN"

            # 기존 포지션 기본값 (MAINTAIN 시 기존 값 그대로 유지)
            pos_defaults = next(
                (p for p in positions_data if p["symbol"] == symbol), {}
            )
            validated.append(
                {
                    "symbol": symbol,
                    "action": action,
                    # 양수 강제 (AI가 음수로 반환하는 경우 방어)
                    "new_target_profit_pct": abs(
                        float(r.get("new_target_profit_pct",
                                    pos_defaults.get("target_profit_pct", 3.0)))
                    ),
                    "new_stop_loss_pct": abs(
                        float(r.get("new_stop_loss_pct",
                                    pos_defaults.get("stop_loss_pct", 2.0)))
                    ),
                    "reason": str(r.get("reason", "")),
                }
            )

        logger.info(
            "AITraderService(review) 완료 (style=%s): %d 개 포지션 검토 "
            "(UPDATE=%d, MAINTAIN=%d)",
            trade_style,
            len(validated),
            sum(1 for v in validated if v["action"] == "UPDATE"),
            sum(1 for v in validated if v["action"] == "MAINTAIN"),
        )
        return validated
