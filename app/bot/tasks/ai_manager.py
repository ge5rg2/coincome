"""
AIFundManagerTask: 매시 정각에 실행되며 엔진 모드(ai_engine_mode)에 따라 유저를 필터링하는 AI 펀드 매니저.

트리거 시각 (KST, 매시 정각):
  00:00 ~ 23:00 (24회/일)

엔진 모드별 동작:
  SWING    — 4h 봉 마감 정각에만 실행 (01/05/09/13/17/21시 KST, 6회/일)
  SCALPING — 매시 정각 실행 (1h 봉 기반 단타, 총 24회/일)
  BOTH     — 두 엔진 동시 가동
             SWING 엔진: 스윙 시간대(01·05·09·13·17·21시)에만 추가 실행
             SCALPING 엔진: 매시 정각 실행
             → 두 엔진은 완전히 독립된 예산·비중·타임프레임 사용

처리 대상 (단일 OR 쿼리):
  (VIP + ai_mode_enabled=True) OR ai_paper_mode_enabled=True  — is_active=True 조건 공통
  → 두 모드를 동시에 켠 유저도 단일 _process_user 호출로 처리.
  → SWING 유저는 스윙 시간대가 아닌 경우 스킵.
  → SCALPING/BOTH 유저는 매시 정각 처리.

격리 아키텍처:
  ┌─ _process_user(user, is_swing_hour) ───────────────────────────┐
  │  is_real  = VIP AND ai_mode_enabled                            │
  │  is_paper = ai_paper_mode_enabled                              │
  │                                                                │
  │  [Step 1] 실전 포지션 리뷰  (is_ai_managed=True, is_paper=False) │
  │  [Step 2] 모의 포지션 리뷰  (is_paper=True)                     │
  │  [Step 3a] SWING 분석·매수  (is_swing_hour=True 시에만)         │
  │            → ai_swing_budget_krw / ai_swing_weight_pct 사용    │
  │  [Step 3b] SCALPING 분석·매수  (매 사이클 실행)                 │
  │            → ai_scalp_budget_krw / ai_scalp_weight_pct 사용    │
  │  [Step 4] 단일 통합 DM Embed 발송                              │
  │           (모의 항목에는 [🎮모의] 태그 명시)                    │
  └────────────────────────────────────────────────────────────────┘

Rate-Limit 방지:
  - 유저 간 asyncio.sleep(1)
  - 코인 간 asyncio.sleep(0.5)
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import selectinload

from app.database import AsyncSessionLocal
from app.models.bot_setting import BotSetting
from app.models.user import SubscriptionTier, User
from app.services.ai_trader import AITraderService
from app.services.exchange import ExchangeService
from app.services.market_data import MarketDataManager
from app.services.trading_worker import TradingWorker, WorkerRegistry
from app.services.websocket import UpbitWebsocketManager
from app.utils.format import format_krw_price
from app.utils.time import get_next_run_time_for_style

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 스케줄 상수
# ------------------------------------------------------------------

_KST = ZoneInfo("Asia/Seoul")

# 매시 정각 24개 (00:00 ~ 23:00 KST)
_LOOP_TIMES: list[datetime.time] = [
    datetime.time(hour=h, minute=0, second=0, tzinfo=_KST)
    for h in range(24)
]

# SWING 모드 전용 실행 시각 (업비트 4시간 봉 마감 정각 KST)
# app/utils/time.py 의 _SWING_SCHEDULE_HOURS 와 반드시 동기화 유지
_SWING_HOURS: frozenset[int] = frozenset({1, 5, 9, 13, 17, 21})


class AIFundManagerTask(commands.Cog):
    """매시간 포지션 리뷰·신규 매수·DM 통합 리포트를 수행하는 백그라운드 Cog.

    BEAST(SCALPING) 유저는 매시 정각 처리, SNIPER(SWING) 유저는 4시간 봉 마감 시각에만 처리.

    Attributes:
        bot:        Discord 봇 인스턴스.
        ai_service: AITraderService 싱글 인스턴스.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.ai_service = AITraderService()
        self.fund_loop.start()

    def cog_unload(self) -> None:
        """Cog 언로드 시 백그라운드 루프를 정상 종료한다."""
        self.fund_loop.cancel()

    # ------------------------------------------------------------------
    # 매시 정각 루프 (KST 00:00 ~ 23:00)
    # ------------------------------------------------------------------

    @tasks.loop(time=_LOOP_TIMES)
    async def fund_loop(self) -> None:
        """매시 정각에 포지션 리뷰·신규 매수를 실행하는 메인 루프.

        처리 흐름:
          0. 10초 대기 (업비트 서버 캔들 롤오버 안정화)
          1. 현재 KST 시각 확인 → SWING 실행 여부 결정
          2. MarketDataManager 캐시 존재 확인
          3. DB: 엔진 모드별 적절한 유저 조회
             - SWING 시간대: SWING + SCALPING + BOTH 유저 모두
             - SCALPING 전용 시간대: SCALPING/BOTH 유저만 (SWING 유저 스킵)
          4. 각 유저별 _process_user() 실행 (is_swing_hour 전달)
        """
        await asyncio.sleep(10)

        # 1. 현재 KST 시각으로 SWING 실행 여부 결정
        current_hour = datetime.datetime.now(_KST).hour
        is_swing_hour = current_hour in _SWING_HOURS

        logger.info(
            "AI 펀드 매니저 루프 실행 (KST %02d:00 | SWING=%s)",
            current_hour, is_swing_hour,
        )

        # 2. 마켓 데이터 캐시 확인
        market_data = MarketDataManager.get().get_all()
        if not market_data:
            logger.warning("AI 펀드 매니저: 마켓 데이터 캐시 없음 — 스킵")
            return

        # 3. 단일 OR 쿼리: (VIP + ai_mode_enabled) OR ai_paper_mode_enabled
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User)
                .where(
                    User.is_active.is_(True),
                    or_(
                        and_(
                            User.subscription_tier == SubscriptionTier.VIP,
                            User.ai_mode_enabled.is_(True),
                        ),
                        User.ai_paper_mode_enabled.is_(True),
                    ),
                )
                .options(selectinload(User.bot_settings))
            )
            all_users: list[User] = result.scalars().all()

        if not all_users:
            logger.info("AI 펀드 매니저: 대상 유저 없음")
            return

        # 4. 엔진 모드별 필터링:
        #    - SWING 시간대: 모든 유저 처리
        #    - SCALPING 전용 시간대: SCALPING/BOTH 유저만 처리 (SWING 유저 스킵)
        if is_swing_hour:
            target_users = all_users
        else:
            target_users = [
                u for u in all_users
                if (getattr(u, "ai_engine_mode", "SWING") or "SWING") in ("SCALPING", "BOTH")
            ]

        if not target_users:
            logger.info(
                "AI 펀드 매니저: SCALPING/BOTH 유저 없음 (SWING 전용 시간대 아님, hour=%d)",
                current_hour,
            )
            return

        # 집계 로그
        def _get_engine(u: User) -> str:
            return (getattr(u, "ai_engine_mode", "SWING") or "SWING").upper()

        swing_count    = sum(1 for u in target_users if _get_engine(u) == "SWING")
        scalping_count = sum(1 for u in target_users if _get_engine(u) == "SCALPING")
        both_count     = sum(1 for u in target_users if _get_engine(u) == "BOTH")
        real_count     = sum(
            1 for u in target_users
            if u.subscription_tier == SubscriptionTier.VIP and u.ai_mode_enabled
        )
        paper_count    = sum(1 for u in target_users if u.ai_paper_mode_enabled)
        logger.info(
            "AI 펀드 매니저 대상: 전체=%d명 (실전=%d명/모의=%d명, "
            "SWING=%d명/SCALPING=%d명/BOTH=%d명)",
            len(target_users), real_count, paper_count,
            swing_count, scalping_count, both_count,
        )

        # 5. 유저별 처리
        for user in target_users:
            try:
                await self._process_user(user, market_data, is_swing_hour=is_swing_hour)
            except Exception as exc:
                logger.error(
                    "AI 유저 처리 오류: user_id=%s err=%s", user.user_id, exc
                )
            await asyncio.sleep(1)

        logger.info("AI 펀드 매니저 루프 완료 (hour=%d)", current_hour)

    @fund_loop.before_loop
    async def before_fund_loop(self) -> None:
        """봇이 완전히 준비될 때까지 루프 첫 실행을 지연한다."""
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # 유저별 전체 처리 (실전·모의 이중 사이클)
    # ------------------------------------------------------------------

    async def _process_user(
        self,
        user: User,
        market_data: dict[str, dict],
        is_swing_hour: bool = True,
    ) -> None:
        """단일 유저에 대해 실전·모의 이중 사이클을 실행한다.

        처리 순서:
          1. 실전 포지션 리뷰  (is_ai_managed=True, is_paper_trading=False)
          2. 모의 포지션 리뷰  (is_paper_trading=True)
          3a. SWING 엔진 분석·매수  (engine_mode=SWING/BOTH이고 is_swing_hour=True 시에만)
          3b. SCALPING 엔진 분석·매수  (engine_mode=SCALPING/BOTH 매 사이클)
          4. 단일 통합 DM Embed 발송

        Args:
            user:          처리 대상 User 인스턴스 (bot_settings eagerly loaded).
            market_data:   MarketDataManager.get_all() 결과.
            is_swing_hour: 현재 KST 시각이 4h 봉 마감 시각인지 여부.
        """
        user_id = user.user_id

        # ── 엔진 모드 결정 (V2 신규 필드 우선, 구형 ai_trade_style 하위 호환) ──
        engine_mode = (getattr(user, "ai_engine_mode", "SWING") or "SWING").upper()
        if engine_mode not in ("SWING", "SCALPING", "BOTH"):
            old_style = (getattr(user, "ai_trade_style", "SWING") or "SWING").upper()
            engine_mode = "SCALPING" if old_style in ("SCALPING", "BEAST") else "SWING"

        # 현재 사이클에서 가동할 엔진 결정
        run_swing = engine_mode in ("SWING", "BOTH") and is_swing_hour
        run_scalp = engine_mode in ("SCALPING", "BOTH")

        # 엔진별 예산·비중 (V2 필드, 기본값으로 폴백)
        swing_budget = float(getattr(user, "ai_swing_budget_krw", 1_000_000))
        swing_weight = float(getattr(user, "ai_swing_weight_pct", 20))
        scalp_budget = float(getattr(user, "ai_scalp_budget_krw", 1_000_000))
        scalp_weight = float(getattr(user, "ai_scalp_weight_pct", 20))

        ws_manager = UpbitWebsocketManager.get()
        registry   = WorkerRegistry.get()

        # ── 활성 모드 결정 ────────────────────────────────────────────
        is_real_active  = (
            user.subscription_tier == SubscriptionTier.VIP
            and user.ai_mode_enabled
        )
        is_paper_active = user.ai_paper_mode_enabled

        # ── 실거래 Exchange 초기화 ────────────────────────────────────
        exchange: ExchangeService | None = None
        if is_real_active:
            if not user.upbit_access_key or not user.upbit_secret_key:
                logger.warning(
                    "업비트 API 키 없음, 실거래 스킵: user_id=%s", user_id
                )
                is_real_active = False
            else:
                exchange = ExchangeService(
                    access_key=user.upbit_access_key,
                    secret_key=user.upbit_secret_key,
                )

        # ── 모드별 실행 중 포지션 분류 ───────────────────────────────
        real_running: list[BotSetting] = (
            [
                s for s in user.bot_settings
                if s.is_running and not s.is_paper_trading and s.is_ai_managed
            ]
            if is_real_active
            else []
        )
        paper_running: list[BotSetting] = (
            [s for s in user.bot_settings if s.is_running and s.is_paper_trading]
            if is_paper_active
            else []
        )

        # ── DM 리포트 수집 버킷 ──────────────────────────────────────
        real_reviewed:    list[dict] = []
        real_bought:      list[dict] = []
        paper_reviewed:   list[dict] = []
        paper_bought:     list[dict] = []
        market_summaries: list[str]  = []

        # 리뷰용 엔진 타입 결정 (포지션의 trade_style과 무관하게 주 엔진 사용)
        _review_engine = "SCALPING" if engine_mode == "SCALPING" else "SWING"

        # ── Step 1: 실전 포지션 리뷰 ─────────────────────────────────
        if real_running:
            await self._review_existing_positions(
                user_id=user_id,
                running_settings=real_running,
                market_data=market_data,
                ws_manager=ws_manager,
                registry=registry,
                reviewed_positions=real_reviewed,
                engine_type=_review_engine,
            )

        # ── Step 2: 모의 포지션 리뷰 ─────────────────────────────────
        if paper_running:
            await self._review_existing_positions(
                user_id=user_id,
                running_settings=paper_running,
                market_data=market_data,
                ws_manager=ws_manager,
                registry=registry,
                reviewed_positions=paper_reviewed,
                engine_type=_review_engine,
            )

        # ── 연착륙 분기: 종료 모드 시 신규 매수 없이 리뷰만 완료 ──────
        is_shutting_down: bool = bool(getattr(user, "ai_is_shutting_down", False))
        if is_shutting_down:
            embed = self._build_unified_report_embed(
                market_summary=(
                    "🟡 **연착륙 진행 중** — 신규 매수를 중단했습니다.\n"
                    "보유 포지션이 모두 청산되면 AI가 자동 종료됩니다."
                ),
                real_reviewed=real_reviewed,
                real_bought=[],
                paper_reviewed=paper_reviewed,
                paper_bought=[],
                is_real_active=is_real_active,
                is_paper_active=is_paper_active,
                engine_mode=engine_mode,
                ai_max_coins=user.ai_max_coins,
                real_position_count=len(real_running),
                paper_position_count=len(paper_running),
            )
            await self._send_dm_embed(user_id, embed)

            if is_real_active:
                async with AsyncSessionLocal() as db:
                    remaining_result = await db.execute(
                        select(BotSetting).where(
                            BotSetting.user_id == user_id,
                            BotSetting.is_running.is_(True),
                            BotSetting.is_ai_managed.is_(True),
                            BotSetting.is_paper_trading.is_(False),
                        )
                    )
                    remaining = remaining_result.scalars().all()

                if not remaining:
                    async with AsyncSessionLocal() as db:
                        db_result = await db.execute(
                            select(User).where(User.user_id == user_id)
                        )
                        db_user = db_result.scalar_one_or_none()
                        if db_user:
                            db_user.ai_mode_enabled = False
                            db_user.ai_is_shutting_down = False
                            await db.commit()

                    logger.info("AI 연착륙 종료 완료 (포지션 전부 청산): user_id=%s", user_id)
                    completion_embed = discord.Embed(
                        title="🎉 AI 펀드 매니저 운용 완전 종료",
                        description=(
                            "모든 AI 포지션이 청산되어 **AI 운용이 완전히 종료**되었습니다.\n"
                            "다시 시작하려면 `/ai실전` 에서 AI 모드를 ON으로 설정하세요."
                        ),
                        color=discord.Color.green(),
                    )
                    await self._send_dm_embed(user_id, completion_embed)
            return

        # ── 슬롯·공통 상태 계산 ──────────────────────────────────────
        real_slots  = (user.ai_max_coins - len(real_running))  if is_real_active  else 0
        paper_slots = (user.ai_max_coins - len(paper_running)) if is_paper_active else 0
        holding_symbols: set[str] = {s.symbol for s in real_running + paper_running}

        # ── 실전 KRW 잔고 조회 (엔진별 예산 계산에 1회만 사용) ──────
        actual_krw = 0.0
        if is_real_active and exchange is not None:
            try:
                actual_krw = await exchange.fetch_krw_balance()
            except Exception as exc:
                logger.warning(
                    "KRW 잔고 조회 실패: user_id=%s err=%s", user_id, exc
                )

        # ── Step 3-a: SWING 엔진 분석·매수 ───────────────────────────
        if run_swing:
            swing_invested = sum(
                float(s.buy_amount_krw) for s in real_running
                if (s.trade_style or "").upper() in ("SWING", "SNIPER")
            )
            swing_remaining       = max(0.0, swing_budget - swing_invested)
            swing_real_available  = min(actual_krw, swing_remaining) if is_real_active else 0.0
            swing_paper_available = float(user.virtual_krw) if is_paper_active else 0.0

            logger.info(
                "AI SWING 예산: user_id=%s budget=%.0f invested=%.0f "
                "remaining=%.0f krw=%.0f → available=%.0f",
                user_id, swing_budget, swing_invested,
                swing_remaining, actual_krw, swing_real_available,
            )

            swing_analysis = await self.ai_service.analyze_market(
                market_data,
                holding_symbols,
                engine_type="SWING",
                weight_pct=swing_weight,
                available_krw=max(swing_real_available, swing_paper_available),
            )
            if swing_analysis.get("market_summary"):
                market_summaries.append(
                    f"📊 **스윙 엔진**\n{swing_analysis['market_summary']}"
                )
            swing_picks: list[dict] = swing_analysis.get("picks", [])

            if is_real_active and real_slots > 0 and swing_picks:
                await self._buy_new_coins(
                    user=user,
                    picks=swing_picks,
                    exchange=exchange,
                    ws_manager=ws_manager,
                    registry=registry,
                    bought_positions=real_bought,
                    market_data=market_data,
                    is_paper_mode=False,
                    available_krw=swing_real_available,
                    max_slots=real_slots,
                    engine_type="SWING",
                )
            elif is_real_active and real_slots <= 0:
                logger.info(
                    "최대 보유 종목 도달로 실전 SWING 매수 스킵 (보유=%d / 최대=%d): user_id=%s",
                    len(real_running), user.ai_max_coins, user_id,
                )
            elif is_real_active:
                logger.info("실전 SWING AI 신규 픽 없음 (관망): user_id=%s", user_id)

            if is_paper_active and paper_slots > 0 and swing_picks:
                await self._buy_new_coins(
                    user=user,
                    picks=swing_picks,
                    exchange=None,
                    ws_manager=ws_manager,
                    registry=registry,
                    bought_positions=paper_bought,
                    market_data=market_data,
                    is_paper_mode=True,
                    available_krw=swing_paper_available,
                    max_slots=paper_slots,
                    engine_type="SWING",
                )
            elif is_paper_active and paper_slots <= 0:
                logger.info(
                    "[모의] 최대 보유 종목 도달로 SWING 매수 스킵 (보유=%d / 최대=%d): user_id=%s",
                    len(paper_running), user.ai_max_coins, user_id,
                )
            elif is_paper_active:
                logger.info("모의 SWING AI 신규 픽 없음 (관망): user_id=%s", user_id)

            # BOTH 모드: 스윙 매수 후 슬롯·보유 집합 업데이트 (스캘핑 중복 방지)
            if engine_mode == "BOTH":
                real_slots  -= sum(1 for b in real_bought  if b.get("engine_type") == "SWING")
                paper_slots -= sum(1 for b in paper_bought if b.get("engine_type") == "SWING")
                holding_symbols |= {b["symbol"] for b in real_bought + paper_bought}

        # ── Step 3-b: SCALPING 엔진 분석·매수 ────────────────────────
        if run_scalp:
            scalp_invested = sum(
                float(s.buy_amount_krw) for s in real_running
                if (s.trade_style or "").upper() in ("SCALPING", "BEAST")
            )
            scalp_remaining       = max(0.0, scalp_budget - scalp_invested)
            scalp_real_available  = min(actual_krw, scalp_remaining) if is_real_active else 0.0
            scalp_paper_available = float(user.virtual_krw) if is_paper_active else 0.0

            logger.info(
                "AI SCALPING 예산: user_id=%s budget=%.0f invested=%.0f "
                "remaining=%.0f krw=%.0f → available=%.0f",
                user_id, scalp_budget, scalp_invested,
                scalp_remaining, actual_krw, scalp_real_available,
            )

            scalp_analysis = await self.ai_service.analyze_market(
                market_data,
                holding_symbols,
                engine_type="SCALPING",
                weight_pct=scalp_weight,
                available_krw=max(scalp_real_available, scalp_paper_available),
            )
            if scalp_analysis.get("market_summary"):
                market_summaries.append(
                    f"⚡ **스캘핑 엔진**\n{scalp_analysis['market_summary']}"
                )
            scalp_picks: list[dict] = scalp_analysis.get("picks", [])

            if is_real_active and real_slots > 0 and scalp_picks:
                await self._buy_new_coins(
                    user=user,
                    picks=scalp_picks,
                    exchange=exchange,
                    ws_manager=ws_manager,
                    registry=registry,
                    bought_positions=real_bought,
                    market_data=market_data,
                    is_paper_mode=False,
                    available_krw=scalp_real_available,
                    max_slots=real_slots,
                    engine_type="SCALPING",
                )
            elif is_real_active and real_slots <= 0:
                logger.info(
                    "최대 보유 종목 도달로 실전 SCALPING 매수 스킵 (보유=%d / 최대=%d): user_id=%s",
                    len(real_running), user.ai_max_coins, user_id,
                )
            elif is_real_active:
                logger.info("실전 SCALPING AI 신규 픽 없음 (관망): user_id=%s", user_id)

            if is_paper_active and paper_slots > 0 and scalp_picks:
                await self._buy_new_coins(
                    user=user,
                    picks=scalp_picks,
                    exchange=None,
                    ws_manager=ws_manager,
                    registry=registry,
                    bought_positions=paper_bought,
                    market_data=market_data,
                    is_paper_mode=True,
                    available_krw=scalp_paper_available,
                    max_slots=paper_slots,
                    engine_type="SCALPING",
                )
            elif is_paper_active and paper_slots <= 0:
                logger.info(
                    "[모의] 최대 보유 종목 도달로 SCALPING 매수 스킵 (보유=%d / 최대=%d): user_id=%s",
                    len(paper_running), user.ai_max_coins, user_id,
                )
            elif is_paper_active:
                logger.info("모의 SCALPING AI 신규 픽 없음 (관망): user_id=%s", user_id)

        # ── Step 4: 통합 DM Embed 발송 ───────────────────────────────
        market_summary = "\n\n".join(market_summaries) if market_summaries else ""
        embed = self._build_unified_report_embed(
            market_summary=market_summary,
            real_reviewed=real_reviewed,
            real_bought=real_bought,
            paper_reviewed=paper_reviewed,
            paper_bought=paper_bought,
            is_real_active=is_real_active,
            is_paper_active=is_paper_active,
            engine_mode=engine_mode,
            ai_max_coins=user.ai_max_coins,
            real_position_count=len(real_running) + len(real_bought),
            paper_position_count=len(paper_running) + len(paper_bought),
        )
        await self._send_dm_embed(user_id, embed)

    # ------------------------------------------------------------------
    # Step 1·2: 기존 포지션 리뷰 (실전·모의 공용)
    # ------------------------------------------------------------------

    async def _review_existing_positions(
        self,
        user_id: str,
        running_settings: list[BotSetting],
        market_data: dict[str, dict],
        ws_manager: UpbitWebsocketManager,
        registry: WorkerRegistry,
        reviewed_positions: list[dict],
        engine_type: str = "SWING",
    ) -> None:
        """보유 포지션을 AI로 리뷰해 UPDATE 시 DB·워커 인메모리를 동기화한다.

        실전·모의 구분 없이 동일한 로직으로 동작한다.

        Args:
            user_id:            Discord 사용자 ID.
            running_settings:   is_running=True 이며 모드별로 이미 필터링된 BotSetting 목록.
            market_data:        MarketDataManager.get_all() 결과.
            ws_manager:         UpbitWebsocketManager 인스턴스.
            registry:           WorkerRegistry 인스턴스.
            reviewed_positions: 결과를 축적할 리스트 (DM 리포트용).
            engine_type:        "SWING" 또는 "SCALPING" — 사용할 지표 타임프레임 결정.
        """
        positions_data: list[dict] = []
        for s in running_settings:
            if s.buy_price is None:
                continue
            current_price = ws_manager.get_price(s.symbol)
            if current_price is None:
                logger.warning(
                    "현재가 캐시 없음 (리뷰 스킵): user_id=%s symbol=%s",
                    user_id, s.symbol,
                )
                continue
            buy_price  = float(s.buy_price)
            profit_pct = (current_price - buy_price) / buy_price * 100
            positions_data.append(
                {
                    "setting_id":        s.id,
                    "symbol":            s.symbol,
                    "buy_price":         buy_price,
                    "current_price":     current_price,
                    "profit_pct":        profit_pct,
                    "target_profit_pct": float(s.target_profit_pct or 3.0),
                    "stop_loss_pct":     float(s.stop_loss_pct or 2.0),
                }
            )

        if not positions_data:
            return

        reviews = await self.ai_service.review_positions(
            positions_data, market_data, engine_type=engine_type,
        )
        if not reviews:
            logger.info("AI 포지션 리뷰 결과 없음: user_id=%s", user_id)
            return

        for review in reviews:
            symbol  = review["symbol"]
            action  = review["action"]
            new_tgt = review["new_target_profit_pct"]
            new_sl  = review["new_stop_loss_pct"]
            reason  = review["reason"]

            pos = next((p for p in positions_data if p["symbol"] == symbol), None)
            if pos is None:
                continue

            setting_id = pos["setting_id"]

            if action == "SELL":
                # 긴급 청산: 워커를 통한 즉시 시장가 매도 후 레지스트리 제거
                worker = registry.get_worker(setting_id)
                if worker:
                    sell_ok = await worker.force_sell(f"🤖 AI 긴급 청산: {reason}")
                    if sell_ok:
                        registry._workers.pop(setting_id, None)
                        logger.info(
                            "AI 긴급 청산 완료: user_id=%s symbol=%s profit_pct=%.2f%%",
                            user_id, symbol, pos["profit_pct"],
                        )
                    else:
                        logger.warning(
                            "AI 긴급 청산 실패 (force_sell 반환 False): user_id=%s symbol=%s",
                            user_id, symbol,
                        )
                else:
                    logger.warning(
                        "AI 긴급 청산: 워커 없음 (이미 종료됐을 수 있음): "
                        "user_id=%s symbol=%s setting_id=%s",
                        user_id, symbol, setting_id,
                    )

            elif action == "UPDATE":
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(BotSetting).where(BotSetting.id == setting_id)
                    )
                    setting = result.scalar_one_or_none()
                    if setting:
                        setting.target_profit_pct = new_tgt
                        setting.stop_loss_pct     = new_sl
                        await db.commit()

                worker = registry.get_worker(setting_id)
                if worker:
                    worker.target_profit_pct = new_tgt
                    worker.stop_loss_pct     = new_sl
                    if worker._position:
                        worker._position.target_profit_pct = new_tgt
                        worker._position.stop_loss_pct     = new_sl

                logger.info(
                    "AI 포지션 UPDATE: user_id=%s symbol=%s tgt=%.1f%% sl=%.1f%%",
                    user_id, symbol, new_tgt, new_sl,
                )

            reviewed_positions.append(
                {
                    "symbol":     symbol,
                    "action":     action,
                    "profit_pct": pos["profit_pct"],
                    "new_target": new_tgt,
                    "new_sl":     new_sl,
                    "reason":     reason,
                }
            )

    # ------------------------------------------------------------------
    # Step 4·5: 신규 종목 매수 (실거래 / 모의투자 분기)
    # ------------------------------------------------------------------

    async def _buy_new_coins(
        self,
        user: User,
        picks: list[dict],
        exchange: ExchangeService | None,
        ws_manager: UpbitWebsocketManager,
        registry: WorkerRegistry,
        bought_positions: list[dict],
        market_data: dict[str, dict],
        is_paper_mode: bool = False,
        available_krw: float = 0.0,
        max_slots: int = 0,
        engine_type: str = "SWING",
    ) -> None:
        """AI가 선정한 신규 종목을 매수(실거래) 또는 가상 체결(모의투자)하고 워커를 등록한다.

        매수 금액은 score/weight 기반으로 자동 산정한다:
          - score ≥ 80 인 픽만 채택 (미달 시 스킵)
          - weight_pct 합계가 100 초과 시 비례 정규화
          - trade_amount = available_krw × (weight_pct / 100)
          - trade_amount < 5,000 KRW 이면 최소 금액 미달로 스킵

        실거래:
          - ExchangeService.create_market_buy_order() 호출
          - BotSetting(is_ai_managed=True, is_paper_trading=False) 저장

        모의투자:
          - API 호출 없이 WS 현재가 × 슬리피지 0.1% 로 가상 체결
          - User.virtual_krw 차감 (사이클 내 remaining 로 과잉 차감 방지)
          - BotSetting(is_ai_managed=True, is_paper_trading=True) 저장

        Args:
            user:             처리 대상 User 인스턴스.
            picks:            AI 분석 전체 픽 리스트 (score/weight 필터는 내부에서 수행).
            exchange:         실거래 시 ExchangeService, 모의투자 시 None.
            ws_manager:       UpbitWebsocketManager 인스턴스.
            registry:         WorkerRegistry 인스턴스.
            bought_positions: 결과를 축적할 리스트 (DM 리포트용).
            market_data:      MarketDataManager.get_all() 결과 — WS 캐시 미스 시 Fallback 가격 제공.
            is_paper_mode:    True = 모의투자 / False = 실거래.
            available_krw:    이번 사이클 가용 예산 (비중 기반 매수금액 산출 기준).
            max_slots:        이번 사이클 최대 신규 매수 가능 슬롯 수.
            engine_type:      "SWING" 또는 "SCALPING" — BotSetting.trade_style 에 저장됨.
        """
        user_id = user.user_id

        # ── Score 필터: score ≥ 80 인 픽만 채택 ───────────────────────
        qualified_picks = [p for p in picks if (p.get("score") or 0) >= 80]
        if not qualified_picks:
            logger.info(
                "AI score ≥ 80 필터 후 유효 픽 없음: user_id=%s is_paper=%s",
                user_id, is_paper_mode,
            )
            return

        # ── Weight 정규화: 합계가 100%를 초과하면 비례 축소 ─────────
        total_weight = sum((p.get("weight_pct") or 0) for p in qualified_picks)
        if total_weight > 100:
            scale = 100.0 / total_weight
            for p in qualified_picks:
                p["weight_pct"] = (p.get("weight_pct") or 0) * scale
            logger.info(
                "AI weight 정규화 (합계 %.1f%% → 100%%): user_id=%s is_paper=%s",
                total_weight, user_id, is_paper_mode,
            )

        # 사이클 내 가상 잔고 추적 (동일 사이클 내 여러 종목 매수 시 과잉 차감 방지)
        remaining_virtual_krw: float = float(user.virtual_krw) if is_paper_mode else 0.0
        slots_used = 0

        for pick in qualified_picks:
            # 슬롯 한도 도달 시 중단
            if max_slots > 0 and slots_used >= max_slots:
                logger.info(
                    "슬롯 한도 도달, 신규 매수 중단: user_id=%s used=%d max=%d",
                    user_id, slots_used, max_slots,
                )
                break

            symbol        = pick["symbol"]
            target_profit = pick["target_profit_pct"]
            stop_loss     = pick["stop_loss_pct"]
            weight_pct    = pick.get("weight_pct") or 0.0
            score         = pick.get("score") or 0

            # ── 비중 기반 매수 금액 산출 ──────────────────────────────
            trade_amount = available_krw * (weight_pct / 100.0)
            safe_trade_amount = trade_amount  # 실거래 else 블록에서 덮어씀
            if trade_amount < 5_000:
                logger.info(
                    "비중 기반 매수 금액 미달 스킵 (%.0f KRW < 5,000): "
                    "user_id=%s symbol=%s weight=%.1f%%",
                    trade_amount, user_id, symbol, weight_pct,
                )
                continue

            try:
                current_price = ws_manager.get_price(symbol)

                # ── Fallback: WS 캐시 미스 시 market_data 가격 사용 ──────
                # AI가 픽한 신규 심볼은 웹소켓 구독이 아직 없을 수 있다.
                # MarketDataManager가 분석에 사용한 캐시 가격으로 대체해 매수를 보호한다.
                if current_price is None and symbol in market_data:
                    current_price = market_data[symbol].get("price")
                    if current_price is not None:
                        logger.info(
                            "현재가 WS 캐시 미스 → market_data Fallback 적용: "
                            "user_id=%s symbol=%s price=%.0f",
                            user_id, symbol, current_price,
                        )

                if current_price is None:
                    logger.warning(
                        "현재가 없음 (WS + market_data 모두 미스), 매수 스킵: "
                        "user_id=%s symbol=%s",
                        user_id, symbol,
                    )
                    continue

                # ── 엽전주 하드 가드: AI 환각·오류로 100원 미만 코인이 picks에
                # 포함되더라도 매수가 절대 체결되지 않도록 이중 방어한다. ──────
                if current_price < 100:
                    logger.warning(
                        "[AI DEBUG] 100원 미만 엽전주 매수 시도 차단: %s (가격: %s)",
                        symbol, current_price,
                    )
                    continue

                if is_paper_mode:
                    # ── 모의투자: 가상 잔고 체크 → 가상 체결 ──────────
                    if remaining_virtual_krw < trade_amount:
                        logger.warning(
                            "[모의투자] 가상 잔고 부족, 매수 중단: "
                            "user_id=%s balance=%.0f needed=%.0f",
                            user_id, remaining_virtual_krw, trade_amount,
                        )
                        break  # 잔고 부족 시 남은 픽도 처리 불가 → 루프 종료

                    # 슬리피지 0.1% 반영한 가상 체결가
                    fill_price  = current_price * 1.001
                    amount_coin = trade_amount / fill_price
                    buy_price   = fill_price

                    # 가상 잔고 DB 차감 (원자적 처리)
                    async with AsyncSessionLocal() as db:
                        result = await db.execute(
                            select(User).where(User.user_id == user_id)
                        )
                        db_user = result.scalar_one_or_none()
                        if db_user is None:
                            continue
                        # DB 값으로 이중 체크 (비동기 레이스 컨디션 방어)
                        if float(db_user.virtual_krw) < trade_amount:
                            logger.warning(
                                "[모의투자] 가상 잔고 부족(DB 재확인): user_id=%s balance=%.0f",
                                user_id, db_user.virtual_krw,
                            )
                            break
                        db_user.virtual_krw = float(db_user.virtual_krw) - trade_amount
                        await db.commit()

                    remaining_virtual_krw -= trade_amount
                    logger.info(
                        "[모의투자] 가상 매수: user_id=%s symbol=%s "
                        "fill_price=%.0f amount=%.6f remaining=%.0f",
                        user_id, symbol, fill_price, amount_coin, remaining_virtual_krw,
                    )

                else:
                    # ── 실거래: 업비트 시장가 매수 API 호출 ───────────
                    # 수수료(0.05%) 및 슬리피지 버퍼 0.1% 차감 후 정수화
                    safe_trade_amount = int(trade_amount * 0.999)
                    if safe_trade_amount < 5_000:
                        logger.info(
                            "수수료 버퍼 적용 후 최소 주문 금액 미달 스킵 "
                            "(%d KRW < 5,000): user_id=%s symbol=%s",
                            safe_trade_amount, user_id, symbol,
                        )
                        continue

                    order      = await exchange.create_market_buy_order(symbol, safe_trade_amount)
                    filled_qty = float(order.get("filled") or 0)
                    order_cost = float(order.get("cost") or 0)

                    # 체결 수량: API 응답 우선, 없으면 현재가 기준 추정
                    amount_coin = filled_qty if filled_qty > 0 else (safe_trade_amount / current_price)

                    # 평균 체결가 산출 (정확도 우선순위):
                    #  1) cost ÷ filled  — 실제 체결 데이터 기준 가장 정확
                    #  2) order["average"] — ccxt 정규화 필드 (업비트 즉시 응답에 없을 수 있음)
                    #  3) WS 현재가 — API 응답에 체결 정보가 없는 예외 상황 fallback
                    if filled_qty > 0 and order_cost > 0:
                        buy_price = order_cost / filled_qty
                    elif order.get("average"):
                        buy_price = float(order["average"])
                    else:
                        buy_price = current_price
                        logger.warning(
                            "실전 매수: 체결가 미확인 (WS 현재가로 대체): "
                            "user_id=%s symbol=%s price=%s",
                            user_id, symbol, current_price,
                        )

                # ── BotSetting DB 삽입 (실거래·모의투자 공통) ──────────
                # is_ai_managed=True 로 수동 봇 포지션과 격리.
                # buy_price·amount_coin 을 함께 저장해 TradingWorker 가
                # _decide_entry() 에서 '포지션 복구' 경로를 타도록 유도.
                async with AsyncSessionLocal() as db:
                    setting = BotSetting(
                        user_id=user_id,
                        symbol=symbol,
                        buy_amount_krw=safe_trade_amount,
                        target_profit_pct=target_profit,
                        stop_loss_pct=stop_loss,
                        is_running=True,
                        buy_price=buy_price,
                        amount_coin=amount_coin,
                        is_paper_trading=is_paper_mode,   # ← 모의/실전 격리 플래그
                        is_ai_managed=True,                # ← 수동 봇과의 격리 플래그
                        trade_style=engine_type,           # ← AI 메타데이터 (SWING/SCALPING)
                        ai_score=score,
                        ai_reason=pick.get("reason"),
                    )
                    db.add(setting)
                    await db.commit()
                    await db.refresh(setting)

                # ── TradingWorker 등록·시작 ───────────────────────────
                worker = TradingWorker(
                    setting_id=setting.id,
                    user_id=user_id,
                    symbol=symbol,
                    buy_amount_krw=safe_trade_amount,
                    target_profit_pct=target_profit,
                    stop_loss_pct=stop_loss,
                    exchange=None if is_paper_mode else exchange,
                    notify_callback=self.bot._send_dm,
                    is_paper_trading=is_paper_mode,
                )
                await registry.register(worker)
                worker.start()

                bought_positions.append(
                    {
                        "symbol":            symbol,
                        "reason":            pick["reason"],
                        "buy_price":         buy_price,
                        "amount_coin":       amount_coin,
                        "target_profit_pct": target_profit,
                        "stop_loss_pct":     stop_loss,
                        "score":             score,
                        "weight_pct":        weight_pct,
                        "trade_amount":      safe_trade_amount,
                        "engine_type":       engine_type,   # BOTH 모드 슬롯 추적용
                    }
                )
                slots_used += 1
                logger.info(
                    "AI %s매수 완료: user_id=%s symbol=%s price=%.0f amount=%.6f "
                    "score=%d weight=%.1f%% trade_amount=%.0f",
                    "[모의] " if is_paper_mode else "",
                    user_id, symbol, buy_price, amount_coin,
                    score, weight_pct, safe_trade_amount,
                )

            except Exception as exc:
                logger.error(
                    "AI 매수 실패: user_id=%s symbol=%s err=%s", user_id, symbol, exc
                )

            await asyncio.sleep(0.5)

    # ------------------------------------------------------------------
    # Step 6: 통합 리포트 Embed 빌드
    # ------------------------------------------------------------------

    @staticmethod
    def _build_unified_report_embed(
        market_summary: str,
        real_reviewed: list[dict],
        real_bought: list[dict],
        paper_reviewed: list[dict],
        paper_bought: list[dict],
        is_real_active: bool,
        is_paper_active: bool,
        engine_mode: str = "SWING",
        ai_max_coins: int = 3,
        real_position_count: int = 0,
        paper_position_count: int = 0,
    ) -> discord.Embed:
        """실전·모의투자 결과를 하나의 Embed로 통합한다.

        - 실전 항목: 일반 표기
        - 모의 항목: [🎮모의] 태그 명시
        - 두 모드 모두 없거나 관망이면 중립 색상

        Args:
            market_summary:        AI가 생성한 시장 전반 분석 요약.
            real_reviewed:         실전 포지션 리뷰 결과 리스트.
            real_bought:           실전 신규 매수 결과 리스트.
            paper_reviewed:        모의 포지션 리뷰 결과 리스트.
            paper_bought:          모의 신규 매수 결과 리스트.
            is_real_active:        이번 사이클에서 실전 모드가 활성 상태였는지.
            is_paper_active:       이번 사이클에서 모의 모드가 활성 상태였는지.
            engine_mode:           "SWING" / "SCALPING" / "BOTH" — 제목/푸터에 표시.
            ai_max_coins:          유저 설정 최대 동시 보유 종목 수.
            real_position_count:   이번 사이클 후 실전 보유 종목 수 (기존 + 신규).
            paper_position_count:  이번 사이클 후 모의 보유 종목 수 (기존 + 신규).

        Returns:
            단일 discord.Embed 객체.
        """
        total_real  = len(real_reviewed)  + len(real_bought)
        total_paper = len(paper_reviewed) + len(paper_bought)
        total       = total_real + total_paper

        # ── 엔진 모드 레이블 ──────────────────────────────────────────
        if engine_mode == "SCALPING":
            style_label = "⚡ 스캘핑 (1h 봉)"
        elif engine_mode == "BOTH":
            style_label = "🔥 동시 가동 (스윙+스캘핑)"
        else:
            style_label = "📊 듀얼 스윙 (4h 봉)"

        # ── 컬러·제목 결정 ────────────────────────────────────────────
        color = discord.Color.blue() if total > 0 else discord.Color.greyple()

        # ── 설명 문구 ─────────────────────────────────────────────────
        parts: list[str] = []
        if total_real  > 0: parts.append(f"실전 **{total_real}건**")
        if total_paper > 0: parts.append(f"모의 **{total_paper}건**")

        if parts:
            desc = " + ".join(parts) + " 처리"
        elif is_real_active and is_paper_active:
            desc = "실전·모의투자 전액 현금 관망 중 (돌파/역추세 타점 부재)"
        elif is_real_active:
            desc = "실전 전액 현금 관망 중 (돌파/역추세 타점 부재)"
        elif is_paper_active:
            desc = "모의투자 전액 가상 현금 관망 중 (돌파/역추세 타점 부재)"
        else:
            desc = "관망 중"

        embed = discord.Embed(
            title=f"🤖 AI 종합 리포트 [{style_label}]",
            description=desc,
            color=color,
        )

        # ── AI 시장 분석 요약 ─────────────────────────────────────────
        embed.add_field(
            name="📊 AI 시장 분석 요약",
            value=market_summary or "분석 결과를 가져오지 못했습니다.",
            inline=False,
        )

        # ── 포트폴리오 현황 (실전·모의 각각 [현재/최대] 슬롯 표시) ──
        portfolio_parts: list[str] = []
        if is_real_active:
            real_remaining = ai_max_coins - real_position_count
            portfolio_parts.append(
                f"실전: **[ {real_position_count} / {ai_max_coins} ]**"
                f"  _(빈 슬롯 {real_remaining}개)_"
            )
        if is_paper_active:
            paper_remaining = ai_max_coins - paper_position_count
            portfolio_parts.append(
                f"🎮모의: **[ {paper_position_count} / {ai_max_coins} ]**"
                f"  _(빈 슬롯 {paper_remaining}개)_"
            )
        if portfolio_parts:
            embed.add_field(
                name="📦 포트폴리오 현황",
                value="\n".join(portfolio_parts),
                inline=False,
            )

        # ════════════════════════════════════════════════════════════
        # 실전 AI 섹션
        # ════════════════════════════════════════════════════════════
        if is_real_active:
            # 긴급 청산 (SELL)
            sold_real = [r for r in real_reviewed if r["action"] == "SELL"]
            if sold_real:
                embed.add_field(
                    name=f"🚨 실전 긴급 청산 ({len(sold_real)}건)",
                    value="\u200b",
                    inline=False,
                )
                for item in sold_real:
                    icon = "📈" if item["profit_pct"] >= 0 else "📉"
                    embed.add_field(
                        name=f"🪙 {item['symbol']} [긴급청산]",
                        value=(
                            f"**청산 수익률:** {item['profit_pct']:+.2f}% {icon}\n"
                            f"**AI 판단:** {item['reason']}"
                        ),
                        inline=False,
                    )

            # 신규 매수
            if real_bought:
                embed.add_field(
                    name=f"🟢 신규 매수 ({len(real_bought)}건)",
                    value="\u200b",
                    inline=False,
                )
                for item in real_bought:
                    embed.add_field(
                        name=f"🪙 {item['symbol']} [신규]",
                        value=(
                            f"**매수가:** {format_krw_price(item['buy_price'])} KRW"
                            f"  |  **매수금액:** {item.get('trade_amount', 0):,.0f} KRW\n"
                            f"**수량:** {item['amount_coin']:.6f}\n"
                            f"**매력도:** {item.get('score', 0)}점"
                            f"  |  **비중:** {item.get('weight_pct', 0):.1f}%\n"
                            f"**익절:** +{item['target_profit_pct']:.1f}%  |  "
                            f"**손절:** -{item['stop_loss_pct']:.1f}%\n"
                            f"**AI 분석:** {item['reason']}"
                        ),
                        inline=False,
                    )

            # 목표 갱신
            updated_real = [r for r in real_reviewed if r["action"] == "UPDATE"]
            if updated_real:
                embed.add_field(
                    name=f"🔄 실전 목표 갱신 ({len(updated_real)}건)",
                    value="\u200b",
                    inline=False,
                )
                for item in updated_real:
                    icon = "📈" if item["profit_pct"] >= 0 else "📉"
                    embed.add_field(
                        name=f"🪙 {item['symbol']} [갱신]",
                        value=(
                            f"**현재 수익률:** {item['profit_pct']:+.2f}% {icon}\n"
                            f"**새 익절:** +{item['new_target']:.1f}%  |  "
                            f"**새 손절:** -{item['new_sl']:.1f}%\n"
                            f"**AI 판단:** {item['reason']}"
                        ),
                        inline=False,
                    )

            # 기존 유지 (HOLD)
            maintained_real = [r for r in real_reviewed if r["action"] == "HOLD"]
            if maintained_real:
                lines = [
                    f"• **{r['symbol']}** — {r['profit_pct']:+.2f}%"
                    f" {'📈' if r['profit_pct'] >= 0 else '📉'} — {r['reason']}"
                    for r in maintained_real
                ]
                embed.add_field(
                    name=f"✅ 실전 기존 유지 ({len(maintained_real)}건)",
                    value="\n".join(lines),
                    inline=False,
                )

            # 실전 완전 관망
            if not real_bought and not real_reviewed:
                embed.add_field(
                    name="💼 실전 AI",
                    value="전액 현금 관망 중 (추세 돌파·낙폭 반등 모두 진입 조건 미달)",
                    inline=False,
                )

        # ════════════════════════════════════════════════════════════
        # 모의투자 섹션 — 모든 항목에 [🎮모의] 태그
        # ════════════════════════════════════════════════════════════
        if is_paper_active:
            # 실전 섹션이 있으면 구분선 추가
            if is_real_active:
                embed.add_field(
                    name="━━━━━━━━━━━━━━━━━━━━━━",
                    value="\u200b",
                    inline=False,
                )

            # 모의 긴급 청산 (SELL)
            sold_paper = [r for r in paper_reviewed if r["action"] == "SELL"]
            if sold_paper:
                embed.add_field(
                    name=f"🚨 [🎮모의] 긴급 청산 ({len(sold_paper)}건)",
                    value="\u200b",
                    inline=False,
                )
                for item in sold_paper:
                    icon = "📈" if item["profit_pct"] >= 0 else "📉"
                    embed.add_field(
                        name=f"🪙 {item['symbol']} [🎮모의 긴급청산]",
                        value=(
                            f"**청산 수익률:** {item['profit_pct']:+.2f}% {icon}\n"
                            f"**AI 판단:** {item['reason']}"
                        ),
                        inline=False,
                    )

            # 신규 가상 매수
            if paper_bought:
                embed.add_field(
                    name=f"🟢 [🎮모의] 신규 가상 매수 ({len(paper_bought)}건)",
                    value="\u200b",
                    inline=False,
                )
                for item in paper_bought:
                    embed.add_field(
                        name=f"🪙 {item['symbol']} [🎮모의 신규]",
                        value=(
                            f"**매수가:** {format_krw_price(item['buy_price'])} KRW"
                            f" _(슬리피지 0.1% 반영)_"
                            f"  |  **매수금액:** {item.get('trade_amount', 0):,.0f} KRW\n"
                            f"**수량:** {item['amount_coin']:.6f}\n"
                            f"**매력도:** {item.get('score', 0)}점"
                            f"  |  **비중:** {item.get('weight_pct', 0):.1f}%\n"
                            f"**익절:** +{item['target_profit_pct']:.1f}%  |  "
                            f"**손절:** -{item['stop_loss_pct']:.1f}%\n"
                            f"**AI 분석:** {item['reason']}"
                        ),
                        inline=False,
                    )

            # 모의 목표 갱신
            updated_paper = [r for r in paper_reviewed if r["action"] == "UPDATE"]
            if updated_paper:
                embed.add_field(
                    name=f"🔄 [🎮모의] 목표 갱신 ({len(updated_paper)}건)",
                    value="\u200b",
                    inline=False,
                )
                for item in updated_paper:
                    icon = "📈" if item["profit_pct"] >= 0 else "📉"
                    embed.add_field(
                        name=f"🪙 {item['symbol']} [🎮모의 갱신]",
                        value=(
                            f"**현재 수익률:** {item['profit_pct']:+.2f}% {icon}\n"
                            f"**새 익절:** +{item['new_target']:.1f}%  |  "
                            f"**새 손절:** -{item['new_sl']:.1f}%\n"
                            f"**AI 판단:** {item['reason']}"
                        ),
                        inline=False,
                    )

            # 모의 기존 유지 (HOLD)
            maintained_paper = [r for r in paper_reviewed if r["action"] == "HOLD"]
            if maintained_paper:
                lines = [
                    f"• **{r['symbol']}** — {r['profit_pct']:+.2f}%"
                    f" {'📈' if r['profit_pct'] >= 0 else '📉'} — {r['reason']}"
                    for r in maintained_paper
                ]
                embed.add_field(
                    name=f"✅ [🎮모의] 기존 유지 ({len(maintained_paper)}건)",
                    value="\n".join(lines),
                    inline=False,
                )

            # 모의 완전 관망
            if not paper_bought and not paper_reviewed:
                embed.add_field(
                    name="🎮 모의투자",
                    value="전액 가상 현금 관망 중 (추세 돌파·낙폭 반등 모두 진입 조건 미달)",
                    inline=False,
                )

        # ── 푸터: 다음 실행 시각 (투자 성향 반영) ────────────────────
        _style_for_next = "SCALPING" if engine_mode in ("SCALPING", "BOTH") else "SWING"
        next_time = get_next_run_time_for_style(_style_for_next)
        footer_parts: list[str] = []
        if is_real_active:  footer_parts.append("실전")
        if is_paper_active: footer_parts.append("🎮모의")
        mode_str = " + ".join(footer_parts) if footer_parts else "AI"
        embed.set_footer(
            text=f"{mode_str} | {style_label} | 익절·손절은 워커가 자동 처리 | 다음 리포트: {next_time}"
        )
        return embed

    # ------------------------------------------------------------------
    # DM 전송 (Embed, 최대 3회 재시도)
    # ------------------------------------------------------------------

    async def _send_dm_embed(self, user_id: str, embed: discord.Embed) -> None:
        """사용자에게 Embed DM을 전송한다. HTTPException 시 최대 3회 재시도.

        Args:
            user_id: Discord 사용자 ID (문자열).
            embed:   전송할 discord.Embed 객체.
        """
        for attempt in range(1, 4):
            try:
                user = await self.bot.fetch_user(int(user_id))
                await user.send(embed=embed)
                return
            except discord.Forbidden:
                logger.warning("AI 리포트 DM 거부됨 (DM 차단): user_id=%s", user_id)
                return
            except discord.HTTPException as exc:
                if attempt < 3:
                    logger.warning(
                        "AI 리포트 DM 실패 (시도 %d/3, HTTP %s): user_id=%s — 3초 후 재시도",
                        attempt, exc.status, user_id,
                    )
                    await asyncio.sleep(3)
                else:
                    logger.error(
                        "AI 리포트 DM 최종 실패 (3회, HTTP %s): user_id=%s",
                        exc.status, user_id,
                    )
            except Exception as exc:
                logger.error(
                    "AI 리포트 DM 오류 (재시도 불가): user_id=%s err=%s", user_id, exc
                )
                return
