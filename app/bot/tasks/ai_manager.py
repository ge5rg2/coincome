"""
AIFundManagerTask: 업비트 4시간 봉 완성 정각(KST)에 동기화되어 실행되는 AI 펀드 매니저.

트리거 시각 (KST, 업비트 4h 봉 마감 정각):
  01:00 / 05:00 / 09:00 / 13:00 / 17:00 / 21:00

처리 대상 (단일 OR 쿼리):
  (VIP + ai_mode_enabled=True) OR ai_paper_mode_enabled=True  — is_active=True 조건 공통
  → 두 모드를 동시에 켠 유저도 단일 _process_user 호출로 처리.

격리 아키텍처:
  ┌─ _process_user(user) ──────────────────────────────────────────┐
  │  is_real  = VIP AND ai_mode_enabled                            │
  │  is_paper = ai_paper_mode_enabled                              │
  │                                                                │
  │  [Step 1] 실전 포지션 리뷰  (is_ai_managed=True, is_paper=False) │
  │  [Step 2] 모의 포지션 리뷰  (is_paper=True)                     │
  │  [Step 3] 시장 분석 1회     (실전·모의 공유, API 비용 절감)      │
  │  [Step 4] 실전 매수 사이클  → ExchangeService.create_market_buy │
  │           BotSetting(is_ai_managed=True, is_paper=False)       │
  │  [Step 5] 모의 매수 사이클  → virtual_krw 차감 (API 없음)       │
  │           BotSetting(is_ai_managed=True, is_paper=True)        │
  │  [Step 6] 단일 통합 DM Embed 발송                              │
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
from app.utils.time import get_next_ai_run_time

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 업비트 4시간 봉 마감 정각 (KST)
# ------------------------------------------------------------------

_KST = ZoneInfo("Asia/Seoul")

_LOOP_TIMES: list[datetime.time] = [
    datetime.time(hour=1,  minute=0, second=0, tzinfo=_KST),
    datetime.time(hour=5,  minute=0, second=0, tzinfo=_KST),
    datetime.time(hour=9,  minute=0, second=0, tzinfo=_KST),
    datetime.time(hour=13, minute=0, second=0, tzinfo=_KST),
    datetime.time(hour=17, minute=0, second=0, tzinfo=_KST),
    datetime.time(hour=21, minute=0, second=0, tzinfo=_KST),
]


class AIFundManagerTask(commands.Cog):
    """4시간마다 포지션 리뷰·신규 매수·DM 통합 리포트를 수행하는 백그라운드 Cog.

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
    # 업비트 4시간 봉 정각 루프 (KST 01/05/09/13/17/21시)
    # ------------------------------------------------------------------

    @tasks.loop(time=_LOOP_TIMES)
    async def fund_loop(self) -> None:
        """업비트 4h 봉 완성 정각(KST)에 포지션 리뷰·신규 매수를 실행하는 메인 루프.

        처리 흐름:
          0. 10초 대기 (업비트 서버 캔들 롤오버 안정화)
          1. MarketDataManager 캐시 존재 확인
          2. DB: (VIP + ai_mode_enabled) OR ai_paper_mode_enabled 유저 단일 쿼리
          3. 각 유저별 _process_user() 실행 (실전·모의 이중 사이클 처리)
        """
        await asyncio.sleep(10)
        logger.info("AI 펀드 매니저 루프 실행")

        # 1. 마켓 데이터 캐시 확인
        market_data = MarketDataManager.get().get_all()
        if not market_data:
            logger.warning("AI 펀드 매니저: 마켓 데이터 캐시 없음 — 스킵")
            return

        # 2. 단일 OR 쿼리: (VIP + ai_mode_enabled) OR ai_paper_mode_enabled
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
            target_users: list[User] = result.scalars().all()

        if not target_users:
            logger.info("AI 펀드 매니저: 대상 유저 없음")
            return

        # 실전·모의 동시 활성 유저 수 집계 (로그용)
        real_count  = sum(
            1 for u in target_users
            if u.subscription_tier == SubscriptionTier.VIP and u.ai_mode_enabled
        )
        paper_count = sum(1 for u in target_users if u.ai_paper_mode_enabled)
        logger.info(
            "AI 펀드 매니저 대상: 전체=%d명 (실전=%d명 / 모의=%d명, 중복 포함)",
            len(target_users), real_count, paper_count,
        )

        # 3. 유저별 처리
        for user in target_users:
            try:
                await self._process_user(user, market_data)
            except Exception as exc:
                logger.error(
                    "AI 유저 처리 오류: user_id=%s err=%s", user.user_id, exc
                )
            await asyncio.sleep(1)

        logger.info("AI 펀드 매니저 루프 완료")

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
    ) -> None:
        """단일 유저에 대해 실전·모의 이중 사이클을 실행한다.

        처리 순서:
          1. 실전 포지션 리뷰  (is_ai_managed=True, is_paper_trading=False)
          2. 모의 포지션 리뷰  (is_paper_trading=True)
          3. 시장 분석 1회     (실전·모의 공유 — API 비용 절감)
          4. 실전 신규 매수    (ExchangeService 호출, VIP only)
          5. 모의 신규 매수    (virtual_krw 차감, API 호출 없음)
          6. 단일 통합 DM Embed 발송

        Args:
            user:        처리 대상 User 인스턴스 (bot_settings eagerly loaded).
            market_data: MarketDataManager.get_all() 결과.
        """
        user_id   = user.user_id
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
        # is_ai_managed=True 로 수동 봇 설정과 완전 격리
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
        real_reviewed:  list[dict] = []
        real_bought:    list[dict] = []
        paper_reviewed: list[dict] = []
        paper_bought:   list[dict] = []

        # ── Step 1: 실전 포지션 리뷰 ─────────────────────────────────
        if real_running:
            await self._review_existing_positions(
                user_id=user_id,
                running_settings=real_running,
                market_data=market_data,
                ws_manager=ws_manager,
                registry=registry,
                reviewed_positions=real_reviewed,
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
            )

        # ── Step 3: 시장 분석 1회 (실전·모의 공유) ───────────────────
        real_slots  = (user.ai_max_coins - len(real_running))  if is_real_active  else 0
        paper_slots = (user.ai_max_coins - len(paper_running)) if is_paper_active else 0
        holding_symbols: set[str] = {s.symbol for s in real_running + paper_running}

        analysis       = await self.ai_service.analyze_market(market_data, holding_symbols)
        market_summary = analysis.get("market_summary", "")
        picks: list[dict] = analysis.get("picks", [])

        # ── Step 4: 실전 신규 매수 사이클 ────────────────────────────
        if is_real_active and real_slots > 0 and picks:
            await self._buy_new_coins(
                user=user,
                picks=picks[:real_slots],
                exchange=exchange,
                ws_manager=ws_manager,
                registry=registry,
                bought_positions=real_bought,
                is_paper_mode=False,
            )
        elif is_real_active and real_slots <= 0:
            logger.info(
                "실전 슬롯 없음 (보유=%d / 최대=%d): user_id=%s",
                len(real_running), user.ai_max_coins, user_id,
            )
        elif is_real_active:
            logger.info("실전 AI 신규 픽 없음 (관망): user_id=%s", user_id)

        # ── Step 5: 모의 신규 매수 사이클 ────────────────────────────
        if is_paper_active and paper_slots > 0 and picks:
            await self._buy_new_coins(
                user=user,
                picks=picks[:paper_slots],
                exchange=None,
                ws_manager=ws_manager,
                registry=registry,
                bought_positions=paper_bought,
                is_paper_mode=True,
            )
        elif is_paper_active and paper_slots <= 0:
            logger.info(
                "모의 슬롯 없음 (보유=%d / 최대=%d): user_id=%s",
                len(paper_running), user.ai_max_coins, user_id,
            )
        elif is_paper_active:
            logger.info("모의 AI 신규 픽 없음 (관망): user_id=%s", user_id)

        # ── Step 6: 통합 DM Embed 발송 ───────────────────────────────
        embed = self._build_unified_report_embed(
            market_summary=market_summary,
            real_reviewed=real_reviewed,
            real_bought=real_bought,
            paper_reviewed=paper_reviewed,
            paper_bought=paper_bought,
            is_real_active=is_real_active,
            is_paper_active=is_paper_active,
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
    ) -> None:
        """보유 포지션을 AI로 리뷰해 UPDATE 시 DB·워커 인메모리를 동기화한다.

        실전·모의 구분 없이 동일한 로직으로 동작한다.
        (포지션 리뷰는 AI가 시장 데이터 기반으로 목표값만 조정하므로 모드 무관)

        Args:
            user_id:            Discord 사용자 ID.
            running_settings:   is_running=True 이며 모드별로 이미 필터링된 BotSetting 목록.
            market_data:        MarketDataManager.get_all() 결과.
            ws_manager:         UpbitWebsocketManager 인스턴스.
            registry:           WorkerRegistry 인스턴스.
            reviewed_positions: 결과를 축적할 리스트 (DM 리포트용).
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

        reviews = await self.ai_service.review_positions(positions_data, market_data)
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

            if action == "UPDATE":
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
        is_paper_mode: bool = False,
    ) -> None:
        """AI가 선정한 신규 종목을 매수(실거래) 또는 가상 체결(모의투자)하고 워커를 등록한다.

        실거래:
          - ExchangeService.create_market_buy_order() 호출
          - BotSetting(is_ai_managed=True, is_paper_trading=False) 저장

        모의투자:
          - API 호출 없이 WS 현재가 × 슬리피지 0.1% 로 가상 체결
          - User.virtual_krw 차감 (사이클 내 remaining 로 과잉 차감 방지)
          - BotSetting(is_ai_managed=True, is_paper_trading=True) 저장

        Args:
            user:             처리 대상 User 인스턴스.
            picks:            _process_user에서 슬롯 수만큼 슬라이싱된 픽 리스트.
            exchange:         실거래 시 ExchangeService, 모의투자 시 None.
            ws_manager:       UpbitWebsocketManager 인스턴스.
            registry:         WorkerRegistry 인스턴스.
            bought_positions: 결과를 축적할 리스트 (DM 리포트용).
            is_paper_mode:    True = 모의투자 / False = 실거래.
        """
        user_id      = user.user_id
        trade_amount = float(user.ai_trade_amount)

        # 사이클 내 가상 잔고 추적 (동일 사이클 내 여러 종목 매수 시 과잉 차감 방지)
        remaining_virtual_krw: float = float(user.virtual_krw) if is_paper_mode else 0.0

        for pick in picks:
            symbol        = pick["symbol"]
            target_profit = pick["target_profit_pct"]
            stop_loss     = pick["stop_loss_pct"]

            try:
                current_price = ws_manager.get_price(symbol)
                if current_price is None:
                    logger.warning(
                        "현재가 캐시 없음, 매수 스킵: user_id=%s symbol=%s",
                        user_id, symbol,
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
                    order       = await exchange.create_market_buy_order(symbol, trade_amount)
                    amount_coin = float(order.get("filled") or 0) or (
                        trade_amount / current_price
                    )
                    buy_price   = float(order.get("average") or current_price)

                # ── BotSetting DB 삽입 (실거래·모의투자 공통) ──────────
                # is_ai_managed=True 로 수동 봇 포지션과 격리.
                # buy_price·amount_coin 을 함께 저장해 TradingWorker 가
                # _decide_entry() 에서 '포지션 복구' 경로를 타도록 유도.
                async with AsyncSessionLocal() as db:
                    setting = BotSetting(
                        user_id=user_id,
                        symbol=symbol,
                        buy_amount_krw=trade_amount,
                        target_profit_pct=target_profit,
                        stop_loss_pct=stop_loss,
                        is_running=True,
                        buy_price=buy_price,
                        amount_coin=amount_coin,
                        is_paper_trading=is_paper_mode,   # ← 모의/실전 격리 플래그
                        is_ai_managed=True,                # ← 수동 봇과의 격리 플래그
                    )
                    db.add(setting)
                    await db.commit()
                    await db.refresh(setting)

                # ── TradingWorker 등록·시작 ───────────────────────────
                worker = TradingWorker(
                    setting_id=setting.id,
                    user_id=user_id,
                    symbol=symbol,
                    buy_amount_krw=trade_amount,
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
                    }
                )
                logger.info(
                    "AI %s매수 완료: user_id=%s symbol=%s price=%.0f amount=%.6f",
                    "[모의] " if is_paper_mode else "",
                    user_id, symbol, buy_price, amount_coin,
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
    ) -> discord.Embed:
        """실전·모의투자 결과를 하나의 Embed로 통합한다.

        - 실전 항목: 일반 표기
        - 모의 항목: [🎮모의] 태그 명시
        - 두 모드 모두 없거나 관망이면 중립 색상

        Args:
            market_summary:  AI가 생성한 시장 전반 분석 요약.
            real_reviewed:   실전 포지션 리뷰 결과 리스트.
            real_bought:     실전 신규 매수 결과 리스트.
            paper_reviewed:  모의 포지션 리뷰 결과 리스트.
            paper_bought:    모의 신규 매수 결과 리스트.
            is_real_active:  이번 사이클에서 실전 모드가 활성 상태였는지.
            is_paper_active: 이번 사이클에서 모의 모드가 활성 상태였는지.

        Returns:
            단일 discord.Embed 객체.
        """
        total_real  = len(real_reviewed)  + len(real_bought)
        total_paper = len(paper_reviewed) + len(paper_bought)
        total       = total_real + total_paper

        # ── 컬러·제목 결정 ────────────────────────────────────────────
        if total > 0:
            color = discord.Color.blue()
        else:
            color = discord.Color.greyple()

        # ── 설명 문구 ─────────────────────────────────────────────────
        parts: list[str] = []
        if total_real  > 0: parts.append(f"실전 **{total_real}건**")
        if total_paper > 0: parts.append(f"모의 **{total_paper}건**")

        if parts:
            desc = " + ".join(parts) + " 처리"
        elif is_real_active and is_paper_active:
            desc = "실전·모의투자 전액 현금 관망 중"
        elif is_real_active:
            desc = "실전 전액 현금 관망 중"
        elif is_paper_active:
            desc = "모의투자 전액 가상 현금 관망 중"
        else:
            desc = "관망 중"

        embed = discord.Embed(
            title="🤖 AI 4시간 종합 리포트",
            description=desc,
            color=color,
        )

        # ── AI 시장 분석 요약 ─────────────────────────────────────────
        embed.add_field(
            name="📊 AI 시장 분석 요약",
            value=market_summary or "분석 결과를 가져오지 못했습니다.",
            inline=False,
        )

        # ════════════════════════════════════════════════════════════
        # 실전 AI 섹션
        # ════════════════════════════════════════════════════════════
        if is_real_active:
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
                            f"**매수가:** {item['buy_price']:,.0f} KRW\n"
                            f"**수량:** {item['amount_coin']:.6f}\n"
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

            # 기존 유지
            maintained_real = [r for r in real_reviewed if r["action"] == "MAINTAIN"]
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
                    value="전액 현금 관망 중 (신규 진입 조건 미달)",
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
                            f"**매수가:** {item['buy_price']:,.0f} KRW"
                            f" _(슬리피지 0.1% 반영)_\n"
                            f"**수량:** {item['amount_coin']:.6f}\n"
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

            # 모의 기존 유지
            maintained_paper = [r for r in paper_reviewed if r["action"] == "MAINTAIN"]
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
                    value="전액 가상 현금 관망 중 (신규 진입 조건 미달)",
                    inline=False,
                )

        next_time = get_next_ai_run_time()
        footer_parts: list[str] = []
        if is_real_active:  footer_parts.append("실전")
        if is_paper_active: footer_parts.append("🎮모의")
        mode_str = " + ".join(footer_parts) if footer_parts else "AI"
        embed.set_footer(
            text=f"{mode_str} | 익절·손절은 워커가 자동 처리 | 다음 리포트: {next_time}"
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
