"""
모의투자 슬래시 커맨드 Cog.

/모의투자 : 실제 업비트 API 키 없이 가상 잔고(1,000만 KRW)로 자동 매매 시뮬레이션 시작.
/AI통계   : 모의투자 누적 성과·승률·최근 거래 이력을 Embed 리포트로 표시.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import desc, select

from app.database import AsyncSessionLocal
from app.models.bot_setting import BotSetting
from app.models.trade_history import TradeHistory
from app.models.user import User
from app.services.trading_worker import TradingWorker, WorkerRegistry

# 코인 자동완성은 SettingsCog 와 공유 (추가 REST 호출 없이 인메모리 캐시 재사용)
from app.bot.cogs.settings import coin_autocomplete

logger = logging.getLogger(__name__)

# 모의투자 초기 가상 잔고 (표시 전용 — 실제 기본값은 User.virtual_krw 에 저장)
_INITIAL_VIRTUAL_KRW = 10_000_000.0
# 모의투자 최소 매수 금액 (KRW)
_PAPER_MIN_BUY_KRW = 6_000.0


class PaperTradingModal(discord.ui.Modal, title="🎮 모의투자 설정"):
    """모의투자 매매 파라미터를 입력받는 Modal.

    실거래 TradingSettingModal 과 동일한 필드 구성이지만,
    API 키 없이도 동작하며 BotSetting.is_paper_trading = True 로 저장된다.
    """

    buy_amount = discord.ui.TextInput(
        label="매수 금액 (KRW)",
        placeholder="예: 100000  (가상 잔고에서 차감됩니다)",
        min_length=1,
        max_length=15,
    )
    target_profit = discord.ui.TextInput(
        label="익절 목표 (%)",
        placeholder="예: 3.5  (비워두면 미설정)",
        required=False,
        max_length=6,
    )
    stop_loss = discord.ui.TextInput(
        label="손절 지점 (%)",
        placeholder="예: 2.0  (비워두면 미설정)",
        required=False,
        max_length=6,
    )

    def __init__(self, symbol: str, bot: commands.Bot) -> None:
        super().__init__()
        self.symbol = symbol
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """모달 제출 처리: 유효성 검사 → BotSetting 저장 → TradingWorker 시작."""
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)

        # ── 입력값 파싱 ───────────────────────────────────────────────
        try:
            buy_amount_krw = float(self.buy_amount.value.replace(",", ""))
            target_pct = (
                float(self.target_profit.value)
                if self.target_profit.value.strip()
                else None
            )
            stop_pct = (
                float(self.stop_loss.value)
                if self.stop_loss.value.strip()
                else None
            )
        except ValueError:
            await interaction.followup.send(
                "❌ 숫자 형식이 올바르지 않습니다.", ephemeral=True
            )
            return

        if buy_amount_krw < _PAPER_MIN_BUY_KRW:
            await interaction.followup.send(
                f"❌ 매수 금액은 최소 **{_PAPER_MIN_BUY_KRW:,.0f} KRW** 이상이어야 합니다.",
                ephemeral=True,
            )
            return

        async with AsyncSessionLocal() as db:
            # ── 유저 조회 / 자동 생성 ─────────────────────────────────
            result = await db.execute(select(User).where(User.user_id == user_id))
            user = result.scalar_one_or_none()
            if user is None:
                user = User(user_id=user_id)
                db.add(user)
                await db.flush()

            # ── 가상 잔고 확인 ────────────────────────────────────────
            if float(user.virtual_krw) < buy_amount_krw:
                await interaction.followup.send(
                    f"❌ 가상 잔고가 부족합니다.\n"
                    f"현재 잔고: **{float(user.virtual_krw):,.0f} KRW**\n"
                    f"필요 금액: **{buy_amount_krw:,.0f} KRW**",
                    ephemeral=True,
                )
                return

            # ── 동일 코인 모의투자 중복 실행 방지 ─────────────────────
            dupe_result = await db.execute(
                select(BotSetting).where(
                    BotSetting.user_id == user_id,
                    BotSetting.symbol == self.symbol,
                    BotSetting.is_running.is_(True),
                    BotSetting.is_paper_trading.is_(True),
                )
            )
            if dupe_result.scalar_one_or_none() is not None:
                await interaction.followup.send(
                    f"⚠️ `{self.symbol}` 모의투자가 이미 실행 중입니다.\n"
                    f"`/중지` 명령어로 먼저 중지한 후 다시 설정해 주세요.",
                    ephemeral=True,
                )
                return

            # ── BotSetting 저장 (is_paper_trading=True) ───────────────
            setting = BotSetting(
                user_id=user_id,
                symbol=self.symbol,
                buy_amount_krw=buy_amount_krw,
                target_profit_pct=target_pct,
                stop_loss_pct=stop_pct,
                is_running=True,
                is_paper_trading=True,
            )
            db.add(setting)
            await db.commit()
            await db.refresh(setting)

            # ── TradingWorker 시작 (exchange=None, is_paper_trading=True) ─
            # 실제 API 키 불필요. _buy()·_sell() 내 paper trading 분기에서
            # exchange 를 참조하지 않으므로 None 전달이 안전하다.
            worker = TradingWorker(
                setting_id=setting.id,
                user_id=user_id,
                symbol=self.symbol,
                buy_amount_krw=buy_amount_krw,
                target_profit_pct=target_pct,
                stop_loss_pct=stop_pct,
                exchange=None,          # 모의투자는 실거래 API 불필요
                notify_callback=self.bot._send_dm,
                is_paper_trading=True,
            )
            await WorkerRegistry.get().register(worker)
            worker.start()
            logger.info(
                "[모의투자] 워커 시작: user=%s symbol=%s amount=%.0f",
                user_id, self.symbol, buy_amount_krw,
            )

        # ── 결과 Embed ────────────────────────────────────────────────
        embed = discord.Embed(
            title=f"🎮 모의투자 시작! `{self.symbol}`",
            color=discord.Color.purple(),
        )
        embed.add_field(name="💰 투자 금액", value=f"**{buy_amount_krw:,.0f} KRW**", inline=True)
        tp_str = f"+{target_pct:.1f}%" if target_pct is not None else "미설정"
        sl_str = f"-{stop_pct:.1f}%" if stop_pct is not None else "미설정"
        embed.add_field(name="🎯 익절", value=tp_str, inline=True)
        embed.add_field(name="🛑 손절", value=sl_str, inline=True)
        embed.add_field(
            name="📌 안내",
            value=(
                "실제 업비트 API를 사용하지 않는 **가상 매매**입니다.\n"
                "슬리피지 0.1%가 반영된 가상 체결가로 시뮬레이션됩니다.\n"
                "매매 결과는 `/AI통계`에서 확인하세요."
            ),
            inline=False,
        )
        embed.set_footer(text=f"가상 잔고 차감 후 잔액 예정: {float(user.virtual_krw) - buy_amount_krw:,.0f} KRW")
        await interaction.followup.send(embed=embed, ephemeral=True)


class PaperTradingCog(commands.Cog):
    """모의투자 관련 슬래시 커맨드 Cog."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /모의투자
    # ------------------------------------------------------------------

    @app_commands.command(
        name="모의투자",
        description="실제 API 키 없이 가상 잔고(1,000만 KRW)로 자동 매매를 시뮬레이션합니다.",
    )
    @app_commands.describe(coin="매매할 코인을 검색하세요 (한글명·영문명·심볼 모두 지원)")
    @app_commands.autocomplete(coin=coin_autocomplete)
    async def paper_trading_command(
        self, interaction: discord.Interaction, coin: str
    ) -> None:
        """유저가 선택한 코인에 대해 PaperTradingModal 을 띄운다."""
        modal = PaperTradingModal(symbol=coin, bot=self.bot)
        await interaction.response.send_modal(modal)

    # ------------------------------------------------------------------
    # /AI통계
    # ------------------------------------------------------------------

    @app_commands.command(
        name="AI통계",
        description="모의투자 누적 성과, 승률, 최근 거래 이력을 확인합니다.",
    )
    async def ai_stats_command(self, interaction: discord.Interaction) -> None:
        """모의투자 성과 리포트 Embed를 전송한다.

        조회 대상: TradeHistory(is_paper_trading=True) + User.virtual_krw.
        """
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)

        async with AsyncSessionLocal() as db:
            # ── 유저 조회 ─────────────────────────────────────────────
            user_result = await db.execute(
                select(User).where(User.user_id == user_id)
            )
            user = user_result.scalar_one_or_none()

            if user is None:
                await interaction.followup.send(
                    "❌ 등록된 계정이 없습니다.\n"
                    "`/모의투자` 명령어로 시뮬레이션을 먼저 시작해 주세요.",
                    ephemeral=True,
                )
                return

            # ── 모의투자 거래 이력 조회 (최신순) ─────────────────────
            history_result = await db.execute(
                select(TradeHistory)
                .where(
                    TradeHistory.user_id == user_id,
                    TradeHistory.is_paper_trading.is_(True),
                )
                .order_by(desc(TradeHistory.created_at))
            )
            histories = history_result.scalars().all()

        # ── 통계 계산 ─────────────────────────────────────────────────
        total_trades = len(histories)
        wins = sum(1 for h in histories if h.profit_pct > 0)
        losses = total_trades - wins
        win_rate = wins / total_trades * 100 if total_trades > 0 else 0.0

        total_pnl_krw = sum(h.profit_krw for h in histories)
        total_invested = sum(h.buy_amount_krw for h in histories)
        cumulative_pct = (
            total_pnl_krw / total_invested * 100 if total_invested > 0 else 0.0
        )

        virtual_krw = float(user.virtual_krw)
        balance_change = virtual_krw - _INITIAL_VIRTUAL_KRW

        # ── Embed 구성 ────────────────────────────────────────────────
        pnl_color = discord.Color.green() if balance_change >= 0 else discord.Color.red()
        embed = discord.Embed(
            title="📊 AI 모의투자 성과 리포트",
            color=pnl_color,
        )

        # 1) 가상 잔고 현황
        balance_icon = "📈" if balance_change >= 0 else "📉"
        embed.add_field(
            name="💰 가상 잔고",
            value=(
                f"**{virtual_krw:,.0f} KRW**\n"
                f"{balance_icon} 초기 대비: **{balance_change:+,.0f} KRW**\n"
                f"_(초기 잔고: {_INITIAL_VIRTUAL_KRW:,.0f} KRW)_"
            ),
            inline=True,
        )

        # 2) 누적 성과 & 승률
        if total_trades > 0:
            stats_value = (
                f"총 거래: **{total_trades}회**\n"
                f"승/패: **{wins}승 {losses}패**\n"
                f"승률: **{win_rate:.1f}%**\n"
                f"누적 손익: **{total_pnl_krw:+,.0f} KRW** ({cumulative_pct:+.2f}%)"
            )
        else:
            stats_value = "아직 완료된 거래가 없습니다."

        embed.add_field(
            name="📈 누적 성과",
            value=stats_value,
            inline=True,
        )

        # 3) 최근 거래 기록 (최대 5건)
        if histories:
            lines = []
            for h in histories[:5]:
                icon = "🟢" if h.profit_pct > 0 else "🔴"
                created_str = h.created_at.strftime("%m/%d %H:%M") if h.created_at else "-"
                lines.append(
                    f"{icon} **{h.symbol}** `{created_str}`\n"
                    f"  {h.buy_price:,.0f} → {h.sell_price:,.0f} KRW | "
                    f"**{h.profit_pct:+.2f}%** ({h.profit_krw:+,.0f} KRW)"
                )
            embed.add_field(
                name="📋 최근 거래 기록 (최대 5건)",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="📋 거래 기록",
                value=(
                    "아직 완료된 모의투자 거래가 없습니다.\n"
                    "`/모의투자` 명령어로 시뮬레이션을 시작해 보세요!"
                ),
                inline=False,
            )

        embed.set_footer(text="💡 /모의투자로 AI 봇 성능을 무료로 체험하세요!")
        await interaction.followup.send(embed=embed, ephemeral=True)
