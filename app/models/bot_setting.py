from sqlalchemy import Boolean, Float, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class BotSetting(Base):
    __tablename__ = "bot_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(255), ForeignKey("users.user_id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)       # e.g. "BTC/KRW"
    buy_amount_krw: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    target_profit_pct: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)  # 익절 %
    stop_loss_pct: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)      # 손절 %
    is_running: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── 상태 영속성 컬럼 ──────────────────────────────────────────────
    # 매수 체결 후 업데이트, 서버 재시작 시 포지션 복구에 사용.
    # 매도 완료 또는 수동 중지 시 NULL로 초기화.
    buy_price: Mapped[float | None] = mapped_column(Float, nullable=True)   # 매수 단가 (KRW)
    amount_coin: Mapped[float | None] = mapped_column(Float, nullable=True) # 보유 코인 수량

    # ── 모의투자 모드 플래그 ──────────────────────────────────────────
    # True  = 실제 업비트 API 호출 없이 가상 잔고로 매매 시뮬레이션.
    # False = 실거래 모드 (기본값).
    # /모의투자 커맨드로 생성된 설정은 항상 True.
    is_paper_trading: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped["User"] = relationship("User", back_populates="bot_settings")
