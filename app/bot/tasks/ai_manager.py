"""
AIFundManagerTask: 업비트 4시간 봉 완성 정각(KST)에 동기화되어 실행되는 AI 펀드 매니저.

트리거 시각 (KST, 업비트 4h 봉 마감 정각):
  01:00 / 05:00 / 09:00 / 13:00 / 17:00 / 21:00

처리 대상:
  [실거래] VIP + ai_mode_enabled=True + is_active=True
  [모의투자] ai_paper_mode_enabled=True + is_active=True  (등급 무관, API 키 불필요)
  → 두 그룹은 is_paper_trading 플래그로 BotSetting·TradingWorker 수준에서 완전 격리

전체 데이터 흐름 (1 사이클):
  await asyncio.sleep(10)   ← 업비트 캔들 롤오버 대기
      ↓
  MarketDataManager._cache
      ↓  get_all()
  [Step 1] 기존 포지션 리뷰
      AITraderService.review_positions(positions_data, market_data)
          ↓  reviews (MAINTAIN / UPDATE)
      UPDATE: BotSetting DB 갱신 + TradingWorker 인메모리 동기화
  [Step 2] 신규 종목 발굴 (슬롯 여유가 있을 때만)
      current_count = is_running 포지션 수 (is_paper_trading 기준으로 각각 집계)
      slots = ai_max_coins - current_count
      AITraderService.analyze_market(market_data, holding_symbols)
          ↓  picks (최대 slots 개)
      [실거래]  ExchangeService.create_market_buy_order() → BotSetting + TradingWorker
      [모의투자] WS 가격 + 슬리피지 0.1% → virtual_krw 차감 → BotSetting(is_paper_trading=True)
  [Step 3] 단일 통합 DM Embed 전송
      신규 매수 + 갱신된 포지션 + 유지된 포지션 모두 1개 Embed로 전송

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
from sqlalchemy import select
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
          2. DB: [실거래] VIP + ai_mode=True 유저 조회
                 [모의투자] ai_paper_mode=True 유저 조회
          3. 각 유저별 _process_user() 실행 (is_paper_mode 플래그로 분기)
        """
        await asyncio.sleep(10)
        logger.info("AI 펀드 매니저 루프 실행")

        # 1. 마켓 데이터 캐시 확인
        market_data = MarketDataManager.get().get_all()
        if not market_data:
            logger.warning("AI 펀드 매니저: 마켓 데이터 캐시 없음 — 스킵")
            return

        # 2. 실거래 대상 조회 (VIP + ai_mode_enabled)
        async with AsyncSessionLocal() as db:
            real_result = await db.execute(
                select(User)
                .where(
                    User.subscription_tier == SubscriptionTier.VIP,
                    User.ai_mode_enabled.is_(True),
                    User.is_active.is_(True),
                )
                .options(selectinload(User.bot_settings))
            )
            real_users: list[User] = real_result.scalars().all()

        # 3. 모의투자 대상 조회 (ai_paper_mode_enabled, 등급 무관)
        async with AsyncSessionLocal() as db:
            paper_result = await db.execute(
                select(User)
                .where(
                    User.ai_paper_mode_enabled.is_(True),
                    User.is_active.is_(True),
                )
                .options(selectinload(User.bot_settings))
            )
            paper_users: list[User] = paper_result.scalars().all()

        if not real_users and not paper_users:
            logger.info("AI 펀드 매니저: 대상 유저 없음")
            return

        logger.info(
            "AI 펀드 매니저 대상: 실거래=%d명, 모의투자=%d명",
            len(real_users), len(paper_users),
        )

        # 4. 실거래 유저 처리
        for user in real_users:
            try:
                await self._process_user(user, market_data, is_paper_mode=False)
            except Exception as exc:
                logger.error(
                    "AI 실거래 처리 오류: user_id=%s err=%s", user.user_id, exc
                )
            await asyncio.sleep(1)

        # 5. 모의투자 유저 처리
        for user in paper_users:
            try:
                await self._process_user(user, market_data, is_paper_mode=True)
            except Exception as exc:
                logger.error(
                    "AI 모의투자 처리 오류: user_id=%s err=%s", user.user_id, exc
                )
            await asyncio.sleep(1)

        logger.info("AI 펀드 매니저 루프 완료")

    @fund_loop.before_loop
    async def before_fund_loop(self) -> None:
        """봇이 완전히 준비될 때까지 루프 첫 실행을 지연한다."""
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # 유저별 전체 처리
    # ------------------------------------------------------------------

    async def _process_user(
        self,
        user: User,
        market_data: dict[str, dict],
        is_paper_mode: bool = False,
    ) -> None:
        """단일 유저에 대해 포지션 리뷰 → 신규 매수 → DM 통합 리포트를 실행한다.

        Args:
            user:          처리 대상 User 인스턴스 (bot_settings eagerly loaded).
            market_data:   MarketDataManager.get_all() 결과.
            is_paper_mode: True = 모의투자 (API 키 불필요, virtual_krw 사용).
                           False = 실거래 (API 키 필수, 실제 업비트 주문).
        """
        user_id = user.user_id
        ws_manager = UpbitWebsocketManager.get()
        registry = WorkerRegistry.get()

        if is_paper_mode:
            # ── 모의투자: API 키 없이 가상 잔고 사용 ─────────────────
            exchange = None
            # 모의투자 포지션만 카운트 (실거래 포지션과 슬롯 완전 분리)
            running_settings = [
                s for s in user.bot_settings
                if s.is_running and s.is_paper_trading
            ]
        else:
            # ── 실거래: API 키 필수 ───────────────────────────────────
            if not user.upbit_access_key or not user.upbit_secret_key:
                logger.warning("업비트 API 키 없음, 실거래 스킵: user_id=%s", user_id)
                return
            exchange = ExchangeService(
                access_key=user.upbit_access_key,
                secret_key=user.upbit_secret_key,
            )
            # 실거래 포지션만 카운트
            running_settings = [
                s for s in user.bot_settings
                if s.is_running and not s.is_paper_trading
            ]

        ai_max_coins = user.ai_max_coins

        # DM 통합 리포트용 수집 버킷
        reviewed_positions: list[dict] = []
        bought_positions: list[dict] = []

        # ── Step 1: 기존 포지션 리뷰 ──────────────────────────────────
        if running_settings:
            await self._review_existing_positions(
                user_id=user_id,
                running_settings=running_settings,
                market_data=market_data,
                ws_manager=ws_manager,
                registry=registry,
                reviewed_positions=reviewed_positions,
            )

        # ── Step 2: 시장 분석 (슬롯 유무와 무관하게 항상 실행) ──────
        current_count = len(running_settings)
        slots = ai_max_coins - current_count
        holding_symbols: set[str] = {s.symbol for s in running_settings}

        analysis = await self.ai_service.analyze_market(market_data, holding_symbols)
        market_summary = analysis.get("market_summary", "")
        picks = analysis.get("picks", [])

        if slots > 0 and picks:
            await self._buy_new_coins(
                user=user,
                picks=picks[:slots],
                exchange=exchange,
                ws_manager=ws_manager,
                registry=registry,
                bought_positions=bought_positions,
                is_paper_mode=is_paper_mode,
            )
        elif slots <= 0:
            logger.info(
                "AI 펀드 매니저: 슬롯 없음 (보유=%d / 최대=%d) — 신규 매수 스킵: user_id=%s",
                current_count, ai_max_coins, user_id,
            )
        else:
            logger.info("AI 신규 픽 없음 (관망): user_id=%s", user_id)

        # ── Step 3: 단일 통합 DM Embed 무조건 전송 ───────────────────
        embed = self._build_unified_report_embed(
            market_summary=market_summary,
            reviewed_positions=reviewed_positions,
            bought_positions=bought_positions,
            is_paper_mode=is_paper_mode,
        )
        await self._send_dm_embed(user_id, embed)

    # ------------------------------------------------------------------
    # Step 1: 기존 포지션 리뷰
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

        실거래·모의투자 구분 없이 동일한 로직으로 동작한다.
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
            buy_price = float(s.buy_price)
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
                        setting.stop_loss_pct = new_sl
                        await db.commit()

                worker = registry.get_worker(setting_id)
                if worker:
                    worker.target_profit_pct = new_tgt
                    worker.stop_loss_pct = new_sl
                    if worker._position:
                        worker._position.target_profit_pct = new_tgt
                        worker._position.stop_loss_pct = new_sl

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
    # Step 2: 신규 종목 매수 (실거래 / 모의투자 분기)
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

        Args:
            user:             처리 대상 User 인스턴스.
            picks:            _process_user에서 이미 슬롯 수만큼 슬라이싱된 픽 리스트.
            exchange:         실거래 시 ExchangeService 인스턴스, 모의투자 시 None.
            ws_manager:       UpbitWebsocketManager 인스턴스.
            registry:         WorkerRegistry 인스턴스.
            bought_positions: 결과를 축적할 리스트 (DM 리포트용).
            is_paper_mode:    True = 모의투자 / False = 실거래.
        """
        user_id = user.user_id
        trade_amount = float(user.ai_trade_amount)

        # 모의투자 사이클 내 가상 잔고 추적 (같은 사이클에서 여러 종목 매수 시 과잉 차감 방지)
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
                    # ── 모의투자: 가상 잔고 체크 → 가상 체결 ─────────
                    if remaining_virtual_krw < trade_amount:
                        logger.warning(
                            "[모의투자] 가상 잔고 부족, 매수 중단: "
                            "user_id=%s balance=%.0f needed=%.0f",
                            user_id, remaining_virtual_krw, trade_amount,
                        )
                        break  # 잔고 부족 시 남은 픽도 처리할 수 없으므로 루프 종료

                    # 슬리피지 0.1% 반영한 가상 체결가
                    fill_price = current_price * 1.001
                    amount_coin = trade_amount / fill_price
                    buy_price = fill_price

                    # 가상 잔고 DB 차감 (원자적 처리)
                    async with AsyncSessionLocal() as db:
                        result = await db.execute(
                            select(User).where(User.user_id == user_id)
                        )
                        db_user = result.scalar_one_or_none()
                        if db_user is None:
                            continue
                        # DB 값으로 이중 체크 (다른 요청에 의한 차감 대비)
                        if float(db_user.virtual_krw) < trade_amount:
                            logger.warning(
                                "[모의투자] 가상 잔고 부족(DB 재확인): user_id=%s balance=%.0f",
                                user_id, db_user.virtual_krw,
                            )
                            break
                        db_user.virtual_krw = float(db_user.virtual_krw) - trade_amount
                        await db.commit()

                    # 사이클 내 인메모리 잔고 추적 업데이트
                    remaining_virtual_krw -= trade_amount
                    logger.info(
                        "[모의투자] 가상 매수: user_id=%s symbol=%s "
                        "fill_price=%.0f amount=%.6f remaining=%.0f",
                        user_id, symbol, fill_price, amount_coin, remaining_virtual_krw,
                    )

                else:
                    # ── 실거래: 업비트 시장가 매수 API 호출 ──────────
                    order = await exchange.create_market_buy_order(symbol, trade_amount)
                    amount_coin = float(order.get("filled") or 0) or (
                        trade_amount / current_price
                    )
                    buy_price = float(order.get("average") or current_price)

                # ── BotSetting DB 삽입 (실거래·모의투자 공통) ─────────
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
                        is_paper_trading=is_paper_mode,   # ← 핵심 격리 플래그
                    )
                    db.add(setting)
                    await db.commit()
                    await db.refresh(setting)

                # ── TradingWorker 등록·시작 ────────────────────────────
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
    # Step 3: 통합 리포트 Embed 빌드
    # ------------------------------------------------------------------

    @staticmethod
    def _build_unified_report_embed(
        market_summary: str,
        reviewed_positions: list[dict],
        bought_positions: list[dict],
        is_paper_mode: bool = False,
    ) -> discord.Embed:
        """AI 시장 분석 요약 + 포지션 리뷰 + 신규 매수 내역을 하나의 Embed로 통합한다.

        Args:
            market_summary:     AI가 생성한 현재 시장 전반 분석 2~3문장.
            reviewed_positions: 리뷰 결과 딕셔너리 리스트.
            bought_positions:   신규 매수 딕셔너리 리스트.
            is_paper_mode:      True = 모의투자 리포트 / False = 실거래 리포트.

        Returns:
            통합 discord.Embed 객체.
        """
        total   = len(reviewed_positions) + len(bought_positions)
        is_idle = total == 0

        # 모의투자·실거래 구분 타이틀 및 컬러
        if is_paper_mode:
            title = "🎮 [AI 모의투자] 4시간 종합 리포트"
            idle_color   = discord.Color.greyple()
            active_color = discord.Color.purple()
        else:
            title = "🤖 [AI 펀드 매니저] 4시간 종합 리포트"
            idle_color   = discord.Color.light_grey()
            active_color = discord.Color.blue()

        color = idle_color if is_idle else active_color
        desc  = "전액 현금 관망 중" if is_idle else f"총 **{total}개** 종목을 처리했습니다."

        embed = discord.Embed(title=title, description=desc, color=color)

        # ── AI 시장 분석 요약 (항상 첫 번째 Field) ───────────────────
        embed.add_field(
            name="📊 AI 시장 분석 요약",
            value=market_summary or "분석 결과를 가져오지 못했습니다.",
            inline=False,
        )

        # ── 전액 현금 관망 안내 ──────────────────────────────────────
        if is_idle:
            embed.add_field(
                name="👀 현재 포지션",
                value="전액 현금 관망 중 (신규 진입 조건 미달)",
                inline=False,
            )

        # ── 신규 매수 섹션 ────────────────────────────────────────────
        if bought_positions:
            buy_label = "🟢 신규 가상 매수" if is_paper_mode else "🟢 신규 매수"
            embed.add_field(
                name=f"{buy_label} ({len(bought_positions)}건)",
                value="\u200b",
                inline=False,
            )
            for item in bought_positions:
                tp_str = f"+{item['target_profit_pct']:.1f}%"
                sl_str = f"-{item['stop_loss_pct']:.1f}%"
                value = (
                    f"**매수가:** {item['buy_price']:,.0f} KRW"
                    + (" _(슬리피지 0.1% 반영)_" if is_paper_mode else "") + "\n"
                    f"**수량:** {item['amount_coin']:.6f}\n"
                    f"**익절 목표:** {tp_str}  |  **손절 기준:** {sl_str}\n"
                    f"**AI 분석:** {item['reason']}"
                )
                tag = "[모의] " if is_paper_mode else ""
                embed.add_field(
                    name=f"🪙 {item['symbol']} [{tag}신규]",
                    value=value,
                    inline=False,
                )

        # ── 포지션 갱신 섹션 ──────────────────────────────────────────
        updated = [r for r in reviewed_positions if r["action"] == "UPDATE"]
        if updated:
            embed.add_field(
                name=f"🔄 목표 갱신 ({len(updated)}건)",
                value="\u200b",
                inline=False,
            )
            for item in updated:
                profit_emoji = "📈" if item["profit_pct"] >= 0 else "📉"
                value = (
                    f"**현재 수익률:** {item['profit_pct']:+.2f}%  {profit_emoji}\n"
                    f"**새 익절 목표:** +{item['new_target']:.1f}%  |  "
                    f"**새 손절 기준:** -{item['new_sl']:.1f}%\n"
                    f"**AI 판단:** {item['reason']}"
                )
                embed.add_field(
                    name=f"🪙 {item['symbol']} [갱신]",
                    value=value,
                    inline=False,
                )

        # ── 포지션 유지 섹션 ──────────────────────────────────────────
        maintained = [r for r in reviewed_positions if r["action"] == "MAINTAIN"]
        if maintained:
            lines = []
            for item in maintained:
                profit_emoji = "📈" if item["profit_pct"] >= 0 else "📉"
                lines.append(
                    f"• **{item['symbol']}** — "
                    f"{item['profit_pct']:+.2f}% {profit_emoji} — {item['reason']}"
                )
            embed.add_field(
                name=f"✅ 기존 목표 유지 ({len(maintained)}건)",
                value="\n".join(lines),
                inline=False,
            )

        next_time = get_next_ai_run_time()
        footer_prefix = "🎮 모의투자" if is_paper_mode else "실거래"
        embed.set_footer(
            text=f"{footer_prefix} | 익절·손절은 워커가 자동 처리 | 다음 리포트: {next_time}"
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
