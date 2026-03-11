"""Discord 봇 엔트리 포인트"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from app.config import settings
from app.services.subscription import expiry_reminder_loop
from app.services.trading_worker import WorkerRegistry

logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True


class CoinComeBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        from app.bot.cogs.settings import SettingsCog
        from app.bot.cogs.subscription import SubscriptionCog

        await self.add_cog(SettingsCog(self))
        await self.add_cog(SubscriptionCog(self))

        if settings.discord_guild_id:
            guild = discord.Object(id=settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("슬래시 커맨드 동기화 완료 (개발 서버)")
        else:
            await self.tree.sync()
            logger.info("슬래시 커맨드 글로벌 동기화 완료")

        self.loop.create_task(expiry_reminder_loop(self._send_dm))

    async def on_ready(self) -> None:
        logger.info("봇 준비 완료: %s (ID: %s)", self.user, self.user.id)
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name="코인 시세 👀"
        ))

    async def _send_dm(self, user_id: str, message: str) -> None:
        """사용자에게 DM 전송"""
        try:
            user = await self.fetch_user(int(user_id))
            await user.send(message)
        except Exception as exc:
            logger.warning("DM 전송 실패: user_id=%s err=%s", user_id, exc)


bot = CoinComeBot()
