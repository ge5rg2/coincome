"""Discord 봇 엔트리 포인트"""
from __future__ import annotations

import asyncio
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
        """사용자에게 DM 전송. HTTPException 발생 시 최대 3회 재시도 (간격 3 초).

        Args:
            user_id: Discord 사용자 ID (문자열)
            message: 전송할 메시지 내용
        """
        for attempt in range(1, 4):
            try:
                user = await self.fetch_user(int(user_id))
                await user.send(message)
                return  # 전송 성공 시 즉시 반환
            except discord.Forbidden:
                # 403: 사용자가 DM을 차단한 경우 — 재시도해도 의미 없음
                logger.warning("DM 전송 거부됨 (DM 차단): user_id=%s", user_id)
                return
            except discord.HTTPException as exc:
                if attempt < 3:
                    logger.warning(
                        "DM 전송 실패 (시도 %d/3, HTTP %s): user_id=%s — 3초 후 재시도",
                        attempt, exc.status, user_id,
                    )
                    await asyncio.sleep(3)
                else:
                    logger.error(
                        "DM 전송 최종 실패 (3회 모두 실패, HTTP %s): user_id=%s",
                        exc.status, user_id,
                    )
            except Exception as exc:
                # fetch_user 실패 등 비-HTTP 오류는 재시도 없이 즉시 종료
                logger.error("DM 전송 오류 (재시도 불가): user_id=%s err=%s", user_id, exc)
                return


bot = CoinComeBot()
