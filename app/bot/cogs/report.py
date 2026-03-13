"""
정기 보고 Cog: 자동 매매 실행 중인 사용자에게 1시간마다 수익률 현황을 DM으로 전송.

설계 참고:
  현재는 1시간 고정 주기로 전체 사용자에게 일괄 전송합니다.
  추후 User 모델에 아래 컬럼을 추가하면 개인화된 보고 기능을 지원할 수 있습니다.
    - report_enabled: bool        — 정기 보고 수신 여부 (기본값 True)
    - report_interval_hours: int  — 보고 주기 (기본값 1, 허용값: 1/3/6/12/24)
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

import discord
from discord.ext import commands, tasks
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.bot_setting import BotSetting
from app.services.websocket import UpbitWebsocketManager

logger = logging.getLogger(__name__)


class ReportCog(commands.Cog):
    """매 시간 자동 매매 실행 중인 사용자에게 수익률 요약 DM을 전송하는 Cog.

    Attributes:
        bot: Discord 봇 인스턴스.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.hourly_report.start()

    def cog_unload(self) -> None:
        """Cog 언로드 시 백그라운드 루프를 정상 종료한다."""
        self.hourly_report.cancel()

    # ------------------------------------------------------------------
    # 백그라운드 루프
    # ------------------------------------------------------------------

    @tasks.loop(hours=1)
    async def hourly_report(self) -> None:
        """실행 중인 BotSetting을 사용자별로 묶어 수익률 요약 DM을 전송한다.

        처리 흐름:
          1. DB에서 is_running=True 인 BotSetting 전체 조회
          2. user_id 기준으로 그룹핑
          3. 사용자별 Embed 빌드 후 DM 전송
          4. 사용자 간 1초 지연으로 Discord Rate-Limit 방지
        """
        logger.info("정기 보고 루프 실행")

        # 1. 실행 중인 모든 BotSetting 조회
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(BotSetting).where(BotSetting.is_running.is_(True))
            )
            all_settings: list[BotSetting] = result.scalars().all()

        if not all_settings:
            logger.info("정기 보고: 실행 중인 봇 없음 — 스킵")
            return

        # 2. user_id 별 그룹핑
        user_settings: dict[str, list[BotSetting]] = defaultdict(list)
        for s in all_settings:
            user_settings[s.user_id].append(s)

        ws_manager = UpbitWebsocketManager.get()
        sent = 0

        for user_id, settings in user_settings.items():
            try:
                embed = self._build_report_embed(settings, ws_manager)
                await self._send_dm_embed(user_id, embed)
                sent += 1
            except Exception as exc:
                logger.error(
                    "정기 보고 처리 오류: user_id=%s err=%s", user_id, exc
                )

            # 사용자 간 1초 간격으로 Rate-Limit 방지
            await asyncio.sleep(1)

        logger.info("정기 보고 루프 완료 — 전송 %d 명 / 전체 %d 명", sent, len(user_settings))

    @hourly_report.before_loop
    async def before_hourly_report(self) -> None:
        """봇이 완전히 준비될 때까지 루프 첫 실행을 지연한다."""
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Embed 빌드
    # ------------------------------------------------------------------

    @staticmethod
    def _build_report_embed(
        settings: list[BotSetting],
        ws_manager: UpbitWebsocketManager,
    ) -> discord.Embed:
        """사용자의 전체 코인 포지션을 요약한 Embed를 생성한다.

        Args:
            settings: 해당 사용자의 실행 중인 BotSetting 목록.
            ws_manager: 현재가 조회를 위한 WebSocket 매니저 인스턴스.

        Returns:
            수익률 및 포지션 정보를 담은 discord.Embed.
        """
        embed = discord.Embed(
            title="⏰ 정기 포트폴리오 보고",
            description="현재 자동 매매 중인 코인의 수익률 현황입니다.",
            color=discord.Color.blurple(),
        )

        total_pnl = 0.0
        has_position = False

        for s in settings:
            current_price = ws_manager.get_price(s.symbol)

            if s.buy_price and s.amount_coin and current_price:
                # 포지션 보유 중 — 실시간 수익률 계산
                profit_pct = (current_price - s.buy_price) / s.buy_price * 100
                pnl = (current_price - s.buy_price) * s.amount_coin
                total_pnl += pnl
                has_position = True
                status_icon = "🟢" if profit_pct >= 0 else "🔴"
                value = (
                    f"**매수가:** {s.buy_price:,.0f} KRW\n"
                    f"**현재가:** {current_price:,.0f} KRW\n"
                    f"**수익률:** {status_icon} **{profit_pct:+.2f}%** ({pnl:+,.0f} KRW)\n"
                    f"**수량:** {s.amount_coin:.6f}"
                )
            elif current_price:
                # 매수 대기 중 — 현재가만 표시
                value = f"⏳ 매수 대기 중\n**현재가:** {current_price:,.0f} KRW"
            else:
                # 시세 미수신 (WebSocket 초기화 중)
                value = "⏳ 매수 대기 중 또는 시세 로딩 중..."

            embed.add_field(name=f"🪙 {s.symbol}", value=value, inline=False)

        # 포지션 보유 코인이 하나 이상인 경우에만 총 평가손익 표시
        if has_position:
            pnl_icon = "🟢" if total_pnl >= 0 else "🔴"
            embed.add_field(
                name="📈 총 평가손익",
                value=f"{pnl_icon} **{total_pnl:+,.0f} KRW**",
                inline=False,
            )

        embed.set_footer(text="💡 /잔고 명령어로 언제든 현황을 확인할 수 있습니다.")
        return embed

    # ------------------------------------------------------------------
    # DM 전송 (Embed 전용, 최대 3회 재시도)
    # ------------------------------------------------------------------

    async def _send_dm_embed(self, user_id: str, embed: discord.Embed) -> None:
        """사용자에게 Embed DM을 전송한다. HTTPException 시 최대 3회 재시도.

        Args:
            user_id: Discord 사용자 ID (문자열).
            embed: 전송할 discord.Embed 객체.
        """
        for attempt in range(1, 4):
            try:
                user = await self.bot.fetch_user(int(user_id))
                await user.send(embed=embed)
                return  # 전송 성공
            except discord.Forbidden:
                # 403: 사용자가 DM을 차단한 경우 — 재시도 불필요
                logger.warning(
                    "정기 보고 DM 거부됨 (DM 차단): user_id=%s", user_id
                )
                return
            except discord.HTTPException as exc:
                if attempt < 3:
                    logger.warning(
                        "정기 보고 DM 실패 (시도 %d/3, HTTP %s): user_id=%s — 3초 후 재시도",
                        attempt,
                        exc.status,
                        user_id,
                    )
                    await asyncio.sleep(3)
                else:
                    logger.error(
                        "정기 보고 DM 최종 실패 (3회 모두 실패, HTTP %s): user_id=%s",
                        exc.status,
                        user_id,
                    )
            except Exception as exc:
                logger.error(
                    "정기 보고 DM 오류 (재시도 불가): user_id=%s err=%s", user_id, exc
                )
                return
