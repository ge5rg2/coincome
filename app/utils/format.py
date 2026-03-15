"""공통 포맷팅 유틸리티.

코인 가격·금액 등을 Discord 메시지에 표시할 때 사용하는 헬퍼 함수 모음.
"""
from __future__ import annotations


def format_krw_price(price: float) -> str:
    """KRW 기준 코인 현재가를 가격대에 맞는 소수점 자리수로 포맷팅한다.

    업비트는 코인 가격에 따라 호가 단위(틱)가 달라지므로,
    100원 미만 동전주의 경우 소수점 4자리까지 표시해 실제 가격 변동을 정확히 표현한다.

    가격대별 규칙:
        - 100 KRW 미만  : 소수점 4자리 (예: 9.1234)
        - 1,000 KRW 미만: 소수점 2자리 (예: 234.56)
        - 1,000 KRW 이상: 정수 (예: 50,000)

    Args:
        price: 포맷팅할 코인 가격 (KRW 단위, 양수).

    Returns:
        쉼표 구분자와 가격대별 소수점 자리수를 적용한 문자열 (KRW 접미사 미포함).
        예: "9.1234" / "234.56" / "50,000"
    """
    if price < 100:
        return f"{price:,.4f}"
    elif price < 1_000:
        return f"{price:,.2f}"
    else:
        return f"{price:,.0f}"
