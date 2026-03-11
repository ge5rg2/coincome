"""구독 등급 관리 및 만료 알림 스케줄러"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.user import User

logger = logging.getLogger(__name__)

RENEWAL_NOTICE_DAYS = 3   # 만료 N일 전 알림
CHECK_INTERVAL = 3600     # 1시간마다 체크


async def extend_subscription(
    db: AsyncSession,
    user_id: str,
    tier: str,
    months: int = 1,
) -> User:
    """결제 완료 후 구독 기간 연장"""
    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        user = User(user_id=user_id)
        db.add(user)

    now = datetime.now(tz=timezone.utc)
    base = max(user.sub_expires_at or now, now)
    user.subscription_tier = tier
    user.sub_expires_at = base + timedelta(days=30 * months)
    await db.commit()
    await db.refresh(user)
    logger.info("구독 연장: user=%s tier=%s expires=%s", user_id, tier, user.sub_expires_at)
    return user


async def expiry_reminder_loop(notify_callback) -> None:
    """
    구독 만료 N일 전 사용자에게 DM 알림.
    notify_callback: async def (user_id: str, message: str)
    """
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        threshold = datetime.now(tz=timezone.utc) + timedelta(days=RENEWAL_NOTICE_DAYS)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User).where(
                    User.subscription_tier != "FREE",
                    User.sub_expires_at <= threshold,
                    User.sub_expires_at > datetime.now(tz=timezone.utc),
                    User.is_active.is_(True),
                )
            )
            expiring_users = result.scalars().all()

        for user in expiring_users:
            days_left = (user.sub_expires_at - datetime.now(tz=timezone.utc)).days
            msg = (
                f"⏰ **구독 만료 안내**\n"
                f"현재 등급: **{user.subscription_tier}**\n"
                f"만료까지 **{days_left}일** 남았습니다.\n"
                f"계속 이용하시려면 `/구독` 명령어로 갱신해 주세요."
            )
            try:
                await notify_callback(user.user_id, msg)
            except Exception as exc:
                logger.warning("만료 알림 실패: user=%s err=%s", user.user_id, exc)
