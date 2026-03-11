from sqlalchemy import Boolean, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class BotSetting(Base):
    __tablename__ = "bot_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(255), ForeignKey("users.user_id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)   # e.g. "BTC/KRW"
    buy_amount_krw: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    target_profit_pct: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)  # 익절 %
    stop_loss_pct: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)       # 손절 %
    is_running: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped["User"] = relationship("User", back_populates="bot_settings")
