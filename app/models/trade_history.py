"""
TradeHistory 모델 — 매매 종료(매도) 결과 이력 테이블.

모의투자(is_paper_trading=True)와 실거래(False) 모두 기록하며,
/AI통계 커맨드의 조회 기반 데이터로 사용된다.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TradeHistory(Base):
    """매도 체결 시 생성되는 매매 이력 레코드.

    Attributes:
        id: 자동 증가 PK.
        user_id: Discord 사용자 ID (users.user_id FK).
        symbol: 거래 심볼 (예: BTC/KRW).
        buy_price: 매수 체결 단가 (KRW). 모의투자는 슬리피지 0.1% 반영 가격.
        sell_price: 매도 체결 단가 (KRW). 모의투자는 웹소켓 현재가 기준.
        profit_pct: 수익률 (%). 양수 = 익절, 음수 = 손절.
        profit_krw: 수익금 (KRW). (sell_price - buy_price) × amount_coin.
        buy_amount_krw: 매수 시 투자 원금 (KRW). 유저 설정 금액 기준.
        is_paper_trading: True = 모의투자 기록 / False = 실거래 기록.
        created_at: 매도 체결 시각 (UTC, DB 서버 타임스탬프).
    """

    __tablename__ = "trade_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("users.user_id"),
        nullable=False,
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)

    buy_price: Mapped[float] = mapped_column(Float, nullable=False)   # 매수 단가 (KRW)
    sell_price: Mapped[float] = mapped_column(Float, nullable=False)  # 매도 단가 (KRW)
    profit_pct: Mapped[float] = mapped_column(Float, nullable=False)  # 수익률 (%)
    profit_krw: Mapped[float] = mapped_column(Float, nullable=False)  # 수익금 (KRW)
    buy_amount_krw: Mapped[float] = mapped_column(Float, nullable=False)  # 투자 원금 (KRW)

    is_paper_trading: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    # ── AI 메타데이터 ─────────────────────────────────────────────────
    # AI 매수 결정 시점의 분석 근거. 수동 봇 거래는 NULL.
    is_ai_managed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trade_style: Mapped[str | None] = mapped_column(String(20), nullable=True)   # "SWING" | "SCALPING"
    ai_score: Mapped[int | None] = mapped_column(Integer, nullable=True)          # 0–100 매력도 점수
    ai_reason: Mapped[str | None] = mapped_column(Text, nullable=True)            # AI 분석 근거 텍스트

    # ── Admin 분석용 메타데이터 ───────────────────────────────────────
    # 익절률/손절률/슬리피지/AI 버전별 성과 집계에 사용.
    bought_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # 매수 체결 시각 (BotSetting.bought_at에서 이관)
    close_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # TP_HIT / SL_HIT / AI_FORCE_SELL / MANUAL_OVERRIDE
    ai_version: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default="v2.0"
    )  # AI 전략 버전 (BotSetting.ai_version에서 이관)
    expected_price: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )  # 익절/손절 목표 단가 — 실제 체결가와 비교해 슬리피지 추적

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    user: Mapped["User"] = relationship("User", back_populates="trade_histories")
