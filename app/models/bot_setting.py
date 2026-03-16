from sqlalchemy import Boolean, Float, ForeignKey, Integer, Numeric, String, Text
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
    is_paper_trading: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── AI 관리 여부 플래그 ───────────────────────────────────────────
    # True  = AI 펀드 매니저(ai_manager)가 자동 생성·관리하는 포지션.
    # False = 사용자가 /설정 커맨드로 직접 등록한 수동 포지션 (기본값).
    # AI 포지션 리뷰·슬롯 카운트 시 is_ai_managed=True 레코드만 대상으로 삼아
    # 수동 봇 설정과의 혼선을 방지한다.
    is_ai_managed: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── AI 메타데이터 ─────────────────────────────────────────────────
    # AI 매수 결정 시점의 분석 근거를 저장한다.
    # 수동 봇 포지션(is_ai_managed=False)에는 NULL.
    trade_style: Mapped[str | None] = mapped_column(String(20), nullable=True)   # "SWING" | "SCALPING"
    ai_score: Mapped[int | None] = mapped_column(Integer, nullable=True)          # 0–100 매력도 점수
    ai_reason: Mapped[str | None] = mapped_column(Text, nullable=True)            # AI 분석 근거 텍스트

    user: Mapped["User"] = relationship("User", back_populates="bot_settings")
