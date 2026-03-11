"""
결제 관련 API 라우터.

Endpoints:
    POST /api/v1/payments/callback   - 토스페이먼츠 웹훅 수신 후 구독 자동 갱신
    POST /api/v1/payments/confirm    - 클라이언트 결제 승인 요청 (프론트 → 서버 → 토스)
    GET  /api/v1/payments/history    - 사용자 결제 이력 조회
"""
from __future__ import annotations

import base64
import logging
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.payment import Payment, PaymentStatus
from app.models.user import SubscriptionTier
from app.services.subscription import extend_subscription

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/payments", tags=["payments"])

TOSS_CONFIRM_URL = "https://api.tosspayments.com/v1/payments/confirm"
SUBSCRIPTION_MONTHS = 1  # 1회 결제 = 1개월 연장


# ------------------------------------------------------------------
# Pydantic 스키마
# ------------------------------------------------------------------

class PaymentConfirmRequest(BaseModel):
    """프론트엔드에서 결제 승인 요청 시 전달하는 페이로드"""
    payment_key: str
    order_id: str
    amount: float
    user_id: str
    tier: str  # PRO | VIP


class TossWebhookPayload(BaseModel):
    """토스페이먼츠 웹훅 페이로드 (일부 필드)"""
    eventType: str
    data: dict


# ------------------------------------------------------------------
# 헬퍼
# ------------------------------------------------------------------

def _toss_auth_header() -> str:
    """토스페이먼츠 Basic 인증 헤더 생성"""
    token = base64.b64encode(f"{settings.toss_secret_key}:".encode()).decode()
    return f"Basic {token}"


async def _call_toss_confirm(payment_key: str, order_id: str, amount: float) -> dict:
    """토스페이먼츠 결제 최종 승인 API 호출"""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            TOSS_CONFIRM_URL,
            json={"paymentKey": payment_key, "orderId": order_id, "amount": amount},
            headers={
                "Authorization": _toss_auth_header(),
                "Content-Type": "application/json",
            },
        )
        if resp.status_code != 200:
            logger.error("토스 승인 실패: status=%s body=%s", resp.status_code, resp.text)
            raise HTTPException(status_code=400, detail="결제 승인 실패")
        return resp.json()


# ------------------------------------------------------------------
# 결제 승인 (클라이언트 → 서버 → 토스)
# ------------------------------------------------------------------

@router.post("/confirm")
async def confirm_payment(
    body: PaymentConfirmRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    결제 승인 플로우:
    1. 프론트에서 payment_key / order_id / amount 전달
    2. 서버에서 토스페이먼츠 승인 API 호출
    3. 성공 시 DB 기록 + 구독 갱신
    """
    # 중복 결제 방지: 같은 order_id가 이미 DONE 상태면 거부
    existing = await db.execute(
        select(Payment).where(Payment.order_id == body.order_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="이미 처리된 주문입니다.")

    toss_resp = await _call_toss_confirm(body.payment_key, body.order_id, body.amount)

    # 결제 이력 저장
    payment = Payment(
        user_id=body.user_id,
        order_id=body.order_id,
        amount=body.amount,
        status=PaymentStatus.DONE,
        payment_key=body.payment_key,
        method=toss_resp.get("method"),
        paid_at=datetime.now(tz=timezone.utc),
    )
    db.add(payment)

    # 구독 갱신
    user = await extend_subscription(db, body.user_id, body.tier, months=SUBSCRIPTION_MONTHS)

    logger.info(
        "결제 완료: user=%s tier=%s order_id=%s amount=%s",
        body.user_id, body.tier, body.order_id, body.amount,
    )
    return {
        "ok": True,
        "subscription_tier": user.subscription_tier,
        "sub_expires_at": user.sub_expires_at.isoformat() if user.sub_expires_at else None,
    }


# ------------------------------------------------------------------
# 토스페이먼츠 웹훅 수신
# ------------------------------------------------------------------

@router.post("/callback")
async def payment_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    토스페이먼츠 이벤트 웹훅 수신 엔드포인트.

    웹훅 이벤트 타입:
      - PAYMENT_STATUS_CHANGED: 결제 상태 변경 (DONE / CANCELED / ABORTED)
      - 기타 이벤트는 수신 후 로그만 기록

    보안: 토스페이먼츠는 IP 화이트리스트 방식이므로 별도 서명 검증은 선택 사항.
    실제 운영 시 X-Toss-Signature 헤더 검증을 추가하는 것을 권장.
    """
    payload = await request.json()
    event_type = payload.get("eventType", "")
    data = payload.get("data", {})

    logger.info("토스 웹훅 수신: eventType=%s", event_type)

    if event_type != "PAYMENT_STATUS_CHANGED":
        return {"received": True}

    order_id = data.get("orderId")
    status = data.get("status")  # DONE / CANCELED / ABORTED
    payment_key = data.get("paymentKey")
    amount = data.get("totalAmount", 0)
    method = data.get("method")

    if not order_id:
        raise HTTPException(status_code=400, detail="orderId 누락")

    # Payment 조회
    result = await db.execute(select(Payment).where(Payment.order_id == order_id))
    payment = result.scalar_one_or_none()

    if payment is None:
        # 웹훅이 confirm보다 먼저 도달하는 엣지 케이스: 새 레코드 생성
        # user_id는 order_id 파싱 또는 별도 조회 로직 필요
        logger.warning("웹훅에서 알 수 없는 order_id: %s", order_id)
        return {"received": True}

    payment.status = status
    if status == PaymentStatus.DONE:
        payment.payment_key = payment_key
        payment.paid_at = datetime.now(tz=timezone.utc)
        payment.method = method
        # 구독 갱신 (이미 /confirm에서 처리된 경우 중복 방지는 extend_subscription 내부에서 처리)
        await extend_subscription(db, payment.user_id, SubscriptionTier.PRO)
    elif status in ("CANCELED", "ABORTED"):
        logger.info("결제 취소/실패: order_id=%s status=%s", order_id, status)

    await db.commit()
    return {"received": True}


# ------------------------------------------------------------------
# 결제 이력 조회
# ------------------------------------------------------------------

@router.get("/history/{user_id}")
async def get_payment_history(
    user_id: str,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """사용자의 결제 이력 반환 (최신순)"""
    result = await db.execute(
        select(Payment)
        .where(Payment.user_id == user_id)
        .order_by(Payment.paid_at.desc())
        .limit(20)
    )
    payments = result.scalars().all()
    return [
        {
            "payment_id": p.payment_id,
            "order_id": p.order_id,
            "amount": float(p.amount),
            "status": p.status,
            "method": p.method,
            "paid_at": p.paid_at.isoformat() if p.paid_at else None,
        }
        for p in payments
    ]
