from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class SubscriptionTier(str, Enum):
    FREE = "FREE"
    PRO = "PRO"
    VIP = "VIP"


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)  # Discord ID
    upbit_access_key: Mapped[str | None] = mapped_column(String, nullable=True)
    upbit_secret_key: Mapped[str | None] = mapped_column(String, nullable=True)
    subscription_tier: Mapped[str] = mapped_column(String(50), default=SubscriptionTier.FREE)
    sub_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    payments: Mapped[list["Payment"]] = relationship("Payment", back_populates="user")
    bot_settings: Mapped[list["BotSetting"]] = relationship("BotSetting", back_populates="user")

    @property
    def is_pro(self) -> bool:
        if self.subscription_tier in (SubscriptionTier.PRO, SubscriptionTier.VIP):
            return self.sub_expires_at is None or self.sub_expires_at > datetime.utcnow()
        return False

    @property
    def max_coins(self) -> int:
        """등급별 최대 등록 가능 코인 수"""
        if self.subscription_tier == SubscriptionTier.VIP:
            return 999
        if self.subscription_tier == SubscriptionTier.PRO and self.is_pro:
            return 999
        return 2  # FREE 2개로 제한

    @property
    def max_invest_krw(self) -> int:
        """등급별 최대 1회 투자 금액 (KRW)"""
        if self.is_pro:
            return 100_000_000
        return 100_000  # FREE: 10만 원
