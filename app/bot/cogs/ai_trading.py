"""
/ai설정 슬래시 커맨드: VIP 전용 AI 자동 매매 펀드 매니저 기능 설정.

처리 흐름:
  1. VIP 등급 검증 → 미달 시 업그레이드 유도 Embed 반환
  2. VIP 확인 → AISettingModal 표시 (현재 설정값 pre-fill)
  3. 유저 입력(ON/OFF · 매수금액) → DB 업데이트 → 완료 Embed 반환
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.user import SubscriptionTier, User
from app.utils.time import get_next_ai_run_time

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# VIP 안내 Embed
# ------------------------------------------------------------------

def _make_vip_required_embed() -> discord.Embed:
    """AI 자동 매매 기능이 VIP 전용임을 안내하는 Embed를 반환한다."""
    embed = discord.Embed(
        title="👑 AI 자동 매매는 VIP 전용 기능입니다!",
        description=(
            "AI 펀드 매니저는 **VIP 등급 전용** 기능으로,\n"
            "AI가 시장 데이터를 분석해 종목 선택부터 매수까지 **완전 자동**으로 수행합니다."
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="🤖 VIP AI 펀드 매니저 기능",
        value=(
            "• 4시간마다 전체 KRW 마켓 자동 스크리닝\n"
            "• GPT-4o-mini 기반 RSI·MA 지표 분석\n"
            "• 최대 2개 종목 자동 매수 및 워커 자동 등록\n"
            "• 매수 후 DM 리포트 자동 발송"
        ),
        inline=False,
    )
    embed.add_field(
        name="💎 VIP 전체 혜택",
        value="• 코인 무제한 동시 운영\n• 우선 지원 채널\n• 전략 커스텀 설정",
        inline=False,
    )
    embed.set_footer(text="/구독 명령어로 VIP로 업그레이드하세요.")
    return embed


# ------------------------------------------------------------------
# AI 설정 Modal
# ------------------------------------------------------------------

class AISettingModal(discord.ui.Modal, title="AI 자동 매매 설정"):
    """AI 모드 ON/OFF, 1회 매수 금액, 최대 보유 종목 수를 입력받는 Modal.

    현재 DB 값을 default 로 pre-fill 해 유저가 기존 설정을 바로 확인·수정할 수 있다.
    """

    def __init__(self, user: User) -> None:
        super().__init__()
        self._user_id = user.user_id

        self.mode = discord.ui.TextInput(
            label="AI 모드 (ON / OFF)",
            placeholder="ON 또는 OFF 입력",
            min_length=2,
            max_length=3,
            default="ON" if user.ai_mode_enabled else "OFF",
        )
        self.trade_amount = discord.ui.TextInput(
            label="1회 매수 금액 (KRW)",
            placeholder="예: 10000  (최소 6,000)",
            min_length=4,
            max_length=10,
            default=str(user.ai_trade_amount),
        )
        self.max_coins = discord.ui.TextInput(
            label="최대 동시 보유 종목 수",
            placeholder="예: 3  (1 ~ 10)",
            min_length=1,
            max_length=2,
            default=str(user.ai_max_coins),
        )
        self.add_item(self.mode)
        self.add_item(self.trade_amount)
        self.add_item(self.max_coins)

    async def on_submit(self, interaction: discord.Interaction) -> None:
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

        if amount < 6000:
            await interaction.followup.send(
                "❌ 매수 금액은 **최소 6,000 KRW** 이상이어야 합니다.\n"
                "(업비트 최소 주문 한도 5,000원 + 손절 하락분 고려)",
                ephemeral=True,
            )
            return

        try:
            max_coins = int(self.max_coins.value.strip())
        except ValueError:
            await interaction.followup.send(
                "❌ 최대 보유 종목 수는 숫자로 입력해 주세요.", ephemeral=True
            )
            return

        if not 1 <= max_coins <= 10:
            await interaction.followup.send(
                "❌ 최대 보유 종목 수는 **1 ~ 10** 사이로 입력해 주세요.", ephemeral=True
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
            user.ai_mode_enabled = enabled
            user.ai_trade_amount = amount
            user.ai_max_coins = max_coins
            await db.commit()

        logger.info(
            "AI 설정 업데이트: user_id=%s enabled=%s amount=%d max_coins=%d",
            self._user_id, enabled, amount, max_coins,
        )

        # ── 완료 Embed 반환 ───────────────────────────────────────────
        status = "✅ 활성화" if enabled else "⏸️ 비활성화"
        embed = discord.Embed(
            title="🤖 AI 자동 매매 설정 완료",
            color=discord.Color.green() if enabled else discord.Color.greyple(),
        )
        embed.add_field(name="AI 모드", value=status, inline=True)
        embed.add_field(name="1회 매수 금액", value=f"{amount:,} KRW", inline=True)
        embed.add_field(name="최대 보유 종목", value=f"{max_coins}개", inline=True)

        if enabled:
            next_time = get_next_ai_run_time()
            embed.set_footer(
                text=f"⏳ 다음 AI 분석 예정: {next_time} (이후 4시간 간격)"
            )
        else:
            embed.set_footer(text="AI 자동 매매가 중지되었습니다. 기존 워커는 계속 동작합니다.")

        await interaction.followup.send(embed=embed, ephemeral=True)


# ------------------------------------------------------------------
# Cog
# ------------------------------------------------------------------

class AITradingCog(commands.Cog):
    """AI 자동 매매 관련 슬래시 커맨드 Cog."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="ai설정",
        description="AI 자동 매매 펀드 매니저를 설정합니다 (VIP 전용).",
    )
    async def ai_settings_command(self, interaction: discord.Interaction) -> None:
        """VIP 여부를 확인한 뒤 AI 설정 Modal을 띄운다.

        [VIP 검증] FREE / PRO 등급이면 업그레이드 유도 Embed로 즉시 반환.
        [설정 UI ] VIP 확인 시 현재 설정값이 pre-fill 된 AISettingModal 표시.
        """
        user_id = str(interaction.user.id)

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == user_id))
            user = result.scalar_one_or_none()

        # VIP 등급 검증 (미등록 유저 포함)
        if user is None or user.subscription_tier != SubscriptionTier.VIP:
            await interaction.response.send_message(
                embed=_make_vip_required_embed(), ephemeral=True
            )
            return

        # VIP 확인 → 설정 Modal 표시
        modal = AISettingModal(user=user)
        await interaction.response.send_modal(modal)
