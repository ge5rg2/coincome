"""
AITraderService: OpenAI GPT-4o-mini 기반 코인 종목 분석·픽 서비스.

analyze_market() 호출 흐름:
  1. MarketDataManager 캐시 데이터 + 가용 예산 → 유저 프롬프트 텍스트로 직렬화
  2. GPT-4o-mini 에 system·user 프롬프트 전송 (JSON 출력 강제)
  3. 응답 JSON 파싱 → score ≥ 80 필터 · 보유 코인 제외 · 최대 2개 제한 후 반환

반환 형식 (analyze_market):
  {
    "market_summary":  "현재 시장 전반 분석 2~3문장.",
    "picks": [
      {
        "symbol":            "BTC/KRW",
        "score":             92,           # 0~100점 (80 이상만 포함)
        "weight_pct":        60,           # 1~100 (가용 예산 대비 비중, 총합 100 이하)
        "reason":            "RSI 42 반등 및 4h 20MA 지지 확인",
        "target_profit_pct": 5.0,
        "stop_loss_pct":     3.0,
      },
      ...
    ]
  }

review_positions() 호출 흐름:
  1. 현재 보유 포지션 데이터 + MarketDataManager 캐시 → 프롬프트 구성
  2. GPT-4o-mini 에 포지션 관리 시스템 프롬프트 + 유저 프롬프트 전송
  3. 응답 JSON 파싱 → HOLD/UPDATE/SELL 리뷰 리스트 반환

반환 형식 (review_positions):
  [
    {
      "symbol":              "BTC/KRW",
      "action":              "HOLD" | "UPDATE" | "SELL",
      "new_target_profit_pct": 5.0,   # SELL 시 None
      "new_stop_loss_pct":     3.0,   # SELL 시 None
      "reason":              "...",
    },
    ...
  ]

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
from app.utils.format import format_krw_price

logger = logging.getLogger(__name__)


def _safe_pct(value: object, *, default: float) -> float:
    """AI 응답의 수익/손절률 값을 안전하게 float 로 변환한다.

    AI가 '%' 기호, '+'/'-' 부호, 공백을 붙여 반환하는 경우를 방어한다.
    예: "+5.0%", "5.0 %", "-3.0" → abs(float) 로 정규화.

    Args:
        value:   AI JSON 응답에서 가져온 원시 값 (str·int·float 혼용 가능).
        default: 변환 불가 시 사용할 기본값.

    Returns:
        항상 양수 float 반환.
    """
    try:
        cleaned = str(value).replace("%", "").replace("+", "").strip()
        return abs(float(cleaned))
    except (ValueError, TypeError):
        logger.warning(
            "AITraderService: pct 값 파싱 실패 %r → 기본값 %.1f 사용", value, default
        )
        return default


# ------------------------------------------------------------------
# 시스템 프롬프트 — SWING (4시간 봉, 보수적 스윙)
# ------------------------------------------------------------------

_SWING_SYSTEM_PROMPT = """\
너는 4시간 봉 기반의 엘리트 스윙 트레이더야.
휩쏘(단기 노이즈)를 걸러내고 묵직한 추세를 포착해 매수하는 것이 목표다.
고정된 익절/손절가는 버려라. 각 코인의 변동성(ATR%)에 맞는 동적 리스크 관리가 핵심이다.

제공된 Top 코인의 4h/1h/15m 지표, 변동성(ATR%), 가용 예산을 분석해서 지금 당장 매수하기 가장 좋은 코인을 최대 2개만 골라.
이미 유저가 보유 중인 코인은 반드시 제외해.

전략 기준:
- 4h RSI14가 30~50 구간에서 반등 조짐이 보이거나 4h MA20 지지 확인 시 매수 검토
- 15m RSI가 70 이상(단기 과매수)이면 단기 고점에 물릴 위험이 있으므로 해당 종목 진입을 스킵하라
- 거래대금이 극히 적거나 4h 추세가 불명확하면 관망

동적 리스크 관리 (ATR 기반):
- ATR%가 2.5% 초과(고변동성): stop_loss_pct = ATR%의 1.5~2배 (예: ATR 3% → stop 4.5~6%), weight_pct는 30 이하로 낮춰 리스크 제한
- ATR%가 1.5% ~ 2.5%(중변동성): stop_loss_pct = 2.0~3.5%, weight_pct 보통 수준
- ATR%가 1.5% 미만(저변동성 메이저): stop_loss_pct = 1.5~2.0%로 타이트하게, weight_pct 높게 배분 가능
- target_profit_pct는 stop_loss_pct의 2배 이상을 유지해 Risk/Reward 비율 ≥ 2:1 확보

반드시 아래 JSON 형식으로만 응답해 (다른 텍스트 없음):
{
  "market_summary": "현재 전체 시장 상황에 대한 분석 및 판단 근거를 2~3문장으로 요약.",
  "picks": [
    {
      "symbol":            "BTC/KRW",
      "score":             92,
      "weight_pct":        55,
      "reason":            "4h 20MA 지지 및 RSI 42 반등 확인. ATR 1.2%(저변동) — 타이트한 손절 적용",
      "target_profit_pct": 4.0,
      "stop_loss_pct":     1.8
    }
  ]
}

score 기준:
- 80점 이상: 강력한 진입 신호 — picks에 반드시 포함
- 79점 이하: 조건 미달 — picks에 절대 포함하지 말 것

weight_pct 기준:
- 가용 예산 대비 투자 비중 (예: 55 → 가용 예산의 55% 투자)
- 두 종목 합산 총합이 100을 넘지 않도록 설정
- score에 비례해 높은 점수 종목에 더 많은 비중, ATR%가 높은 종목은 비중을 낮춰 리스크 제한

규칙:
- market_summary는 관망을 선택했더라도 반드시 작성 (왜 아무것도 안 샀는지 이유 포함)
- target_profit_pct: 양수, stop_loss_pct의 2배 이상으로 설정 (R/R ≥ 2:1)
- stop_loss_pct: 양수, ATR% 기반으로 동적 결정 (범위: 1.5 ~ 6.0)
- [절대 규칙 1 - 언행일치] market_summary에서 '지지선 확인', '추세 전환', '진입 적합' 등 긍정 평가한 종목이 있다면 핑계 대지 말고 무조건(MUST) picks 배열에 매수 데이터로 포함해야 한다.
- [절대 규칙 2 - 관망의 조건] picks 배열을 []로 비울 것이라면, market_summary에도 "모든 코인이 과매수이거나 추세가 깨져서 전액 현금 관망한다"라고 철저히 부정적으로만 적어야 한다.
- [절대 규칙 3 - symbol 형식] symbol은 반드시 "코인명/KRW" 형태로 작성하라. (예: BTC/KRW, ETH/KRW)
- [절대 규칙 4 - 숫자 형식] 모든 숫자 필드는 % 기호나 +/- 부호 없이 순수 숫자만 적어라.
- [절대 규칙 5 - 동전주 금지] 현재가(price)가 **100 KRW 미만인 코인(동전주/엽전주)은 틱 단위 변동성이 너무 커서 리스크 관리가 불가능하므로, 아무리 지표가 좋아도 절대 picks 배열에 포함하지 말고 스킵**하라.

[올바른 응답 예시]
{
  "market_summary": "BTC가 4h 20MA 지지를 받고 있으며 ATR 1.2%로 저변동성. ETH는 ATR 3.1%로 고변동성이나 4h RSI 38 반등 확인. 두 종목 매수하되 ETH는 비중 축소.",
  "picks": [
    {"symbol": "BTC/KRW", "score": 90, "weight_pct": 60, "reason": "4h 20MA 지지, ATR 1.2%(저변동) — 타이트 손절 적용, 15m RSI 55(과매수 아님)", "target_profit_pct": 4.0, "stop_loss_pct": 1.8},
    {"symbol": "ETH/KRW", "score": 82, "weight_pct": 25, "reason": "4h RSI 38 반등, ATR 3.1%(고변동) — 손절 넓게, 비중 축소로 리스크 제한", "target_profit_pct": 7.0, "stop_loss_pct": 4.5}
  ]
}
"""

# ------------------------------------------------------------------
# 시스템 프롬프트 — SCALPING (1시간 봉, 공격적 모멘텀 단타)
# ------------------------------------------------------------------

_SCALPING_SYSTEM_PROMPT = """\
너는 1시간 봉 스캘핑·단타 전문 AI 퀀트 트레이더야.
보수적인 접근은 버려라. 작은 모멘텀이라도 포착되면 즉각 매수를 실행하는 것이 너의 역할이다.
단, 고정된 손절가(1.5%)는 버려라. 변동성(ATR%)에 맞는 동적 손절이 휩쏘를 방지하는 유일한 방법이다.

제공된 Top 코인의 1h/15m 지표, 변동성(ATR%), 24h 거래대금, 가용 예산을 분석해서 지금 당장 모멘텀 돌파 진입하기 좋은 코인을 최대 2개만 골라.
이미 유저가 보유 중인 코인은 반드시 제외해.

전략 기준:
- 1h RSI14가 55~70 구간에서 상승 모멘텀이 강하고 거래대금이 급증하면 돌파 매수
- RSI가 60 이상이더라도 거래대금이 폭발적으로 몰리며 추세를 탄다면 진입 가능
- 15m RSI가 75 이상(단기 극과매수)이면 이미 단기 고점일 가능성이 높으므로 해당 종목 스킵
- 15m MA20 위에서 가격이 지지받고 있으면 단기 진입 타점으로 유리
- 모멘텀이 완전히 없거나 거래대금이 매우 적을 때만 관망

동적 리스크 관리 (ATR 기반):
- ATR%가 2.5% 초과(고변동성): stop_loss_pct = ATR%의 1.5배 수준 (예: ATR 3% → stop 4.0~5.0%), weight_pct는 25 이하로 낮춰 리스크 제한
- ATR%가 1.5% ~ 2.5%(중변동성): stop_loss_pct = 1.8~2.5%, weight_pct 보통 수준
- ATR%가 1.5% 미만(저변동성): stop_loss_pct = 1.2~1.8%로 타이트하게, weight_pct 높게 배분 가능
- target_profit_pct는 stop_loss_pct의 1.5배 이상 유지 (단타이므로 R/R ≥ 1.5:1)

반드시 아래 JSON 형식으로만 응답해 (다른 텍스트 없음):
{
  "market_summary": "현재 전체 시장 상황에 대한 분석 및 판단 근거를 2~3문장으로 요약.",
  "picks": [
    {
      "symbol":            "ETH/KRW",
      "score":             88,
      "weight_pct":        50,
      "reason":            "1h RSI 62, 거래대금 급증하며 1h MA20 돌파. ATR 1.8%(중변동) — 동적 손절 적용, 15m RSI 60(과매수 아님)",
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
- score에 비례해 높은 점수 종목에 더 많은 비중, ATR%가 높은 종목은 비중을 낮춰 리스크 제한

규칙:
- market_summary는 관망을 선택했더라도 반드시 작성
- target_profit_pct: 양수, stop_loss_pct의 1.5배 이상으로 설정
- stop_loss_pct: 양수, ATR% 기반으로 동적 결정 (범위: 1.2 ~ 5.0)
- [절대 규칙 1 - 언행일치] market_summary 텍스트에서 특정 코인에 대해 '상승 모멘텀', '진입 유효' 등 긍정적인 평가를 했다면, 그 코인은 핑계 대지 말고 무조건(MUST) picks 배열에 포함해야 한다.
- [절대 규칙 2 - 관망의 조건] picks 배열을 []로 비우고 싶다면, market_summary에도 반드시 "모든 코인의 상태가 나빠서 전액 관망한다"라고 부정적으로만 적어야 한다.
- [절대 규칙 3 - symbol 형식] symbol은 반드시 "코인명/KRW" 형태로 작성하라. (예: ETH/KRW, SOL/KRW)
- [절대 규칙 4 - 숫자 형식] 모든 숫자 필드는 % 기호나 +/- 부호 없이 순수 숫자만 적어라.
- [절대 규칙 5 - 동전주 금지] 현재가(price)가 **100 KRW 미만인 코인(동전주/엽전주)은 틱 단위 변동성이 너무 커서 리스크 관리가 불가능하므로, 아무리 지표가 좋아도 절대 picks 배열에 포함하지 말고 스킵**하라.
- [공격적 실행] 조건에 부합하는 종목이 단 1개라도 있다면 주저하지 말고 즉시 picks에 담아라.

[올바른 응답 예시]
{
  "market_summary": "ETH가 1h MA20 돌파하며 거래대금 급증, ATR 1.8%(중변동). SOL은 1h RSI 66으로 모멘텀 강하나 15m RSI 76으로 단기 과매수 — SOL 스킵. ETH 단독 진입.",
  "picks": [
    {"symbol": "ETH/KRW", "score": 88, "weight_pct": 60, "reason": "1h RSI 63, 거래대금 급증하며 MA20 돌파. ATR 1.8%(중변동) — 동적 손절, 15m RSI 60(진입 적합)", "target_profit_pct": 3.0, "stop_loss_pct": 2.0}
  ]
}
"""

# ------------------------------------------------------------------
# 포지션 리뷰용 시스템 프롬프트 (SWING·SCALPING 공용)
# ------------------------------------------------------------------

_REVIEW_SYSTEM_PROMPT = """\
너는 보유 포지션을 입체적으로 관리하는 AI 포지션 매니저야.
각 코인의 수익률, 멀티 타임프레임 RSI·MA 지표, 그리고 변동성(ATR%)을 종합해 아래 3가지 액션 중 하나를 반드시 선택해라.

액션 정의:
- HOLD   : 현재 익절/손절 기준 그대로 유지 (추세 지속, 특별한 변화 없음)
- UPDATE : 익절/손절 기준을 현재 시장 상황에 맞게 적극 조정
           (예: 목표가 상향, 손절을 본절로 이동, ATR 변동성 급증 시 손절 확대)
- SELL   : 즉시 시장가 청산 (추세 반전 확인, 손절 임박, ATR 폭등으로 리스크 감당 불가)

반드시 아래 JSON 형식으로만 응답해 (다른 텍스트 없음):
{
  "reviews": [
    {
      "symbol":                "ETH/KRW",
      "action":                "SELL",
      "reason":                "1h MA20 하향 이탈, 15m RSI 25 급락 — 추세 반전 확인, 즉시 청산",
      "new_target_profit_pct": null,
      "new_stop_loss_pct":     null
    },
    {
      "symbol":                "XRP/KRW",
      "action":                "UPDATE",
      "reason":                "수익률 +2.5% 달성, ATR 1.4% 유지 — 목표 상향, 손절 본절로 이동",
      "new_target_profit_pct": 5.0,
      "new_stop_loss_pct":     0.0
    }
  ]
}

SELL 판단 기준 (아래 중 하나라도 해당하면 SELL):
- 1h RSI가 30 이하로 급락하거나 1h MA20을 하향 이탈
- 15m RSI가 25 이하로 급락해 단기 추세 붕괴 신호
- 현재 수익률이 손절 기준(-stop_loss_pct)에 근접하고 반등 여지 없음
- ATR%가 진입 시점 대비 크게 증가(변동성 폭등)해 현재 손절폭으로 리스크 감당 불가

UPDATE 판단 기준 (방어적 갱신):
- 수익률이 목표의 50% 이상 달성됐으면 손절을 본절(0%) 이상으로 이동해 수익을 보호
- ATR%가 상승 중이면 손절폭을 ATR의 1.5배 수준으로 확대해 휩쏘를 방지
- 추세가 강하게 유지되면 목표 익절가를 상향

규칙:
- action=SELL 시 new_target_profit_pct, new_stop_loss_pct 는 반드시 null 로 설정
- action=HOLD 시 new_target_profit_pct, new_stop_loss_pct 는 기존 값 그대로 반환
- action=UPDATE 시 새로운 값을 양수로 제시 (% 기호·부호 없이 순수 숫자)
- 확실한 SELL 근거가 없으면 HOLD를 선택해 (보수적 운용 우선)
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
        available_krw: float = 0.0,
    ) -> dict:
        """MarketDataManager 캐시 데이터를 기반으로 시장을 분석하고 최대 2개 코인을 픽한다.

        score ≥ 80 인 종목만 picks에 포함하며, weight_pct 는 가용 예산 대비 비중이다.

        Args:
            market_data:     MarketDataManager.get_all() 반환값.
            holding_symbols: 유저가 현재 감시 중인 코인 심볼 집합. AI 픽에서 자동 제외.
            trade_style:     "SWING" (4h 보수 스윙) 또는 "SCALPING" (1h 공격 단타).
            available_krw:   이번 사이클 가용 예산 (KRW). AI가 weight_pct 를 결정할 때 참조.

        Returns:
            {
              "market_summary": str,
              "picks": list[dict],  # score·weight_pct·reason·target/stop_loss_pct 포함
            }
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
        lines: list[str] = [f"# Top 코인 시장 데이터 (멀티 타임프레임)\n"]
        for symbol, data in market_data.items():
            price = data.get("price")
            chg   = data.get("change_pct")
            vol   = data.get("volume_krw")

            # 변동성 지표
            atr_pct = data.get("atr_pct")

            # 각 타임프레임 지표
            rsi14_4h  = data.get("rsi14")
            ma20_4h   = data.get("ma20")
            rsi14_1h  = data.get("rsi14_1h")
            ma20_1h   = data.get("ma20_1h")
            rsi14_15m = data.get("rsi14_15m")
            ma20_15m  = data.get("ma20_15m")

            price_str   = f"{format_krw_price(price)} KRW" if price    is not None else "N/A"
            atr_str     = f"{atr_pct:.2f}%"                if atr_pct  is not None else "N/A"
            chg_str     = f"{chg:+.2f}%"                   if chg      is not None else "N/A"
            vol_str     = f"{vol / 1e8:.1f}억"              if vol      is not None else "N/A"
            rsi4h_str   = f"{rsi14_4h:.1f}"                if rsi14_4h  is not None else "N/A"
            ma4h_str    = f"{format_krw_price(ma20_4h)}"   if ma20_4h   is not None else "N/A"
            rsi1h_str   = f"{rsi14_1h:.1f}"                if rsi14_1h  is not None else "N/A"
            ma1h_str    = f"{format_krw_price(ma20_1h)}"   if ma20_1h   is not None else "N/A"
            rsi15m_str  = f"{rsi14_15m:.1f}"               if rsi14_15m is not None else "N/A"
            ma15m_str   = f"{format_krw_price(ma20_15m)}"  if ma20_15m  is not None else "N/A"

            lines.append(
                f"- {symbol}: 현재가={price_str} | 변동성(ATR)={atr_str}"
                f" | 15m(RSI={rsi15m_str}, MA={ma15m_str})"
                f" | 1h(RSI={rsi1h_str}, MA={ma1h_str})"
                f" | 4h(RSI={rsi4h_str}, MA={ma4h_str})"
                f" | 24h변동={chg_str} | 24h대금={vol_str}"
            )

        if holding_symbols:
            lines.append(
                f"\n# 이미 보유 중 — 반드시 제외: {', '.join(sorted(holding_symbols))}"
            )

        # ── 가용 예산 컨텍스트 추가 (AI가 weight_pct 결정 시 참조) ──
        if available_krw > 0:
            lines.append(f"\n# 이번 사이클 가용 예산: {available_krw:,.0f} KRW")

        user_prompt = "\n".join(lines)

        # ── [AI DEBUG - INPUT] 디버깅용 입력 로그 ────────────────────
        logger.info(
            "[AI DEBUG - INPUT] analyze_market 프롬프트 (style=%s, budget=%.0f):\n%s",
            trade_style, available_krw, user_prompt,
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
                max_tokens=700,
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

            symbol = str(p.get("symbol", "")).strip()

            # symbol 형식 방어
            if "/" not in symbol:
                symbol = f"{symbol.upper()}/KRW"
            else:
                base, quote = symbol.split("/", 1)
                symbol = f"{base.upper()}/{quote.upper()}"

            if not symbol.endswith("/KRW"):
                logger.warning("AITraderService: 비KRW 마켓 심볼 무시: %s", symbol)
                continue
            if symbol in holding_symbols:
                continue

            # score 파싱 및 80점 미만 필터
            try:
                score = max(0, min(100, int(p.get("score", 0) or 0)))
            except (ValueError, TypeError):
                score = 0

            if score < 80:
                logger.info(
                    "AITraderService: score 미달로 제외 (score=%d < 80): %s", score, symbol
                )
                continue

            # weight_pct 파싱
            try:
                weight_pct = max(0.0, float(p.get("weight_pct", 0) or 0))
            except (ValueError, TypeError):
                weight_pct = 0.0

            validated.append(
                {
                    "symbol":     symbol,
                    "score":      score,
                    "weight_pct": weight_pct,
                    "reason":     str(p.get("reason", "")),
                    # '%' · '+' 기호 및 음수 방어
                    "target_profit_pct": _safe_pct(p.get("target_profit_pct", 3.0), default=3.0),
                    "stop_loss_pct":     _safe_pct(p.get("stop_loss_pct",     2.0), default=2.0),
                }
            )
            if len(validated) == 2:
                break

        logger.info(
            "AITraderService 분석 완료 (style=%s): %d 개 픽 %s",
            trade_style,
            len(validated),
            [(v["symbol"], v["score"], v["weight_pct"]) for v in validated],
        )
        return {"market_summary": market_summary, "picks": validated}

    async def review_positions(
        self,
        positions_data: list[dict],
        market_data: dict[str, dict],
        trade_style: str = "SWING",
    ) -> list[dict]:
        """현재 보유 포지션을 AI가 재검토해 HOLD / UPDATE / SELL 을 결정한다.

        Args:
            positions_data: 보유 포지션 리스트.
                            각 항목: {
                                "symbol", "buy_price", "current_price",
                                "profit_pct", "target_profit_pct", "stop_loss_pct"
                            }
            market_data: MarketDataManager.get_all() 반환값.
            trade_style: "SWING" 또는 "SCALPING".

        Returns:
            리뷰 리스트:
            [{"symbol", "action", "new_target_profit_pct", "new_stop_loss_pct", "reason"}, ...]
            action 은 "HOLD" | "UPDATE" | "SELL" 중 하나.
            SELL 시 new_target/new_stop 은 None.
        """
        if not positions_data:
            return []

        if not settings.openai_api_key:
            logger.error("AITraderService: OPENAI_API_KEY 미설정")
            return []

        is_scalping = trade_style == "SCALPING"

        # ── 유저 프롬프트 구성 ────────────────────────────────────────
        lines: list[str] = ["# 현재 보유 포지션 (멀티 타임프레임 + ATR 변동성)\n"]
        for pos in positions_data:
            symbol     = pos["symbol"]
            buy_price  = pos["buy_price"]
            cur_price  = pos["current_price"]
            profit_pct = pos["profit_pct"]
            tgt        = pos["target_profit_pct"]
            sl         = pos["stop_loss_pct"]

            mdata     = market_data.get(symbol, {})
            atr_pct   = mdata.get("atr_pct")
            rsi14_4h  = mdata.get("rsi14")
            ma20_4h   = mdata.get("ma20")
            rsi14_1h  = mdata.get("rsi14_1h")
            ma20_1h   = mdata.get("ma20_1h")
            rsi14_15m = mdata.get("rsi14_15m")

            atr_str    = f"{atr_pct:.2f}%"               if atr_pct   is not None else "N/A"
            rsi4h_str  = f"{rsi14_4h:.1f}"               if rsi14_4h  is not None else "N/A"
            ma4h_str   = f"{format_krw_price(ma20_4h)}"  if ma20_4h   is not None else "N/A"
            rsi1h_str  = f"{rsi14_1h:.1f}"               if rsi14_1h  is not None else "N/A"
            ma1h_str   = f"{format_krw_price(ma20_1h)}"  if ma20_1h   is not None else "N/A"
            rsi15m_str = f"{rsi14_15m:.1f}"              if rsi14_15m is not None else "N/A"

            lines.append(
                f"- {symbol}: "
                f"매수가={format_krw_price(buy_price)}KRW  현재가={format_krw_price(cur_price)}KRW  "
                f"수익률={profit_pct:+.2f}%  목표익절={tgt:.1f}%  손절기준={sl:.1f}%"
                f" | 변동성(ATR)={atr_str}"
                f" | 15m(RSI={rsi15m_str})"
                f" | 1h(RSI={rsi1h_str}, MA={ma1h_str})"
                f" | 4h(RSI={rsi4h_str}, MA={ma4h_str})"
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
                temperature=0.2,
                max_tokens=600,
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
            if not symbol or symbol not in valid_symbols:
                continue

            # MAINTAIN(구버전) → HOLD 정규화 포함
            raw_action = str(r.get("action", "HOLD")).upper()
            if raw_action == "MAINTAIN":
                raw_action = "HOLD"
            if raw_action not in ("HOLD", "UPDATE", "SELL"):
                raw_action = "HOLD"

            pos_defaults = next(
                (p for p in positions_data if p["symbol"] == symbol), {}
            )

            if raw_action == "SELL":
                # SELL: new_target / new_stop 은 None
                validated.append(
                    {
                        "symbol": symbol,
                        "action": "SELL",
                        "new_target_profit_pct": None,
                        "new_stop_loss_pct":     None,
                        "reason": str(r.get("reason", "")),
                    }
                )
            else:
                # HOLD / UPDATE: 양수 강제 보정
                validated.append(
                    {
                        "symbol": symbol,
                        "action": raw_action,
                        "new_target_profit_pct": abs(
                            float(
                                r.get("new_target_profit_pct",
                                      pos_defaults.get("target_profit_pct", 3.0))
                                or pos_defaults.get("target_profit_pct", 3.0)
                            )
                        ),
                        "new_stop_loss_pct": abs(
                            float(
                                r.get("new_stop_loss_pct",
                                      pos_defaults.get("stop_loss_pct", 2.0))
                                or pos_defaults.get("stop_loss_pct", 2.0)
                            )
                        ),
                        "reason": str(r.get("reason", "")),
                    }
                )

        sell_count   = sum(1 for v in validated if v["action"] == "SELL")
        update_count = sum(1 for v in validated if v["action"] == "UPDATE")
        hold_count   = sum(1 for v in validated if v["action"] == "HOLD")
        logger.info(
            "AITraderService(review) 완료 (style=%s): %d 개 포지션 검토 "
            "(SELL=%d, UPDATE=%d, HOLD=%d)",
            trade_style, len(validated), sell_count, update_count, hold_count,
        )
        return validated
