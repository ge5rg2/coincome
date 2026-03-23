"""
AITraderService: Anthropic claude-sonnet-4-6 기반 코인 종목 분석·픽 서비스.

백테스트 3종 LLM 벤치마크 결과 Claude가 지시사항 준수율·수익률 모두 1위로 채택.

── SWING 엔진 (4h 봉 기반, 듀얼 전략) ───────────────────────────────────────────
  전략A (추세 돌파 v7): Close>MA50 + RSI 55~70 — 상승장 전용, TP 6% / SL 4% (R:R 1.5:1)
    백테스트 결과: 승률 44.6% / MDD -19.1% / ROI +91.4% (알트코인 집중)
  전략B (낙폭과대 반등 Reversal v2): Close<MA50 + RSI<25 — 하락·혼조장 전용, TP 3% / SL 2.5% (R:R 1.2:1)
    백테스트 결과: 승률 목표 ≥55% / MDD 억제 우선 (짧은 기술적 반등 스나이핑)

── SCALPING 엔진 (1h 봉 기반, 모멘텀 단타) ──────────────────────────────────────
  진입 조건: Close > 1h MA20 AND 1h RSI 60~75
  목표가 TP 2.0% / 손절 SL 1.5% (R:R 1.33:1, 하드 고정)
    백테스트 결과: 승률 51.6% / MDD -8.3% / ROI +62.3% (fast_backtest_scalping v1)

BLACKLIST(8개): BTC/ETH/XRP/DOGE/ADA/SOL/SUI/PEPE — Python 단에서 AI에 데이터 미전달 (토큰 절약 + 휩쏘 원천 차단).

analyze_market() 호출 흐름:
  1. MarketDataManager 캐시 데이터 + 가용 예산 → 블랙리스트 필터링 후 유저 프롬프트 직렬화
  2. claude-sonnet-4-6 에 engine_type 별 프롬프트 전송 (JSON 출력 강제)
     SWING → _CORE_SWING_PROMPT (4h MA50 포함)
     SCALPING → _CORE_SCALPING_PROMPT (1h MA20 기준)
  3. 응답 JSON 파싱 → score ≥ 90 필터
     SWING:    stop ≤ 5% 하드 상한 · R:R ≥ 1.2 최소 보정 · 최대 2개 제한
     SCALPING: stop ≤ 2% 하드 상한 · R:R ≥ 1.3 (min_target = stop × 1.3) · 최대 2개 제한
  4. weight_pct는 호출자(ai_manager)가 engine별 설정값을 직접 주입 (AI 응답 무시)

반환 형식 (analyze_market):
  {
    "market_summary":  "현재 시장 전반 분석 2~3문장.",
    "picks": [
      {
        "symbol":            "LINK/KRW",
        "score":             92,           # 0~100점 (90 이상만 포함)
        "weight_pct":        20.0,         # 호출자가 주입한 engine별 비중 (AI 응답 무시)
        "reason":            "[전략A 추세돌파] 4h RSI 62 모멘텀, MA50 위 상승 추세 확인",
        "target_profit_pct": 6.0,          # SWING: stop × 1.2 이상 / SCALPING: stop × 1.3 이상
        "stop_loss_pct":     4.0,          # SWING: 최대 5.0% / SCALPING: 최대 2.0%
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
      "symbol":              "LINK/KRW",
      "action":              "HOLD" | "UPDATE" | "SELL",
      "new_target_profit_pct": 5.0,   # SELL 시 None
      "new_stop_loss_pct":     7.5,   # SELL 시 None
      "reason":              "...",
    },
    ...
  ]

engine_type 분기:
  SWING    — 📊 4h 듀얼 스윙 (추세돌파A + 낙폭반등B), weight_pct 호출자 주입
  SCALPING — ⚡ 1h 모멘텀 단타, TP 2.0% / SL 1.5% 하드 고정, weight_pct 호출자 주입
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
# SWING 엔진 시스템 프롬프트 — 전천후 듀얼 전략 (4h 봉, 알트코인 전용)
# 전략A 추세 돌파: Close > MA50 + RSI 55~70 / TP 6.0% / SL 4.0% (R:R 1.5:1)
#   백테스트 v7: 승률 44.6% / MDD -19.1% / ROI +91.4%
# 전략B 낙폭과대 반등: Close < MA50 + RSI < 25 / TP 3.0% / SL 2.5% (R:R 1.2:1)
#   백테스트 Reversal v2: 승률 목표 ≥55% / MDD 억제 (하락장 서브 전략)
# 비중(weight_pct)은 Python 코드 레벨에서 덮어씀 (AI 응답값 무시).
# BTC/ETH/XRP/DOGE 등 메이저 코인은 Python 단에서 이미 BLACKLIST 처리되어
# 이 프롬프트에 도달하는 데이터에는 포함되지 않음 (토큰 낭비·휩쏘 원천 차단).
# ------------------------------------------------------------------

_CORE_SWING_PROMPT = """\
너는 4시간 봉 기반의 '전천후 듀얼 엔진 트레이더'야.
시장 상황에 따라 두 가지 전략(A: 추세 돌파 / B: 낙폭과대 반등) 중 하나를 선택해 진입한다.
BTC/ETH/XRP/DOGE/ADA/SOL/SUI/PEPE 등 무거운 메이저 코인은 이미 시스템 레벨에서 제외되었으므로 제공된 알트코인들만 분석해.
이미 유저가 보유 중인 코인은 반드시 제외해.
확신이 없으면 picks 배열을 비워서 관망해도 된다 — 관망 자체가 최고의 전략일 수 있다.

반드시 아래 JSON 형식으로만 응답해 (다른 텍스트 없음):
{
  "market_summary": "시장 분석 2~3문장.",
  "picks": [
    {
      "symbol":            "LINK/KRW",
      "score":             92,
      "reason":            "[전략A 추세돌파] 4h RSI 62 모멘텀 확인, MA50 위 장기 상승 추세. ATR 2.0% — stop 4.0% / target 6.0% (R:R 1.5)",
      "target_profit_pct": 6.0,
      "stop_loss_pct":     4.0
    }
  ]
}

[핵심 매매 원칙 — 전천후 듀얼 엔진 (알트코인 전용)]

1. BTC 시장 국면 필터 (최우선 — 모든 원칙보다 우선):
   ※ BTC 데이터는 입력 섹션 "# BTC 시장 국면" 에 실제 수치로 별도 제공된다.
     데이터가 없는데도 BTC 수치를 지어내는 것은 엄격히 금지 — 반드시 제공된 값만 사용.
   - BTC 4h MA20 아래이거나, BTC 4h RSI14 < 45이면:
     → 전략A(추세 돌파) 완전 금지. 단, 전략B(낙폭과대 반등)는 RSI < 25 조건이 충족된 종목에 한해 허용.
   - BTC 4h RSI14가 45~55 사이라면 전략A는 score 95 이상의 확실한 종목만 고려, 아니면 관망.
   - BTC 4h RSI14 ≥ 55이고 MA20 위에 있을 때 전략A 정상 진입 가능.
   ※ BTC 자체는 절대 픽하지 말 것.
   ※ market_summary 와 reason 필드에 "BTC"라는 단어를 절대 사용하지 말 것.
     "시장 국면 호조", "전반적 추세 상승" 같은 간접 표현으로만 서술할 것.

2. 진입 전략 선택 (A / B 중 하나 — 같은 종목에 중복 적용 금지):

   ▶ 전략A — 추세 돌파 (Momentum Breakout, 상승장 전용):
   - [진입 필수 조건] 4h RSI14 55~70 구간 AND 현재가 > 4h MA50 (장기 상승 추세 내 위치)
   - [진입 금지] 4h RSI14 < 55: 모멘텀 불확실 구간 — 절대 진입 금지
   - [진입 금지] 현재가 < 4h MA50: 중장기 하락 추세 — 반드시 패스 (휩쏘 위험 극대)
   - [진입 금지] 과매수 구간(4h RSI14 > 70): 이미 많이 오른 종목 — 되돌림 위험
   - 현재가가 4h MA20을 방금 돌파했거나, MA20 위에서 눌렸다가 재상승 중인 종목 최우선
   - 1h RSI가 55 이상으로 올라서며 단기 모멘텀을 동반하는 종목에 가산점
   - 거래대금(24h) 상위권 종목 중 추세가 명확한 것만 선택 (유동성 + 추세 동반 필수)
   - reason 태그: "[전략A 추세돌파]" 로 시작 (예: "[전략A 추세돌파] 4h RSI 62, MA50 위 상승 추세. TP 6.0% / SL 4.0%")

   ▶ 전략B — 낙폭과대 반등 (Oversold Reversal, 하락·혼조장 전용):
   - [진입 필수 조건] 4h RSI14 < 25 (극단적 투매/공포 구간) AND 현재가 < 4h MA50 (하락 추세 확인)
   - RSI가 20 이하로 내려갈수록 반등 강도 높을 가능성 증가 → 가산점
   - 거래량이 평소 대비 급증한 경우 (투매 피크 신호) 가산점
   - [진입 금지] 4h RSI14 ≥ 25: 이미 과매도 구간 탈출 중 — 적정 타점 아님
   - [진입 금지] 현재가 > 4h MA50: 하락 추세 미확인 — 전략B 진입 기준 미충족
   - 짧은 기술적 반등만 노리므로 목표가를 욕심내지 말 것 (3.0~4.0% 범위 유지)
   - reason 태그: "[전략B 역추세]" 로 시작 (예: "[전략B 역추세] 4h RSI 22 극단적 공포, MA50 하단. TP 3.0% / SL 2.5%")

   ※ 두 조건은 상호 배타적 (MA50 위·아래 + RSI 기준이 겹칠 수 없음) — 중복 진입 불가
   ※ score 90 이상만 진입 (절대 90 미만 금지 — 진입 빈도를 낮춰야 한다)
   ※ 현재가 100 KRW 미만 동전주는 전략 불문 스킵

3. 손절폭 (선택한 전략에 따라 다르게 적용):

   ▶ 전략A 진입 시 — 손절 기본 4.0% (하드 상한 5.0%):
   - 기본 가이드: stop_loss_pct = 4.0% (백테스트 v7 기준, R:R 1.5:1)
   - stop_loss_pct = ATR% × 1.5~2배 수준으로 동적 조정 가능
   - [하드 상한] stop_loss_pct 절대 5.0% 초과 불가 → 5% 초과 종목은 고변동성 잡알트로 판단해 패스

   ▶ 전략B 진입 시 — 손절 기본 2.5% (하드 상한 3.0%):
   - 기본 가이드: stop_loss_pct = 2.5% (백테스트 Reversal v2 기준, R:R 1.2:1)
   - [하드 상한] stop_loss_pct 절대 3.0% 초과 불가 → 짧은 반등 전략 특성상 타이트한 손절 필수
   - 예시: stop 2.5% → target 최소 3.0% (R:R 1.2:1)

4. 목표가 (선택한 전략에 따라 다르게 적용):

   ▶ 전략A 진입 시 — 목표 기본 6.0% (R:R ≥ 1.5 강제):
   - 기본 가이드: target_profit_pct = 6.0% (백테스트 v7 기준값)
   - [필수 규칙] target_profit_pct ≥ stop_loss_pct × 1.5
     (예: 손절 4.0% → 익절 최소 6.0%)
   - 직전 저항선을 고려해 현실적인 도달 가능 범위 내에서 설정

   ▶ 전략B 진입 시 — 목표 기본 3.0% (R:R ≥ 1.2 강제):
   - 기본 가이드: target_profit_pct = 3.0% (백테스트 Reversal v2 기준값)
   - [필수 규칙] target_profit_pct ≥ stop_loss_pct × 1.2
     (예: 손절 2.5% → 익절 최소 3.0%)
   - 기술적 반등만 노리므로 목표를 3.0~4.0% 범위로 유지할 것 (욕심 금지)

5. 일반 규칙:
   - symbol은 "코인명/KRW" 형태 (예: LINK/KRW)
   - symbol은 반드시 "# Top 코인 시장 데이터" 목록에 존재하는 심볼만 사용할 것.
     목록에 없는 코인명을 지어내는 것은 엄격히 금지 — 모르면 관망(picks=[])
   - 모든 숫자 필드는 순수 숫자만 (%, +/- 없음)
   - reason 필드에 반드시 "[전략A 추세돌파]" 또는 "[전략B 역추세]" 태그로 시작할 것
   - market_summary는 관망 시에도 반드시 작성 (관망 이유 및 현재 채택 전략 명확히 포함)
   - market_summary에 특정 코인명(BTC·ETH 포함)을 직접 나열하지 말 것.
     "시장 국면", "전반적 추세", "알트코인 전체 분위기" 같은 총평 표현으로만 서술할 것.
"""

# ------------------------------------------------------------------
# SCALPING 엔진 시스템 프롬프트 — 1h 모멘텀 단타 (알트코인 전용)
# 진입 조건: Close > 1h MA20 AND 1h RSI 60~75
# 목표가 TP 2.0% / 손절 SL 1.5% 하드 고정 (R:R 1.33:1)
#   백테스트 v1: 승률 51.6% / MDD -8.3% / ROI +62.3%
# 비중(weight_pct)은 Python 코드 레벨에서 덮어씀 (AI 응답값 무시).
# BTC/ETH/XRP/DOGE 등 메이저 코인은 Python 단에서 이미 BLACKLIST 처리됨.
# ------------------------------------------------------------------

_CORE_SCALPING_PROMPT = """\
너는 1시간 봉 기반의 '모멘텀 스캘핑 트레이더'야.
상승 추세가 확인된 알트코인에서 짧고 빠른 단타 진입을 노린다.
BTC/ETH/XRP/DOGE/ADA/SOL/SUI/PEPE 등 무거운 메이저 코인은 이미 시스템 레벨에서 제외되었으므로 제공된 알트코인들만 분석해.
이미 유저가 보유 중인 코인은 반드시 제외해.
확신이 없으면 picks 배열을 비워서 관망해도 된다 — 관망 자체가 최고의 전략일 수 있다.

반드시 아래 JSON 형식으로만 응답해 (다른 텍스트 없음):
{
  "market_summary": "시장 분석 2~3문장.",
  "picks": [
    {
      "symbol":            "LINK/KRW",
      "score":             92,
      "reason":            "[스캘핑] 1h RSI 65 모멘텀, MA20 위 단기 상승 추세. TP 2.0% / SL 1.5%",
      "target_profit_pct": 2.0,
      "stop_loss_pct":     1.5
    }
  ]
}

[핵심 매매 원칙 — 1h 모멘텀 스캘핑]

1. 진입 필수 조건 (모두 충족해야 함):
   - 현재가 > 1h MA20 (단기 상승 추세 확인 필수)
   - 1h RSI14 60~75 구간 (모멘텀 진입 구간 — 과매수 전 진입)
   ※ 두 조건 중 하나라도 미충족 시 절대 진입 금지

2. 진입 금지 조건:
   - 현재가 < 1h MA20: 상승 추세 미확인 → 패스
   - 1h RSI14 < 60: 모멘텀 부족 → 상승 동력 없음
   - 1h RSI14 > 75: 단기 과매수 구간 → 되돌림 위험
   - 현재가 100 KRW 미만 동전주: 스킵

3. 목표가 / 손절가 (하드 고정 — 반드시 이 값만 사용):
   - target_profit_pct: 2.0% 고정 (R:R 1.33:1 기준값)
   - stop_loss_pct: 1.5% 고정 (단타 특성상 타이트한 손절 필수)
   ※ 이 값은 Python 코드에서 하드 상한으로 재검증됨 — 다른 값 제시 불가

4. 종목 선정 우선순위:
   - 1h 거래량이 평소 대비 증가 중인 종목 (모멘텀 초기 신호)
   - 4h RSI도 50 이상인 종목 (중기 추세 동반 시 가산점)
   - 거래대금 상위권으로 유동성이 풍부한 종목 우선

5. 일반 규칙:
   - symbol은 "코인명/KRW" 형태 (예: LINK/KRW)
   - symbol은 반드시 "# Top 코인 시장 데이터" 목록에 존재하는 심볼만 사용할 것.
     목록에 없는 코인명을 지어내는 것은 엄격히 금지 — 모르면 관망(picks=[])
   - 모든 숫자 필드는 순수 숫자만 (%, +/- 없음)
   - reason 필드는 반드시 "[스캘핑]" 태그로 시작할 것
   - score 90 이상만 진입 (확실한 모멘텀만 — 관망을 두려워 말 것)
   - market_summary는 관망 시에도 반드시 작성
   - market_summary에 특정 코인명(BTC·ETH 포함)을 직접 나열하지 말 것.
     "전반적 모멘텀 강도", "시장 전체 단기 흐름" 같은 총평 표현으로만 서술할 것.
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

    # v7: 메이저 코인 블랙리스트 — Python 단에서 AI에 데이터 자체를 미전달
    # (토큰 낭비 방지 + 휩쏘 취약 코인의 AI 매수 원천 차단)
    # 백테스트 v6 결과: 해당 코인들 승률 10~20%대 → 전체 수익률·MDD 저하 주원인
    _BLACKLIST: frozenset[str] = frozenset({
        "BTC/KRW",   # 메이저 코인 — 대형 매물대 휩쏘 빈발 (backtest v6 승률 ~15%)
        "ETH/KRW",   # 메이저 코인 — 고변동성 추세 추종 취약 (backtest v6 승률 ~18%)
        "XRP/KRW",   # 뉴스·고래 매도 휩쏘 빈발 (backtest v6 승률 ~12%)
        "DOGE/KRW",  # 밈코인 — 모멘텀 지속성 낮음 (backtest v6 승률 ~20%)
        "ADA/KRW",   # 무거운 메이저 알트 — 돌파 실패율 높음
        "SOL/KRW",   # 고변동성 L1 — 목표가 도달 전 되돌림 잦음
        "SUI/KRW",   # 신흥 L1 — 매물대 취약
        "PEPE/KRW",  # 고변동성 밈코인 — 예측 불가 급등락
    })

    async def analyze_market(
        self,
        market_data: dict[str, dict],
        holding_symbols: set[str],
        engine_type: str = "SWING",
        weight_pct: float = 20.0,
        available_krw: float = 0.0,
    ) -> dict:
        """MarketDataManager 캐시 데이터를 기반으로 시장을 분석하고 최대 2개 코인을 픽한다.

        score ≥ 90 인 종목만 picks에 포함. weight_pct는 호출자(ai_manager)가 엔진별
        설정값을 직접 주입한다 (AI 응답값 무시).

        Args:
            market_data:     MarketDataManager.get_all() 반환값.
            holding_symbols: 유저가 현재 감시 중인 코인 심볼 집합. AI 픽에서 자동 제외.
            engine_type:     "SWING" (4h 듀얼 전략) 또는 "SCALPING" (1h 모멘텀 단타).
            weight_pct:      이번 사이클에 적용할 진입 비중 (%). ai_manager가 엔진별로 주입.
            available_krw:   이번 사이클 가용 예산 (KRW). AI 유저 프롬프트에 컨텍스트 제공.

        Returns:
            {
              "market_summary": str,
              "picks": list[dict],  # score·weight_pct(주입값)·reason·target/stop_loss_pct 포함
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

        # ── 엔진 타입 정규화 + 프롬프트 선택 ─────────────────────────
        _engine = engine_type.upper()
        is_scalping = _engine == "SCALPING"
        system_prompt = _CORE_SCALPING_PROMPT if is_scalping else _CORE_SWING_PROMPT

        # 호출자가 주입한 weight_pct 그대로 사용 (AI 응답 무시)
        forced_weight: float = float(weight_pct)

        # ── 유저 프롬프트 구성 ────────────────────────────────────────
        # v7: 블랙리스트 심볼 사전 로그 (AI에 데이터 미전달 — 토큰 절약 + 휩쏘 원천 차단)
        blacklisted_in_market = [s for s in market_data if s in self._BLACKLIST]
        if blacklisted_in_market:
            logger.info(
                "AITraderService: 블랙리스트 %d개 심볼 제외 (데이터 미전달): %s",
                len(blacklisted_in_market), ", ".join(sorted(blacklisted_in_market)),
            )

        # ── BTC 시장 국면 컨텍스트 (픽 금지, 국면 판단 전용) ────────────
        # 문제: BTC 데이터를 주지 않으면서 "BTC RSI 기준으로 판단하라"고 지시하면
        #       AI가 환각으로 BTC 수치를 조합해 market_summary에 노출하는 부작용 발생.
        # 해결: BTC 핵심 지표만 별도 섹션으로 전달 → 환각 없이 국면 판단 가능.
        btc_data = market_data.get("BTC/KRW") or market_data.get("BTC_KRW")
        btc_ctx_lines: list[str] = []
        if btc_data is not None:
            _b_price = btc_data.get("price")
            _b_rsi4h = btc_data.get("rsi14")
            _b_ma20  = btc_data.get("ma20")
            _b_ma50  = btc_data.get("ma50")
            _above_ma20 = (
                "위(상승)" if (_b_price and _b_ma20 and _b_price > _b_ma20)
                else "아래(하락)" if (_b_price and _b_ma20)
                else "N/A"
            )
            _above_ma50 = (
                "위(상승)" if (_b_price and _b_ma50 and _b_price > _b_ma50)
                else "아래(하락)" if (_b_price and _b_ma50)
                else "N/A"
            )
            btc_ctx_lines = [
                "# BTC 시장 국면 (전략A/B 필터 판단 전용 — picks 목록 및 market_summary 에 'BTC' 직접 언급 절대 금지)",
                (
                    f"- BTC/KRW: 4h RSI={f'{_b_rsi4h:.1f}' if _b_rsi4h is not None else 'N/A'}"
                    f" | 4h MA20 대비={_above_ma20}"
                    f" | 4h MA50 대비={_above_ma50}"
                ),
                "",
            ]
            logger.info(
                "AITraderService: BTC 국면 컨텍스트 전달 (RSI4h=%.1f, MA20=%s, MA50=%s)",
                _b_rsi4h if _b_rsi4h is not None else 0,
                _above_ma20, _above_ma50,
            )

        lines: list[str] = btc_ctx_lines + [f"# Top 코인 시장 데이터 (멀티 타임프레임)\n"]
        for symbol, data in market_data.items():
            # v7: 블랙리스트 코인은 AI에 데이터 자체를 주지 않음
            # (프롬프트에서 제외 지시만 하는 것보다 강력한 원천 차단)
            if symbol in self._BLACKLIST:
                continue

            price = data.get("price")
            chg   = data.get("change_pct")
            vol   = data.get("volume_krw")

            # 변동성 지표
            atr_pct = data.get("atr_pct")

            # 각 타임프레임 지표
            rsi14_4h  = data.get("rsi14")
            ma20_4h   = data.get("ma20")
            ma50_4h   = data.get("ma50")      # v7 신규: 4h MA50 장기 추세 필터
            rsi14_1h  = data.get("rsi14_1h")
            ma20_1h   = data.get("ma20_1h")
            rsi14_15m = data.get("rsi14_15m")
            ma20_15m  = data.get("ma20_15m")

            price_str    = f"{format_krw_price(price)} KRW" if price     is not None else "N/A"
            atr_str      = f"{atr_pct:.2f}%"                if atr_pct   is not None else "N/A"
            chg_str      = f"{chg:+.2f}%"                   if chg       is not None else "N/A"
            vol_str      = f"{vol / 1e8:.1f}억"              if vol       is not None else "N/A"
            rsi4h_str    = f"{rsi14_4h:.1f}"                if rsi14_4h  is not None else "N/A"
            ma20_4h_str  = f"{format_krw_price(ma20_4h)}"   if ma20_4h   is not None else "N/A"
            ma50_4h_str  = f"{format_krw_price(ma50_4h)}"   if ma50_4h   is not None else "N/A"
            rsi1h_str    = f"{rsi14_1h:.1f}"                if rsi14_1h  is not None else "N/A"
            ma1h_str     = f"{format_krw_price(ma20_1h)}"   if ma20_1h   is not None else "N/A"
            rsi15m_str   = f"{rsi14_15m:.1f}"               if rsi14_15m is not None else "N/A"
            ma15m_str    = f"{format_krw_price(ma20_15m)}"  if ma20_15m  is not None else "N/A"

            lines.append(
                f"- {symbol}: 현재가={price_str} | 변동성(ATR)={atr_str}"
                f" | 15m(RSI={rsi15m_str}, MA={ma15m_str})"
                f" | 1h(RSI={rsi1h_str}, MA={ma1h_str})"
                f" | 4h(RSI={rsi4h_str}, MA20={ma20_4h_str}, MA50={ma50_4h_str})"
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
            "[AI DEBUG - INPUT] analyze_market 프롬프트 (engine=%s weight=%.0f%%, budget=%.0f):\n%s",
            _engine, forced_weight, available_krw, user_prompt,
        )

        # ── Anthropic Claude 호출 (엔진 타입에 따라 프롬프트 분기) ───
        try:
            response = await self._client.messages.create(
                model=_CLAUDE_MODEL,
                system=system_prompt,
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
            "[AI DEBUG - OUTPUT] analyze_market 원본 응답 (engine=%s):\n%s",
            _engine, raw,
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

            # ── 환각 티커 차단 (Bug 2 수정) ────────────────────────────
            # market_data에 실제로 없는 심볼 = AI가 지어낸 환각 코인 → 즉시 거부.
            # market_data 키 형식이 "LINK/KRW"와 "LINK_KRW" 두 가지일 수 있으므로 모두 확인.
            _sym_slash = symbol                           # "LINK/KRW"
            _sym_under = symbol.replace("/", "_")        # "LINK_KRW"
            if _sym_slash not in market_data and _sym_under not in market_data:
                logger.warning(
                    "AITraderService: 환각 티커 차단 — 마켓 데이터에 없는 심볼: %s", symbol
                )
                continue

            # ── 블랙리스트 이중 차단 (프롬프트 우회 방어, Bug 1 보조) ─
            # Python 레벨에서 블랙리스트 재확인 — 프롬프트 우회로 BTC 등이
            # picks에 포함될 경우를 코드 레벨에서 원천 차단.
            if symbol in self._BLACKLIST:
                logger.warning(
                    "AITraderService: 블랙리스트 심볼 픽 차단 (프롬프트 우회 방어): %s", symbol
                )
                continue

            if symbol in holding_symbols:
                continue

            # score 파싱 및 90점 미만 필터
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

            # stop_loss_pct 검증 — 엔진별 하드 상한 적용
            # SWING:    하드 상한 5.0% (전략A 기본 4%, 전략B 기본 2.5%)
            # SCALPING: 하드 상한 2.0% (1h 단타 특성상 타이트한 손절 필수)
            if is_scalping:
                _stop_default = 1.5
                _stop_ceiling = 2.0
            else:
                _stop_default = 3.5
                _stop_ceiling = 5.0

            raw_stop = _safe_pct(p.get("stop_loss_pct", _stop_default), default=_stop_default)
            if raw_stop > _stop_ceiling:
                logger.info(
                    "AITraderService: 넓은 손절 스킵 (stop=%.1f%% > %.1f%%, engine=%s): %s",
                    raw_stop, _stop_ceiling, _engine, symbol,
                )
                continue
            stop_loss_pct = raw_stop

            # target_profit_pct: 엔진별 최소 R:R 코드 레벨 보정 (AI 이중 방어)
            # SWING:    R:R ≥ 1.2 (전략A 프롬프트에서 1.5 지시, 코드는 1.2 하한)
            # SCALPING: R:R ≥ 1.3 (TP 2.0% / SL 1.5% 기준, 하드 보정)
            _rr_min = 1.3 if is_scalping else 1.2
            if is_scalping:
                _target_default = 2.0
            else:
                _target_default = 5.0

            raw_target      = _safe_pct(p.get("target_profit_pct", _target_default), default=_target_default)
            min_target      = round(stop_loss_pct * _rr_min, 2)
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
            "AITraderService 분석 완료 (engine=%s weight=%.0f%%): %d 개 픽 %s",
            _engine,
            forced_weight,
            len(validated),
            [(v["symbol"], v["score"], v["weight_pct"]) for v in validated],
        )
        return {"market_summary": market_summary, "picks": validated}

    async def review_positions(
        self,
        positions_data: list[dict],
        market_data: dict[str, dict],
        engine_type: str = "SWING",
    ) -> list[dict]:
        """현재 보유 포지션을 AI가 재검토해 HOLD / UPDATE / SELL 을 결정한다.

        Args:
            positions_data: 보유 포지션 리스트.
                            각 항목: {
                                "symbol", "buy_price", "current_price",
                                "profit_pct", "target_profit_pct", "stop_loss_pct"
                            }
            market_data: MarketDataManager.get_all() 반환값.
            engine_type: "SWING" 또는 "SCALPING" (포지션 리뷰 로직은 두 엔진 동일).

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
            "AITraderService(review) 완료 (engine=%s): %d 개 포지션 검토 "
            "(SELL=%d, UPDATE=%d, HOLD=%d)",
            engine_type.upper(), len(validated), sell_count, update_count, hold_count,
        )
        return validated
