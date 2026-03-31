import logging
from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.database import Base

logger = logging.getLogger(__name__)


class EncryptedString(TypeDecorator):
    """DB 입출력 시 AES-256(Fernet) 자동 암복호화를 수행하는 컬럼 타입.

    - process_bind_param : Python → DB  (평문 → 암호문)
    - process_result_value: DB → Python (암호문 → 평문)

    비즈니스 로직(TradingWorker, ExchangeService 등)은 평문으로만 다루며
    암호화 세부 사항에 완전히 무관(투명)하게 동작한다.
    """

    impl = String
    cache_ok = True  # 인스턴스 상태 없음 → SQLAlchemy 캐시 허용

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        """Python → DB: 평문을 Fernet 암호문으로 변환한다."""
        if value is None:
            return None
        from app.utils.crypto import encrypt  # 순환 임포트 방지를 위해 지연 임포트

        return encrypt(value)

    def process_result_value(self, value: str | None, dialect) -> str | None:
        """DB → Python: Fernet 암호문을 평문으로 복원한다.

        마이그레이션 이전 평문 값이 남아 있는 경우 복호화에 실패하므로
        InvalidToken 시 WARNING 을 남기고 원본 값을 그대로 반환한다.
        마이그레이션 완료 후에는 이 경로에 진입하지 않아야 한다.
        """
        if value is None:
            return None
        from app.utils.crypto import InvalidToken, decrypt  # 지연 임포트

        try:
            return decrypt(value)
        except (InvalidToken, Exception):
            logger.warning(
                "API 키 복호화 실패 — 평문으로 저장된 값으로 추정됩니다."
            )
            return value


class SubscriptionTier(str, Enum):
    FREE = "FREE"
    PRO = "PRO"
    VIP = "VIP"


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)  # Discord ID
    upbit_access_key: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    upbit_secret_key: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    subscription_tier: Mapped[str] = mapped_column(String(50), default=SubscriptionTier.FREE)
    sub_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── 정기 보고 설정 ────────────────────────────────────────────────
    # report_enabled         : 보고 DM 수신 여부 (기본 켜짐)
    # report_interval_hours  : 보고 주기 (허용값: 1 / 3 / 6 / 12 / 24)
    # last_report_sent_at    : 마지막 보고 전송 시각 (주기 계산에 사용)
    report_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    report_interval_hours: Mapped[int] = mapped_column(Integer, default=1)
    last_report_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── AI 자동 매매 설정 ─────────────────────────────────────────────
    # ai_mode_enabled      : AI 펀드 매니저 활성화 여부 (VIP 전용, 기본 꺼짐)
    # ai_max_coins         : 동시 보유 최대 코인 수 (기본 3, 실거래·모의투자 각각 적용)
    # ai_paper_mode_enabled: AI 모의투자 ON/OFF (등급 무관, 기본 꺼짐)
    #                        True → 스케줄러가 virtual_krw 잔고로 가상 매매 수행
    ai_mode_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_max_coins: Mapped[int] = mapped_column(Integer, default=3)
    ai_paper_mode_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── 모의투자 가상 잔고 ────────────────────────────────────────────
    # virtual_krw : 모의투자 시 사용하는 가상 원화 잔고 (기본 1,000만 원)
    #               매수 시 차감, 매도 시 체결 대금 합산.
    virtual_krw: Mapped[float] = mapped_column(Float, default=10_000_000.0)

    # ── AI 전용 연착륙 플래그 ─────────────────────────────────────────
    # ai_is_shutting_down: True이면 연착륙 진행 중 — 신규 매수를 중단하고
    #                      기존 포지션이 모두 청산되면 ai_mode_enabled=False 로 자동 전환.
    ai_is_shutting_down: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── 등급별 활성 가능 AI 엔진 수 ──────────────────────────────────
    # max_active_engines: 동시에 활성화할 수 있는 AI 엔진 개수
    #   FREE  = 0 (AI 트레이딩 불가)
    #   PRO   = 1 (알트 스윙 or 알트 스캘핑 중 1개)
    #   VIP   = 3 (알트 스윙 + 알트 스캘핑 + 메이저 트렌드 모두 가능)
    max_active_engines: Mapped[int] = mapped_column(Integer, default=1)

    # ── AI V2: 모듈형 엔진 선택 + 엔진별 독립 예산·비중 ─────────────────
    # ai_engine_mode     : 알트코인 엔진 선택
    #   "SWING"   — 📊 4h 듀얼 스윙 전용 (01·05·09·13·17·21시 KST 실행)
    #   "SCALPING"— ⚡ 1h 스캘핑 전용   (매시 정각 실행)
    #   "BOTH"    — 🔥 동시 가동        (스윙+스캘핑 독립 실행, 예산·비중 각각 설정)
    # ai_swing_budget_krw  : 스윙 엔진 운용 예산 한도 (KRW, 1,000,000 ~ 100,000,000)
    # ai_swing_weight_pct  : 스윙 1회 진입 비중 (10 ~ 100%)
    # ai_scalp_budget_krw  : 스캘핑 엔진 운용 예산 한도 (KRW, 1,000,000 ~ 100,000,000)
    # ai_scalp_weight_pct  : 스캘핑 1회 진입 비중 (10 ~ 100%)
    ai_engine_mode: Mapped[str] = mapped_column(String(10), default="SWING")
    ai_swing_budget_krw: Mapped[int] = mapped_column(Integer, default=1_000_000)
    ai_swing_weight_pct: Mapped[int] = mapped_column(Integer, default=20)
    ai_scalp_budget_krw: Mapped[int] = mapped_column(Integer, default=1_000_000)
    ai_scalp_weight_pct: Mapped[int] = mapped_column(Integer, default=20)

    # ── AI V2: MAJOR 메이저 코인 전용 Trend Catcher 엔진 ─────────────
    # is_major_enabled  : MAJOR 엔진 활성화 여부 (기본 꺼짐)
    #                     스윙/스캘핑과 독립적으로 ON/OFF 가능
    # major_budget      : MAJOR 엔진 운용 예산 한도 (KRW, 1,000,000 ~ 100,000,000)
    # major_trade_ratio : MAJOR 1회 진입 비중 (10 ~ 100%, 기본 10%)
    #                     전략: EMA200·EMA20>EMA50·BB상단 돌파 3중 필터 (4h 봉)
    is_major_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    major_budget: Mapped[int] = mapped_column(Integer, default=0)
    major_trade_ratio: Mapped[int] = mapped_column(Integer, default=10)

    payments: Mapped[list["Payment"]] = relationship("Payment", back_populates="user")
    bot_settings: Mapped[list["BotSetting"]] = relationship("BotSetting", back_populates="user")
    trade_histories: Mapped[list["TradeHistory"]] = relationship(
        "TradeHistory", back_populates="user"
    )

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
