"""/구독 슬래시 커맨드 - 현재 구독 등급 확인 및 결제 링크 안내"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.user import SubscriptionTier, User

logger = logging.getLogger(__name__)

TIER_PRICES = {
    SubscriptionTier.PRO: 9_900,
    SubscriptionTier.VIP: 29_900,
}

TIER_BENEFITS = {
    SubscriptionTier.FREE: "• 코인 2개 동시 운영\n• 최대 투자금 10만 원\n• 기본 알림",
    SubscriptionTier.PRO: "• 코인 무제한 동시 운영\n• 투자금 무제한\n• 트레일링 스탑 기능\n• 상세 수익 통계",
    SubscriptionTier.VIP: "• PRO 모든 기능\n• 우선 지원 채널\n• 전략 커스텀 설정",
}


class SubscriptionCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="구독", description="현재 구독 등급을 확인하고 결제 링크를 받습니다.")
    async def subscription_command(self, interaction: discord.Interaction) -> None:
        user_id = str(interaction.user.id)

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == user_id))
            user = result.scalar_one_or_none()

        tier = user.subscription_tier if user else SubscriptionTier.FREE
        expires_at = user.sub_expires_at if user else None

        embed = discord.Embed(
            title="💎 구독 정보",
            color=self._tier_color(tier),
        )
        embed.add_field(name="현재 등급", value=f"**{tier}**", inline=True)

        if expires_at:
            remaining = (expires_at.replace(tzinfo=timezone.utc) - datetime.now(tz=timezone.utc)).days
            embed.add_field(
                name="만료일",
                value=f"{expires_at.strftime('%Y-%m-%d')} (D-{remaining})",
                inline=True,
            )

        embed.add_field(
            name="현재 등급 혜택",
            value=TIER_BENEFITS.get(tier, ""),
            inline=False,
        )

        # 업그레이드 버튼 뷰
        view = UpgradeView(user_id=user_id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @staticmethod
    def _tier_color(tier: str) -> discord.Color:
        return {
            SubscriptionTier.FREE: discord.Color.greyple(),
            SubscriptionTier.PRO: discord.Color.blue(),
            SubscriptionTier.VIP: discord.Color.gold(),
        }.get(tier, discord.Color.greyple())


class UpgradeView(discord.ui.View):
    def __init__(self, user_id: str) -> None:
        super().__init__(timeout=120)
        base = settings.dashboard_base_url

        self.add_item(discord.ui.Button(
            label="PRO 구독 (₩9,900/월)",
            style=discord.ButtonStyle.primary,
            url=f"{base}/payment?tier=PRO&user_id={user_id}",
            emoji="🚀",
        ))
        self.add_item(discord.ui.Button(
            label="VIP 구독 (₩29,900/월)",
            style=discord.ButtonStyle.success,
            url=f"{base}/payment?tier=VIP&user_id={user_id}",
            emoji="👑",
        ))
