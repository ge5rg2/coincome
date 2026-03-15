"""
모의투자 슬래시 커맨드 Cog.

/모의투자 : AI 모의투자 ON/OFF 설정 모달. API 키 없이 가상 잔고로 AI가 자동 종목 선택·매수.
/ai통계   : 모의투자 가상 잔고, 누적 성과, 현재 진행 중인 포지션, 최근 거래 이력 Embed 리포트.

정책:
  - ai_paper_mode_enabled = True 이면 구독 등급 무관하게 AI 스케줄러 대상 포함.
  - 실거래 AI(ai_mode_enabled=True, VIP)와 완전히 격리: BotSetting.is_paper_trading 플래그로 구분.
  - 실거래 슬롯(is_paper_trading=False)과 모의투자 슬롯(is_paper_trading=True)은 각각 독립 카운트.
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
from app.services.websocket import UpbitWebsocketManager
from app.utils.time import get_next_ai_run_time

logger = logging.getLogger(__name__)

# 모의투자 초기 가상 잔고 (표시 전용 — 실제 기본값은 User.virtual_krw 에 저장)
_INITIAL_VIRTUAL_KRW = 10_000_000.0


# ------------------------------------------------------------------
# AI 모의투자 설정 Modal
# ------------------------------------------------------------------


class PaperAISettingModal(discord.ui.Modal, title="🎮 AI 모의투자 설정"):
    """AI 모의투자 ON/OFF 및 1회 가상 매수 금액을 입력받는 Modal.

    유저가 ON 으로 설정하면 다음 AI 스케줄러 실행 시부터
    virtual_krw 가상 잔고로 AI가 자동 종목을 선정·매수한다.

    Args:
        user: 현재 DB User 인스턴스 (기존 설정값 pre-fill 용).
    """

    def __init__(self, user: User) -> None:
        super().__init__()
        self._user_id = user.user_id

        self.mode = discord.ui.TextInput(
            label="AI 모의투자 모드 (ON / OFF)",
            placeholder="ON 또는 OFF 입력",
            min_length=2,
            max_length=3,
            default="ON" if user.ai_paper_mode_enabled else "OFF",
        )
        self.trade_amount = discord.ui.TextInput(
            label="1회 가상 매수 금액 (KRW)",
            placeholder="예: 100000  (가상 잔고에서 차감됩니다)",
            min_length=4,
            max_length=10,
            default=str(user.ai_trade_amount),
        )
        self.add_item(self.mode)
        self.add_item(self.trade_amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """모달 제출 처리: 입력 검증 → DB 업데이트 → 완료 Embed 반환."""
        await interaction.response.defer(ephemeral=True)

        # ── 입력값 검증 ───────────────────────────────────────────────
        mode_str = self.mode.value.strip().upper()
        if mode_str not in ("ON", "OFF"):
            await interaction.followup.send(
                "❌ 모드는 **ON** 또는 **OFF** 만 입력 가능합니다.", ephemeral=True
            )
            return

        try:
            amount = int(self.trade_amount.value.replace(",", "").strip())
        except ValueError:
            await interaction.followup.send(
                "❌ 매수 금액은 숫자로 입력해 주세요.", ephemeral=True
            )
            return

        if amount < 6_000:
            await interaction.followup.send(
                "❌ 매수 금액은 **최소 6,000 KRW** 이상이어야 합니다.\n"
                "(업비트 최소 주문 한도 5,000원 + 손절 하락분 고려)",
                ephemeral=True,
            )
            return

        enabled = mode_str == "ON"

        # ── DB 업데이트 ───────────────────────────────────────────────
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == self._user_id))
            user = result.scalar_one_or_none()
            if user is None:
                await interaction.followup.send(
                    "❌ 유저 정보를 찾을 수 없습니다.", ephemeral=True
                )
                return
            user.ai_paper_mode_enabled = enabled
            user.ai_trade_amount = amount
            virtual_krw = float(user.virtual_krw)
            await db.commit()

        logger.info(
            "AI 모의투자 설정 업데이트: user_id=%s enabled=%s amount=%d",
            self._user_id, enabled, amount,
        )

        # ── 완료 Embed 반환 ───────────────────────────────────────────
        status = "✅ 활성화" if enabled else "⏸️ 비활성화"
        embed = discord.Embed(
            title="🎮 AI 모의투자 설정 완료",
            color=discord.Color.purple() if enabled else discord.Color.greyple(),
        )
        embed.add_field(name="AI 모의투자", value=status, inline=True)
        embed.add_field(name="1회 매수 금액", value=f"{amount:,} KRW", inline=True)
        embed.add_field(name="💰 현재 가상 잔고", value=f"{virtual_krw:,.0f} KRW", inline=True)

        if enabled:
            next_time = get_next_ai_run_time()
            embed.add_field(
                name="📌 안내",
                value=(
                    "다음 AI 스케줄러 실행 시 **가상 잔고**로 종목을 선택하고 자동 매수합니다.\n"
                    "실제 업비트 API 키는 필요하지 않습니다.\n"
                    "매매 성과는 `/ai통계`에서 확인하세요."
                ),
                inline=False,
            )
            embed.set_footer(text=f"⏳ 다음 AI 분석 예정: {next_time} (이후 4시간 간격)")
        else:
            embed.add_field(
                name="📌 안내",
                value=(
                    "AI 모의투자가 중지되었습니다.\n"
                    "현재 진행 중인 모의 포지션은 계속 감시됩니다."
                ),
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)


# ------------------------------------------------------------------
# Cog
# ------------------------------------------------------------------


class PaperTradingCog(commands.Cog):
    """모의투자 관련 슬래시 커맨드 Cog."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /모의투자
    # ------------------------------------------------------------------

    @app_commands.command(
        name="모의투자",
        description="API 키 없이 AI가 가상 잔고로 자동 매매하는 모의투자 모드를 설정합니다.",
    )
    async def paper_trading_command(self, interaction: discord.Interaction) -> None:
        """유저 정보를 조회(없으면 자동 생성)한 뒤 PaperAISettingModal 을 띄운다."""
        user_id = str(interaction.user.id)

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == user_id))
            user = result.scalar_one_or_none()
            if user is None:
                # 첫 방문 유저 자동 생성 (가상 잔고 1,000만 KRW 기본값 포함)
                user = User(user_id=user_id)
                db.add(user)
                await db.commit()
                await db.refresh(user)

        modal = PaperAISettingModal(user=user)
        await interaction.response.send_modal(modal)

    # ------------------------------------------------------------------
    # /ai통계
    # ------------------------------------------------------------------

    @app_commands.command(
        name="ai통계",
        description="모의투자 가상 잔고, 누적 성과, 현재 진행 중인 포지션을 확인합니다.",
    )
    async def ai_stats_command(self, interaction: discord.Interaction) -> None:
        """모의투자 성과 리포트 Embed를 전송한다.

        조회 항목:
          1) User.virtual_krw                          — 현재 가상 잔고
          2) TradeHistory(is_paper_trading=True)        — 완료 거래 (누적 성과·승률)
          3) BotSetting(is_paper=True, is_running=True) — 미실현 보유 포지션 + 현재 수익률
        """
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)

        async with AsyncSessionLocal() as db:
            # ── 유저 조회 ─────────────────────────────────────────────
            user_result = await db.execute(select(User).where(User.user_id == user_id))
            user = user_result.scalar_one_or_none()

            if user is None:
                await interaction.followup.send(
                    "❌ 등록된 계정이 없습니다.\n"
                    "`/모의투자` 명령어로 설정을 먼저 완료해 주세요.",
                    ephemeral=True,
                )
                return

            # ── 완료된 모의투자 거래 이력 조회 (최신순) ───────────────
            history_result = await db.execute(
                select(TradeHistory)
                .where(
                    TradeHistory.user_id == user_id,
                    TradeHistory.is_paper_trading.is_(True),
                )
                .order_by(desc(TradeHistory.created_at))
            )
            histories = history_result.scalars().all()

            # ── 현재 진행 중인 모의투자 포지션 조회 ───────────────────
            open_result = await db.execute(
                select(BotSetting).where(
                    BotSetting.user_id == user_id,
                    BotSetting.is_running.is_(True),
                    BotSetting.is_paper_trading.is_(True),
                )
            )
            open_positions = open_result.scalars().all()

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
        stats_value = (
            f"총 거래: **{total_trades}회**\n"
            f"승/패: **{wins}승 {losses}패**\n"
            f"승률: **{win_rate:.1f}%**\n"
            f"누적 손익: **{total_pnl_krw:+,.0f} KRW** ({cumulative_pct:+.2f}%)"
        ) if total_trades > 0 else "아직 완료된 거래가 없습니다."

        embed.add_field(name="📈 완료 거래 성과", value=stats_value, inline=True)

        # 3) 현재 진행 중인 미실현 포지션
        if open_positions:
            ws_manager = UpbitWebsocketManager.get()
            lines: list[str] = []
            unrealized_pnl = 0.0

            for s in open_positions:
                current_price = ws_manager.get_price(s.symbol)
                if s.buy_price is not None and current_price is not None:
                    pct = (current_price - float(s.buy_price)) / float(s.buy_price) * 100
                    pnl = (current_price - float(s.buy_price)) * float(s.amount_coin or 0)
                    unrealized_pnl += pnl
                    icon = "🟢" if pct >= 0 else "🔴"
                    lines.append(
                        f"{icon} **{s.symbol}**\n"
                        f"  매수: {float(s.buy_price):,.0f} → 현재: {current_price:,.0f} KRW"
                        f" | **{pct:+.2f}%** ({pnl:+,.0f} KRW)"
                    )
                elif s.buy_price is None:
                    lines.append(f"⏳ **{s.symbol}** | 매수 대기 중...")
                else:
                    lines.append(f"❓ **{s.symbol}** | 시세 수신 대기 중...")

            unrealized_str = f"\n\n미실현 손익 합계: **{unrealized_pnl:+,.0f} KRW**"
            embed.add_field(
                name=f"👀 현재 진행 중인 모의투자 ({len(open_positions)}건)",
                value="\n".join(lines) + unrealized_str,
                inline=False,
            )
        else:
            embed.add_field(
                name="👀 현재 진행 중인 모의투자",
                value=(
                    "현재 보유 중인 모의 포지션이 없습니다.\n"
                    "AI 스케줄러가 다음 실행 시 종목을 선정합니다."
                ),
                inline=False,
            )

        # 4) 최근 완료 거래 기록 (최대 5건)
        if histories:
            rec_lines: list[str] = []
            for h in histories[:5]:
                icon = "🟢" if h.profit_pct > 0 else "🔴"
                created_str = h.created_at.strftime("%m/%d %H:%M") if h.created_at else "-"
                rec_lines.append(
                    f"{icon} **{h.symbol}** `{created_str}`\n"
                    f"  {h.buy_price:,.0f} → {h.sell_price:,.0f} KRW"
                    f" | **{h.profit_pct:+.2f}%** ({h.profit_krw:+,.0f} KRW)"
                )
            embed.add_field(
                name="📋 최근 거래 기록 (최대 5건)",
                value="\n".join(rec_lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="📋 최근 거래 기록",
                value=(
                    "아직 완료된 거래가 없습니다.\n"
                    "`/모의투자`를 ON 으로 설정하고 AI 스케줄러를 기다리세요!"
                ),
                inline=False,
            )

        embed.set_footer(text="💡 AI 모의투자로 실거래 전 봇 성능을 무료로 검증하세요!")
        await interaction.followup.send(embed=embed, ephemeral=True)
