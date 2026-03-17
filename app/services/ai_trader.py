"""
AITraderService: Anthropic claude-sonnet-4-6 기반 코인 종목 분석·픽 서비스.

백테스트 3종 LLM 벤치마크 결과 Claude가 지시사항 준수율·수익률 모두 1위로 채택.
추세 돌파 스나이퍼 v5: RSI 50~65 상승 모멘텀 돌파 타점, 손익비 R:R ≥ 1.5 강제.
(구 v4 RSI 30~45 역추세 전략 폐기 — 5개월 백테스트 승률 50% / 손익비 악화로 실패)

analyze_market() 호출 흐름:
  1. MarketDataManager 캐시 데이터 + 가용 예산 → 유저 프롬프트 텍스트로 직렬화
  2. claude-sonnet-4-6 에 _CORE_SNIPER_PROMPT + 유저 프롬프트 전송 (JSON 출력 강제)
  3. 응답 JSON 파싱 → score ≥ 90 필터 · stop ≤ 5% 검증 · R:R ≥ 1.5 강제 · 최대 2개 제한
  4. trade_style에 따라 weight_pct 코드 레벨에서 강제 덮어쓰기 후 반환

반환 형식 (analyze_market):
  {
    "market_summary":  "현재 시장 전반 분석 2~3문장.",
    "picks": [
      {
        "symbol":            "BTC/KRW",
        "score":             92,           # 0~100점 (90 이상만 포함)
        "weight_pct":        20.0,         # SNIPER=20.0 / BEAST=70.0 (AI 응답 무시, 코드 고정)
        "reason":            "4h RSI 57 상승 모멘텀, MA20 돌파 지지 확인",
        "target_profit_pct": 6.0,          # stop × 1.5 이상 보장 (코드 레벨 강제)
        "stop_loss_pct":     3.5,          # 최대 5.0% 하드 상한 (코드 레벨 강제)
      },
      ...
    ]
  }

review_positions() 호출 흐름:
  1. 현재 보유 포지션 데이터 + MarketDataManager 캐시 → 프롬프트 구성
  2. claude-sonnet-4-6 에 _REVIEW_SYSTEM_PROMPT + 유저 프롬프트 전송
  3. 응답 JSON 파싱 → HOLD/UPDATE/SELL 리뷰 리스트 반환

반환 형식 (review_positions):
  [
    {
      "symbol":              "BTC/KRW",
      "action":              "HOLD" | "UPDATE" | "SELL",
      "new_target_profit_pct": 5.0,   # SELL 시 None
      "new_stop_loss_pct":     7.5,   # SELL 시 None
      "reason":              "...",
    },
    ...
  ]

trade_style 분기 (비중 결정 전용 — 진입 로직은 공통):
  SNIPER — 🛡️ 인텔리전트 스나이퍼 (기본/안전 모드): weight_pct = 20.0 고정
  BEAST  — 🔥 야수의 심장 (공격 모드):              weight_pct = 70.0 고정
  진입 타점 판단 로직(시스템 프롬프트·RSI 조건·손절 기준)은 두 모드 모두 동일.
"""
from __future__ import annotations

import json
import logging

import anthropic

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
# 시스템 프롬프트 — 추세 돌파 스나이퍼 v5 (SNIPER·BEAST 공용)
# 전략: RSI 50~65 상승 모멘텀 돌파 + 손익비 R:R ≥ 1.5 수학적 강제
# 비중(weight_pct)은 Python 코드 레벨에서 덮어씀 (AI 응답값 무시).
# ------------------------------------------------------------------

_CORE_SNIPER_PROMPT = """\
너는 4시간 봉 기반의 '추세 돌파 스나이퍼' 트레이더야.
상승 모멘텀이 막 시작되는 타점을 포착해 명확한 손익비(R:R ≥ 1.5)로 진입하는 것이 핵심이다.
제공된 코인들의 멀티 타임프레임(4h/1h/15m) RSI·MA·ATR 지표를 분석해서
지금 당장 매수하기 가장 좋은 코인을 최대 2개만 골라.
이미 유저가 보유 중인 코인은 반드시 제외해.
확신이 없으면 picks 배열을 비워서 관망해도 된다 — 관망 자체가 최고의 전략일 수 있다.

반드시 아래 JSON 형식으로만 응답해 (다른 텍스트 없음):
{
  "market_summary": "시장 분석 2~3문장.",
  "picks": [
    {
      "symbol":            "BTC/KRW",
      "score":             91,
      "reason":            "4h RSI 57 상승 모멘텀 진입, MA20 돌파 직후 지지 확인. ATR 1.8% — stop 3.5% / target 6.0% (R:R 1.71)",
      "target_profit_pct": 6.0,
      "stop_loss_pct":     3.5
    }
  ]
}

[핵심 매매 원칙 — 추세 돌파 스나이퍼 v5]

1. BTC 시장 국면 필터 (최우선 — 모든 원칙보다 우선):
   - BTC/KRW가 4h MA20 아래에 있거나, BTC 4h RSI14 < 45이면:
     → picks 배열을 반드시 완전히 비울 것 (어떤 알트코인도 절대 진입 금지)
   - BTC 4h RSI14가 45~55 사이라면 score 95 이상의 확실한 종목만 고려하고 아니면 관망
   - BTC 4h RSI14 ≥ 55이고 MA20 위에 있을 때만 정상 진입 가능

2. 진입 타점 — 상승 모멘텀 돌파 (Momentum Breakout):
   - [절대 금지] 4h RSI14 < 50인 종목: '떨어지는 칼날' — 어떤 이유로도 절대 진입 금지
   - [최우선] 4h RSI14 50~65 구간: "이제 막 모멘텀이 살아나는 구간"
     → 현재가가 4h MA20을 방금 돌파했거나, MA20 위에서 눌렸다가 재상승 중인 종목 최우선
     → 1h RSI가 50 이상으로 올라서며 단기 모멘텀을 동반하는 종목에 가산점
   - 거래대금(24h) 상위권 종목 중 추세가 명확한 것만 선택 (유동성 + 추세 동반 필수)
   - [진입 금지] 과매수 구간(4h RSI14 > 70): 이미 많이 오른 종목 — 되돌림 위험
   - score 90 이상만 진입 (절대 90 미만 금지 — 진입 빈도를 낮춰야 한다)

3. 손절폭 — 타이트한 설계 (하드 상한 5.0%):
   - stop_loss_pct = ATR% × 1.5~2배 수준으로 설정
   - [하드 상한] stop_loss_pct는 절대 5.0%를 초과할 수 없음
     → 5%를 넘어야 하는 종목은 변동성 과대 잡알트로 판단해 반드시 패스
   - 예시: ATR 2.0% → stop 3.0~4.0% / ATR 2.5% → stop 4.0~5.0%

4. 목표가 — 수학적 손익비 1.5:1 이상 강제 (R:R ≥ 1.5):
   - [필수 규칙] target_profit_pct ≥ stop_loss_pct × 1.5
     (예: 손절 3.0% → 익절 최소 4.5% / 손절 4.0% → 익절 최소 6.0%)
   - 직전 저항선을 고려해 현실적인 도달 가능 범위 내에서 설정
   - R:R 1.5 미달이 되는 경우(저항이 너무 가까움) 반드시 패스

5. 일반 규칙:
   - symbol은 "코인명/KRW" 형태 (예: BTC/KRW)
   - 모든 숫자 필드는 순수 숫자만 (%, +/- 없음)
   - 현재가 100 KRW 미만 동전주는 스킵
   - market_summary는 관망 시에도 반드시 작성 (관망 이유 명확히 포함)
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

_CLAUDE_MODEL = "claude-sonnet-4-6"  # 벤치마크 1위 채택 모델


class AITraderService:
    """Anthropic Claude를 통해 코인 종목을 분석하고 매수 픽을 반환하는 서비스.

    Attributes:
        _client: anthropic.AsyncAnthropic 클라이언트 인스턴스.
    """

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    @staticmethod
    def _empty_analysis(summary: str) -> dict:
        """분석 불가 또는 오류 시 반환하는 빈 결과 딕셔너리."""
        return {"market_summary": summary, "picks": []}

    # trade_style → weight_pct 강제 매핑 (코드 레벨 하드 룰)
    _WEIGHT_MAP: dict[str, float] = {
        "SNIPER": 20.0,  # 🛡️ 인텔리전트 스나이퍼 — 안전 모드
        "BEAST":  70.0,  # 🔥 야수의 심장       — 공격 모드
    }

    async def analyze_market(
        self,
        market_data: dict[str, dict],
        holding_symbols: set[str],
        trade_style: str = "SNIPER",
        available_krw: float = 0.0,
    ) -> dict:
        """MarketDataManager 캐시 데이터를 기반으로 시장을 분석하고 최대 2개 코인을 픽한다.

        score ≥ 90 인 종목만 picks에 포함. weight_pct는 trade_style에 따라 코드 레벨에서
        강제 덮어쓰기한다 (AI 응답값 무시 — SNIPER=20%, BEAST=70%).

        Args:
            market_data:     MarketDataManager.get_all() 반환값.
            holding_symbols: 유저가 현재 감시 중인 코인 심볼 집합. AI 픽에서 자동 제외.
            trade_style:     "SNIPER" (기본/안전 모드) 또는 "BEAST" (공격 모드).
                             진입 타점 로직은 동일, 비중만 차이남.
            available_krw:   이번 사이클 가용 예산 (KRW). AI 유저 프롬프트에 컨텍스트 제공.

        Returns:
            {
              "market_summary": str,
              "picks": list[dict],  # score·weight_pct(고정값)·reason·target/stop_loss_pct 포함
            }
        """
        if not market_data:
            logger.warning("AITraderService: 마켓 데이터 없음 — MarketDataManager 캐시 초기화 대기 중")
            return self._empty_analysis(
                "마켓 데이터 캐시가 아직 초기화되지 않아 분석을 수행하지 못했습니다."
            )

        if not settings.anthropic_api_key:
            logger.error("AITraderService: ANTHROPIC_API_KEY 미설정")
            return self._empty_analysis("Anthropic API 키가 설정되지 않아 분석을 수행하지 못했습니다.")

        # ── trade_style → weight_pct 매핑 (AI 응답 무시, 코드 레벨 강제 고정) ──
        forced_weight: float = self._WEIGHT_MAP.get(trade_style.upper(), 20.0)

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
            "[AI DEBUG - INPUT] analyze_market 프롬프트 (style=%s weight=%.0f%%, budget=%.0f):\n%s",
            trade_style, forced_weight, available_krw, user_prompt,
        )

        # ── Anthropic Claude 호출 (단일 프롬프트 — 두 모드 공통) ──────
        try:
            response = await self._client.messages.create(
                model=_CLAUDE_MODEL,
                system=_CORE_SNIPER_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
                temperature=0.3,
                max_tokens=700,
            )
        except Exception as exc:
            logger.error("AITraderService Anthropic 호출 실패: %s", exc)
            return self._empty_analysis(f"AI 분석 중 오류가 발생했습니다: {exc}")

        raw = response.content[0].text if response.content else "{}"

        # ── [AI DEBUG - OUTPUT] 디버깅용 원본 응답 로그 ──────────────
        logger.info(
            "[AI DEBUG - OUTPUT] analyze_market 원본 응답 (style=%s):\n%s",
            trade_style, raw,
        )

        # ── 응답 파싱 및 안전 검증 (마크다운 펜스 제거 후 JSON 파싱) ──
        try:
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            result = json.loads(text.strip())
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

            # 스나이퍼 전략 v4 하드 하한: score 90 미만 하드 차단
            if score < 90:
                logger.info(
                    "AITraderService: score 미달로 제외 (score=%d < 90): %s", score, symbol
                )
                continue

            # stop_loss_pct 검증: 하드 상한 5.0% (추세 돌파 전략 v5 — 타이트한 손절)
            # 구 전략(역추세): 7~9% 허용 → 신 전략(모멘텀 돌파): ATR×1.5~2배, 최대 5%
            raw_stop = _safe_pct(p.get("stop_loss_pct", 3.5), default=3.5)
            if raw_stop > 5.0:                    # 하드 상한 5% 초과 → 손익비 구조 붕괴 차단
                logger.info(
                    "AITraderService: 넓은 손절 스킵 (stop=%.1f%% > 5%%): %s", raw_stop, symbol
                )
                continue
            stop_loss_pct = raw_stop

            # target_profit_pct: 손익비 R:R ≥ 1.5 코드 레벨 강제 (AI 지시사항 이중 방어)
            # AI가 stop × 1.5 미만 target을 내놓을 경우 자동 보정해 손익비 구조를 유지
            raw_target      = _safe_pct(p.get("target_profit_pct", 5.0), default=5.0)
            min_target      = round(stop_loss_pct * 1.5, 2)   # R:R 1.5:1 최솟값
            target_profit_pct = max(raw_target, min_target)

            # weight_pct: trade_style 기반 코드 레벨 강제 덮어쓰기 (AI 응답값 무시)
            # SNIPER=20% / BEAST=70% — 어떤 경우에도 이 값만 허용
            weight_pct: float = forced_weight

            validated.append(
                {
                    "symbol":            symbol,
                    "score":             score,
                    "weight_pct":        weight_pct,
                    "reason":            str(p.get("reason", "")),
                    "target_profit_pct": target_profit_pct,   # R:R ≥ 1.5 보장 후 삽입
                    "stop_loss_pct":     stop_loss_pct,       # 최대 5.0% 보장 후 삽입
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
        trade_style: str = "SNIPER",
    ) -> list[dict]:
        """현재 보유 포지션을 AI가 재검토해 HOLD / UPDATE / SELL 을 결정한다.

        Args:
            positions_data: 보유 포지션 리스트.
                            각 항목: {
                                "symbol", "buy_price", "current_price",
                                "profit_pct", "target_profit_pct", "stop_loss_pct"
                            }
            market_data: MarketDataManager.get_all() 반환값.
            trade_style: "SNIPER" 또는 "BEAST" (포지션 리뷰 로직은 두 모드 동일).

        Returns:
            리뷰 리스트:
            [{"symbol", "action", "new_target_profit_pct", "new_stop_loss_pct", "reason"}, ...]
            action 은 "HOLD" | "UPDATE" | "SELL" 중 하나.
            SELL 시 new_target/new_stop 은 None.
        """
        if not positions_data:
            return []

        if not settings.anthropic_api_key:
            logger.error("AITraderService: ANTHROPIC_API_KEY 미설정")
            return []

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

        # ── Anthropic Claude 호출 ─────────────────────────────────────
        try:
            response = await self._client.messages.create(
                model=_CLAUDE_MODEL,
                system=_REVIEW_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
                temperature=0.2,
                max_tokens=600,
            )
        except Exception as exc:
            logger.error("AITraderService(review) Anthropic 호출 실패: %s", exc)
            return []

        raw = response.content[0].text if response.content else "{}"
        logger.debug("AITraderService(review) 응답: %s", raw)

        # ── 응답 파싱 및 안전 검증 (마크다운 펜스 제거 후 JSON 파싱) ──
        try:
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            result = json.loads(text.strip())
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
            "AITraderService(review) 완료 (mode=%s): %d 개 포지션 검토 "
            "(SELL=%d, UPDATE=%d, HOLD=%d)",
            trade_style, len(validated), sell_count, update_count, hold_count,
        )
        return validated
