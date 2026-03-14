"""
AIFundManagerTask: 업비트 4시간 봉 완성 정각(KST)에 동기화되어 실행되는 AI 펀드 매니저.

트리거 시각 (KST, 업비트 4h 봉 마감 정각):
  01:00 / 05:00 / 09:00 / 13:00 / 17:00 / 21:00

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
      current_count = is_running 포지션 수
      slots = ai_max_coins - current_count
      AITraderService.analyze_market(market_data, holding_symbols)
          ↓  picks (최대 slots 개)
      ExchangeService.create_market_buy_order(symbol, ai_trade_amount)
          ↓  order
      BotSetting INSERT + TradingWorker.start()
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
from app.utils.time import get_next_ai_run_time
from app.models.user import SubscriptionTier, User
from app.services.ai_trader import AITraderService
from app.services.exchange import ExchangeService
from app.services.market_data import MarketDataManager
from app.services.trading_worker import TradingWorker, WorkerRegistry
from app.services.websocket import UpbitWebsocketManager

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 업비트 4시간 봉 마감 정각 (한국 표준시 KST = UTC+9)
# 업비트 4h 봉은 01 / 05 / 09 / 13 / 17 / 21시에 새 봉이 열린다.
# discord.py tasks.loop(time=...) 는 timezone-aware datetime.time 을 지원하므로
# ZoneInfo("Asia/Seoul") 로 KST 기준 시각을 직접 지정한다.
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
          2. DB: VIP + ai_mode_enabled=True 유저 + bot_settings 일괄 조회
          3. 각 유저별 _process_user() 실행 (유저 간 1초 간격)
        """
        # 업비트 서버가 새 4시간 봉 데이터를 확정적으로 반영하기까지
        # 수 초의 롤오버 지연이 발생할 수 있다.
        # 10초 대기로 불완전한 캔들 데이터를 읽는 경쟁 조건을 방어한다.
        await asyncio.sleep(10)

        logger.info("AI 펀드 매니저 루프 실행")

        # 1. 마켓 데이터 캐시 확인 (MarketDataManager 초기화 전이면 스킵)
        market_data = MarketDataManager.get().get_all()
        if not market_data:
            logger.warning("AI 펀드 매니저: 마켓 데이터 캐시 없음 — 스킵 (다음 주기 대기)")
            return

        # 2. VIP + AI 모드 활성 유저 조회
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User)
                .where(
                    User.subscription_tier == SubscriptionTier.VIP,
                    User.ai_mode_enabled.is_(True),
                    User.is_active.is_(True),
                )
                .options(selectinload(User.bot_settings))
            )
            users: list[User] = result.scalars().all()

        if not users:
            logger.info("AI 펀드 매니저: 대상 유저 없음")
            return

        logger.info("AI 펀드 매니저 대상 유저: %d 명", len(users))

        # 3. 유저별 처리 (유저 간 1초 Rate-Limit 방지)
        for user in users:
            try:
                await self._process_user(user, market_data)
            except Exception as exc:
                logger.error(
                    "AI 펀드 매니저 처리 오류: user_id=%s err=%s", user.user_id, exc
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
        self, user: User, market_data: dict[str, dict]
    ) -> None:
        """단일 유저에 대해 포지션 리뷰 → 신규 매수 → DM 통합 리포트를 실행한다.

        Args:
            user:        처리 대상 User 인스턴스 (bot_settings eagerly loaded).
            market_data: MarketDataManager.get_all() 결과.
        """
        user_id = user.user_id

        # API 키 없으면 스킵
        if not user.upbit_access_key or not user.upbit_secret_key:
            logger.warning("업비트 API 키 없음, 스킵: user_id=%s", user_id)
            return

        exchange  = ExchangeService(
            access_key=user.upbit_access_key,
            secret_key=user.upbit_secret_key,
        )
        ws_manager = UpbitWebsocketManager.get()
        registry   = WorkerRegistry.get()

        # 현재 감시 중인 running 포지션 목록
        running_settings = [s for s in user.bot_settings if s.is_running]
        ai_max_coins     = user.ai_max_coins  # 최대 동시 보유 종목 수

        # DM 통합 리포트용 수집 버킷
        reviewed_positions: list[dict] = []   # MAINTAIN / UPDATE 처리 결과
        bought_positions: list[dict]   = []   # 신규 매수 결과

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
        # analyze_market은 항상 {"market_summary": str, "picks": list} 를 반환한다.
        # market_summary는 관망 이유 포함 — 슬롯이 없어도 사용자에게 전달한다.
        current_count   = len(running_settings)
        slots           = ai_max_coins - current_count
        holding_symbols: set[str] = {s.symbol for s in running_settings}

        analysis       = await self.ai_service.analyze_market(market_data, holding_symbols)
        market_summary = analysis.get("market_summary", "")
        picks          = analysis.get("picks", [])

        if slots > 0 and picks:
            await self._buy_new_coins(
                user=user,
                picks=picks[:slots],     # 슬롯 수만큼만 전달 (이미 필터링 완료)
                exchange=exchange,
                ws_manager=ws_manager,
                registry=registry,
                bought_positions=bought_positions,
            )
        elif slots <= 0:
            logger.info(
                "AI 펀드 매니저: 슬롯 없음 (보유=%d / 최대=%d) — 신규 매수 스킵: user_id=%s",
                current_count, ai_max_coins, user_id,
            )
        else:
            logger.info("AI 신규 픽 없음 (관망): user_id=%s", user_id)

        # ── Step 3: 단일 통합 DM Embed 무조건 전송 ───────────────────
        # 매수·리뷰 액션이 없어도 AI 시장 분석 요약과 함께 반드시 DM을 발송한다.
        # 유저가 봇의 동작 여부(고장 vs 관망)를 항상 인지할 수 있어야 한다.
        embed = self._build_unified_report_embed(
            market_summary=market_summary,
            reviewed_positions=reviewed_positions,
            bought_positions=bought_positions,
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

        Args:
            user_id:           Discord 사용자 ID.
            running_settings:  is_running=True 인 BotSetting 목록.
            market_data:       MarketDataManager.get_all() 결과.
            ws_manager:        UpbitWebsocketManager 인스턴스.
            registry:          WorkerRegistry 인스턴스.
            reviewed_positions: 결과를 축적할 리스트 (DM 리포트용).
        """
        # ── 포지션 데이터 구성 ─────────────────────────────────────────
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

        # ── AI 리뷰 요청 ──────────────────────────────────────────────
        reviews = await self.ai_service.review_positions(positions_data, market_data)
        if not reviews:
            logger.info("AI 포지션 리뷰 결과 없음: user_id=%s", user_id)
            return

        # ── 리뷰 결과 적용 ────────────────────────────────────────────
        for review in reviews:
            symbol     = review["symbol"]
            action     = review["action"]
            new_tgt    = review["new_target_profit_pct"]
            new_sl     = review["new_stop_loss_pct"]
            reason     = review["reason"]

            pos = next((p for p in positions_data if p["symbol"] == symbol), None)
            if pos is None:
                continue

            setting_id = pos["setting_id"]

            if action == "UPDATE":
                # DB 갱신
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(BotSetting).where(BotSetting.id == setting_id)
                    )
                    setting = result.scalar_one_or_none()
                    if setting:
                        setting.target_profit_pct = new_tgt
                        setting.stop_loss_pct      = new_sl
                        await db.commit()

                # 워커 인메모리 동기화
                worker = registry.get_worker(setting_id)
                if worker:
                    worker.target_profit_pct = new_tgt
                    worker.stop_loss_pct      = new_sl
                    if worker._position:
                        worker._position.target_profit_pct = new_tgt
                        worker._position.stop_loss_pct      = new_sl

                logger.info(
                    "AI 포지션 UPDATE: user_id=%s symbol=%s tgt=%.1f%% sl=%.1f%%",
                    user_id, symbol, new_tgt, new_sl,
                )

            reviewed_positions.append(
                {
                    "symbol":        symbol,
                    "action":        action,
                    "profit_pct":    pos["profit_pct"],
                    "new_target":    new_tgt,
                    "new_sl":        new_sl,
                    "reason":        reason,
                }
            )

    # ------------------------------------------------------------------
    # Step 2: 신규 종목 매수
    # ------------------------------------------------------------------

    async def _buy_new_coins(
        self,
        user: User,
        picks: list[dict],
        exchange: ExchangeService,
        ws_manager: UpbitWebsocketManager,
        registry: WorkerRegistry,
        bought_positions: list[dict],
    ) -> None:
        """AI가 선정한 신규 종목을 시장가 매수하고 워커를 등록한다.

        Args:
            user:             처리 대상 User 인스턴스.
            picks:            _process_user에서 이미 슬롯 수만큼 슬라이싱된 픽 리스트.
                              (analyze_market 호출은 _process_user 에서 수행)
            exchange:         ExchangeService 인스턴스.
            ws_manager:       UpbitWebsocketManager 인스턴스.
            registry:         WorkerRegistry 인스턴스.
            bought_positions: 결과를 축적할 리스트 (DM 리포트용).
        """
        user_id = user.user_id

        for pick in picks:
            symbol        = pick["symbol"]
            target_profit = pick["target_profit_pct"]
            stop_loss     = pick["stop_loss_pct"]

            try:
                # 현재가 조회 (WebSocket 캐시 우선)
                current_price = ws_manager.get_price(symbol)
                if current_price is None:
                    logger.warning(
                        "현재가 캐시 없음, 매수 스킵: user_id=%s symbol=%s",
                        user_id, symbol,
                    )
                    continue

                # 시장가 매수 실행
                order = await exchange.create_market_buy_order(
                    symbol, float(user.ai_trade_amount)
                )

                # 체결 수량·단가 산출 (order 응답에 따라 폴백)
                amount_coin = float(order.get("filled") or 0) or (
                    user.ai_trade_amount / current_price
                )
                buy_price = float(order.get("average") or current_price)

                # BotSetting DB 삽입
                # buy_price·amount_coin을 함께 저장해 TradingWorker가
                # _decide_entry() 에서 '포지션 복구' 경로를 타도록 유도.
                async with AsyncSessionLocal() as db:
                    setting = BotSetting(
                        user_id=user_id,
                        symbol=symbol,
                        buy_amount_krw=float(user.ai_trade_amount),
                        target_profit_pct=target_profit,
                        stop_loss_pct=stop_loss,
                        is_running=True,
                        buy_price=buy_price,
                        amount_coin=amount_coin,
                    )
                    db.add(setting)
                    await db.commit()
                    await db.refresh(setting)

                # TradingWorker 등록·시작
                worker = TradingWorker(
                    setting_id=setting.id,
                    user_id=user_id,
                    symbol=symbol,
                    buy_amount_krw=float(user.ai_trade_amount),
                    target_profit_pct=target_profit,
                    stop_loss_pct=stop_loss,
                    exchange=exchange,
                    notify_callback=self.bot._send_dm,
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
                    "AI 매수 완료: user_id=%s symbol=%s price=%.0f amount=%.6f",
                    user_id, symbol, buy_price, amount_coin,
                )

            except Exception as exc:
                logger.error(
                    "AI 매수 실패: user_id=%s symbol=%s err=%s", user_id, symbol, exc
                )

            # 코인 간 Rate-Limit 방지
            await asyncio.sleep(0.5)

    # ------------------------------------------------------------------
    # Step 3: 통합 리포트 Embed 빌드
    # ------------------------------------------------------------------

    @staticmethod
    def _build_unified_report_embed(
        market_summary: str,
        reviewed_positions: list[dict],
        bought_positions: list[dict],
    ) -> discord.Embed:
        """AI 시장 분석 요약 + 포지션 리뷰 + 신규 매수 내역을 하나의 Embed로 통합한다.

        매수·리뷰 액션이 전혀 없는 '전액 현금 관망' 상태여도 반드시 호출되며,
        이 경우 market_summary(관망 이유)와 관망 안내 문구만 표시된다.

        Args:
            market_summary:     AI가 생성한 현재 시장 전반 분석 2~3문장.
            reviewed_positions: 리뷰 결과 딕셔너리 리스트.
            bought_positions:   신규 매수 딕셔너리 리스트.

        Returns:
            통합 discord.Embed 객체.
        """
        total    = len(reviewed_positions) + len(bought_positions)
        is_idle  = total == 0   # 매수·갱신·유지 액션 없음 = 전액 현금 관망

        color    = discord.Color.light_grey() if is_idle else discord.Color.blue()
        desc     = "전액 현금 관망 중" if is_idle else f"총 **{total}개** 종목을 처리했습니다."
        embed = discord.Embed(
            title="🤖 [AI 펀드 매니저] 4시간 종합 리포트",
            description=desc,
            color=color,
        )

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
            embed.add_field(
                name=f"🟢 신규 매수 ({len(bought_positions)}건)",
                value="\u200b",   # 섹션 헤더 (빈 값 대체)
                inline=False,
            )
            for item in bought_positions:
                tp_str = f"+{item['target_profit_pct']:.1f}%"
                sl_str = f"-{item['stop_loss_pct']:.1f}%"
                value = (
                    f"**매수가:** {item['buy_price']:,.0f} KRW\n"
                    f"**수량:** {item['amount_coin']:.6f}\n"
                    f"**익절 목표:** {tp_str}  |  **손절 기준:** {sl_str}\n"
                    f"**AI 분석:** {item['reason']}"
                )
                embed.add_field(
                    name=f"🪙 {item['symbol']} [신규]",
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
        embed.set_footer(
            text=f"익절·손절은 워커가 자동 처리 | /잔고 로 현황 확인 가능 | 다음 리포트: {next_time}"
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
