"""
웹 페이지 SSR 라우터 (Jinja2Templates).

Endpoints:
    GET /payment              - 토스페이먼츠 결제 위젯 페이지
    GET /payment/success      - 결제 성공 콜백 (JS가 /api/v1/payments/confirm 호출)
    GET /payment/fail         - 결제 실패 안내 페이지
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["web"])

# app/templates/ 절대 경로 (실행 위치 무관)
TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# 등급별 결제 금액 및 혜택 정의
TIER_CONFIG: dict[str, dict] = {
    "PRO": {
        "amount": 9_900,
        "order_name": "CoinCome PRO 구독 1개월",
        "benefits": [
            "코인 무제한 동시 운영",
            "투자금 무제한",
            "트레일링 스탑 기능",
            "상세 수익 통계",
        ],
    },
    "VIP": {
        "amount": 29_900,
        "order_name": "CoinCome VIP 구독 1개월",
        "benefits": [
            "PRO 모든 기능 포함",
            "우선 지원 채널",
            "전략 커스텀 설정",
            "1:1 전략 컨설팅",
        ],
    },
}


@router.get("/payment", response_class=HTMLResponse)
async def payment_page(request: Request, user_id: str, tier: str) -> HTMLResponse:
    """
    결제 위젯 페이지 렌더링.

    Args:
        user_id: Discord 사용자 ID (URL 쿼리 파라미터)
        tier:    구독 등급 — PRO 또는 VIP (URL 쿼리 파라미터)

    Returns:
        토스페이먼츠 위젯이 포함된 다크모드 결제 페이지
    """
    tier = tier.upper()
    config = TIER_CONFIG.get(tier)
    if not config:
        raise HTTPException(
            status_code=400, detail=f"지원하지 않는 구독 등급입니다: {tier}"
        )

    # 서버 측에서 주문 ID 생성 — 결제 건마다 고유해야 함
    order_id = f"coincome-{uuid.uuid4().hex[:20]}"

    logger.info("결제 페이지 요청: user_id=%s tier=%s order_id=%s", user_id, tier, order_id)

    return templates.TemplateResponse(
        "payment.html",
        {
            "request": request,
            "user_id": user_id,
            "tier": tier,
            "amount": config["amount"],
            "order_name": config["order_name"],
            "order_id": order_id,
            "benefits": config["benefits"],
            "toss_client_key": settings.toss_client_key,
        },
    )


@router.get("/payment/success", response_class=HTMLResponse)
async def payment_success(request: Request) -> HTMLResponse:
    """
    토스페이먼츠 결제 성공 콜백 페이지.

    TossPayments SDK가 성공 시 아래 파라미터를 붙여 이 URL로 리다이렉트:
        paymentKey, orderId, amount  (TossPayments 자동 추가)
        user_id, tier                (successUrl에 미리 포함한 커스텀 파라미터)

    실제 결제 확정(서버 승인)은 클라이언트 JS가
    /api/v1/payments/confirm 를 호출해 처리한다.
    """
    return templates.TemplateResponse(
        "payment_success.html",
        {"request": request},
    )


@router.get("/payment/fail", response_class=HTMLResponse)
async def payment_fail(
    request: Request,
    code: str = "",
    message: str = "",
) -> HTMLResponse:
    """결제 실패/취소 안내 페이지."""
    logger.info("결제 실패 콜백: code=%s message=%s", code, message)
    return templates.TemplateResponse(
        "payment_fail.html",
        {"request": request, "code": code, "message": message},
    )
