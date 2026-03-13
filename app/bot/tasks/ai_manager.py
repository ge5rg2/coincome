"""
AIFundManagerTask: 4시간마다 AI 종목 픽 → 자동 매수 → DM 리포트 스케줄러.

전체 데이터 흐름 (1 사이클):
  MarketDataManager._cache
      ↓  get_all()
  AITraderService.analyze_market(market_data, holding_symbols)
      ↓  picks (최대 2개)
  ExchangeService.create_market_buy_order(symbol, ai_trade_amount)
      ↓  order (체결 결과)
  BotSetting INSERT (buy_price·amount_coin·target_profit_pct·stop_loss_pct·is_running=True)
      ↓
  TradingWorker.start() → 포지션 복구 경로로 진입 → 매도 감시 루프
      ↓
  Discord DM Embed (매수 리포트)

Rate-Limit 방지:
  - 유저 간 asyncio.sleep(1)
  - 코인 간 asyncio.sleep(0.5)
"""
from __future__ import annotations

import asyncio
import logging

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

logger = logging.getLogger(__name__)


class AIFundManagerTask(commands.Cog):
    """4시간마다 AI 종목 분석·매수·DM 리포트를 수행하는 백그라운드 Cog.

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
    # 4시간 루프
    # ------------------------------------------------------------------

    @tasks.loop(hours=4)
    async def fund_loop(self) -> None:
        """4시간마다 AI 분석·매수를 실행하는 메인 루프.

        처리 흐름:
          1. MarketDataManager 캐시 존재 확인
          2. DB: VIP + ai_mode_enabled=True 유저 + bot_settings 일괄 조회
          3. 각 유저별 _process_user() 실행 (유저 간 1초 간격)
        """
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
    # 유저별 AI 매수 처리
    # ------------------------------------------------------------------

    async def _process_user(
        self, user: User, market_data: dict[str, dict]
    ) -> None:
        """단일 유저에 대해 AI 분석 → 매수 → DM 리포트를 실행한다.

        Args:
            user:        처리 대상 User 인스턴스 (bot_settings eagerly loaded).
            market_data: MarketDataManager.get_all() 결과.
        """
        user_id = user.user_id

        # API 키 없으면 스킵
        if not user.upbit_access_key or not user.upbit_secret_key:
            logger.warning("업비트 API 키 없음, 스킵: user_id=%s", user_id)
            return

        # 현재 감시 중인 코인 집합 (AI 픽 제외 대상)
        holding_symbols: set[str] = {
            s.symbol for s in user.bot_settings if s.is_running
        }

        # ── AI 분석 요청 ─────────────────────────────────────────────
        picks = await self.ai_service.analyze_market(market_data, holding_symbols)
        if not picks:
            logger.info("AI 픽 없음: user_id=%s", user_id)
            return

        exchange = ExchangeService(
            access_key=user.upbit_access_key,
            secret_key=user.upbit_secret_key,
        )
        ws_manager = UpbitWebsocketManager.get()
        registry   = WorkerRegistry.get()
        bought: list[dict] = []   # 성공한 매수 내역 (DM 리포트용)

        for pick in picks:
            symbol        = pick["symbol"]
            target_profit = pick["target_profit_pct"]
            stop_loss     = pick["stop_loss_pct"]

            try:
                # ── 현재가 조회 (WebSocket 캐시 우선) ─────────────────
                current_price = ws_manager.get_price(symbol)
                if current_price is None:
                    logger.warning(
                        "현재가 캐시 없음, 매수 스킵: user_id=%s symbol=%s",
                        user_id, symbol,
                    )
                    continue

                # ── 시장가 매수 실행 ──────────────────────────────────
                order = await exchange.create_market_buy_order(
                    symbol, float(user.ai_trade_amount)
                )

                # 체결 수량·단가 산출 (order 응답에 따라 폴백)
                amount_coin = float(order.get("filled") or 0) or (
                    user.ai_trade_amount / current_price
                )
                buy_price = float(order.get("average") or current_price)

                # ── BotSetting DB 삽입 ────────────────────────────────
                # buy_price·amount_coin을 함께 저장해 TradingWorker가
                # _decide_entry() 에서 '포지션 복구' 경로를 타도록 유도.
                # (신규 매수 없이 바로 매도 감시 루프로 진입)
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

                # ── TradingWorker 등록·시작 ───────────────────────────
                # notify_callback 으로 bot._send_dm 를 전달해
                # 익절/손절 체결 시 기존 방식대로 DM 알림.
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

                bought.append(
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

        # ── DM 리포트 전송 (매수 성공 건이 있는 경우만) ───────────────
        if bought:
            embed = self._build_report_embed(bought)
            await self._send_dm_embed(user_id, embed)

    # ------------------------------------------------------------------
    # 리포트 Embed 빌드
    # ------------------------------------------------------------------

    @staticmethod
    def _build_report_embed(bought: list[dict]) -> discord.Embed:
        """AI 매수 리포트 Embed를 생성한다.

        Args:
            bought: 성공한 매수 내역 딕셔너리 리스트.

        Returns:
            매수 코인·이유·목표를 담은 discord.Embed.
        """
        embed = discord.Embed(
            title="🤖 [AI 펀드 매니저] 매수 리포트",
            description=f"AI가 **{len(bought)}개** 종목을 자동 매수했습니다.",
            color=discord.Color.blue(),
        )

        for item in bought:
            tp_str = f"+{item['target_profit_pct']:.1f}%"
            sl_str = f"-{item['stop_loss_pct']:.1f}%"
            value = (
                f"**매수가:** {item['buy_price']:,.0f} KRW\n"
                f"**수량:** {item['amount_coin']:.6f}\n"
                f"**익절 목표:** {tp_str}  |  **손절 기준:** {sl_str}\n"
                f"**AI 분석:** {item['reason']}"
            )
            embed.add_field(name=f"🪙 {item['symbol']}", value=value, inline=False)

        embed.set_footer(
            text="이후 익절·손절은 기존 워커가 자동으로 처리합니다. | /잔고 로 현황 확인 가능"
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
