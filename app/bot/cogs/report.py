"""
정기 보고 Cog: 자동 매매 실행 중인 사용자에게 주기적으로 수익률 현황을 DM으로 전송.

사용자별 설정:
  - report_enabled        : 보고 수신 on/off (기본 켜짐)
  - report_interval_hours : 보고 주기 — 1 / 3 / 6 / 12 / 24 시간 (기본 1)
  - last_report_sent_at   : 마지막 전송 시각 (DB 영속, 재시작 후에도 주기 유지)

루프 주기는 1시간 고정이며, 각 사용자의 last_report_sent_at 와 interval 을
비교해 실제 전송 여부를 결정합니다.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.database import AsyncSessionLocal
from app.models.bot_setting import BotSetting
from app.models.user import User
from app.services.websocket import UpbitWebsocketManager
from app.utils.format import format_krw_price

logger = logging.getLogger(__name__)

# 허용 보고 주기 선택지 (Discord Choice)
_INTERVAL_CHOICES = [
    app_commands.Choice(name="1시간마다", value=1),
    app_commands.Choice(name="3시간마다", value=3),
    app_commands.Choice(name="6시간마다", value=6),
    app_commands.Choice(name="12시간마다", value=12),
    app_commands.Choice(name="24시간마다", value=24),
]


class ReportCog(commands.Cog):
    """매 시간 사용자별 설정을 확인해 수익률 요약 DM을 전송하는 Cog.

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
    # 백그라운드 루프 (1시간 주기로 실행, 개별 전송 여부는 내부에서 판단)
    # ------------------------------------------------------------------

    @tasks.loop(hours=1)
    async def hourly_report(self) -> None:
        """실행 중인 BotSetting을 사용자별로 묶어 수익률 요약 DM을 전송한다.

        처리 흐름:
          1. DB: is_running=True BotSetting + 연관 User 일괄 조회
          2. user_id 기준 그룹핑
          3. 각 사용자별:
               a. report_enabled=False → 스킵
               b. last_report_sent_at 기준 주기 미도달 → 스킵
               c. Embed 빌드 → DM 전송 → last_report_sent_at 갱신
          4. 사용자 간 asyncio.sleep(1) 으로 Discord Rate-Limit 방지
        """
        logger.info("정기 보고 루프 실행")
        now = datetime.now(timezone.utc)

        # 1. 실행 중인 BotSetting + User 일괄 조회
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(BotSetting)
                .where(BotSetting.is_running.is_(True))
                .options(selectinload(BotSetting.user))
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
        sent = skipped_disabled = skipped_interval = 0

        for user_id, settings in user_settings.items():
            # User 레코드 — selectinload 로 이미 로드됨
            user: User | None = settings[0].user
            if user is None:
                logger.warning("User 레코드 없음, 보고 스킵: user_id=%s", user_id)
                continue

            # a. 보고 비활성화 여부 확인
            if not user.report_enabled:
                logger.debug("정기 보고 스킵 (비활성화): user_id=%s", user_id)
                skipped_disabled += 1
                continue

            # b. 전송 주기 도달 여부 확인
            if user.last_report_sent_at is not None:
                last = user.last_report_sent_at
                # DB에 naive datetime이 저장된 경우 UTC로 보정
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                elapsed_hours = (now - last).total_seconds() / 3600
                if elapsed_hours < user.report_interval_hours:
                    logger.debug(
                        "정기 보고 스킵 (주기 미도달 %.1fh / %dh): user_id=%s",
                        elapsed_hours,
                        user.report_interval_hours,
                        user_id,
                    )
                    skipped_interval += 1
                    continue

            # c. DM 전송 및 last_report_sent_at 갱신
            try:
                embed = self._build_report_embed(settings, ws_manager)
                await self._send_dm_embed(user_id, embed)

                # last_report_sent_at 업데이트 (별도 세션)
                async with AsyncSessionLocal() as db:
                    await db.execute(
                        update(User)
                        .where(User.user_id == user_id)
                        .values(last_report_sent_at=now)
                    )
                    await db.commit()

                sent += 1
            except Exception as exc:
                logger.error(
                    "정기 보고 처리 오류: user_id=%s err=%s", user_id, exc
                )

            # Rate-Limit 방지: 사용자 간 1초 간격
            await asyncio.sleep(1)

        logger.info(
            "정기 보고 루프 완료 — 전송 %d명 | 주기 미도달 %d명 | 비활성 %d명",
            sent,
            skipped_interval,
            skipped_disabled,
        )

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
                    f"**매수가:** {format_krw_price(s.buy_price)} KRW\n"
                    f"**현재가:** {format_krw_price(current_price)} KRW\n"
                    f"**수익률:** {status_icon} **{profit_pct:+.2f}%** ({pnl:+,.0f} KRW)\n"
                    f"**수량:** {s.amount_coin:.6f}"
                )
            elif current_price:
                # 매수 대기 중 — 현재가만 표시
                value = f"⏳ 매수 대기 중\n**현재가:** {format_krw_price(current_price)} KRW"
            else:
                # 시세 미수신 (WebSocket 초기화 중)
                value = "⏳ 매수 대기 중 또는 시세 로딩 중..."

            embed.add_field(name=f"🪙 {s.symbol}", value=value, inline=False)

        # 포지션 보유 코인이 하나 이상일 때만 총 평가손익 표시
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

    # ------------------------------------------------------------------
    # /보고설정 슬래시 커맨드
    # ------------------------------------------------------------------

    @app_commands.command(
        name="보고설정",
        description="정기 수익률 보고 DM 수신 여부와 주기를 설정합니다.",
    )
    @app_commands.describe(
        enabled="정기 보고 수신 여부 (True: 켜기 / False: 끄기)",
        interval="보고 주기를 선택하세요 (미선택 시 현재 설정 유지)",
    )
    @app_commands.choices(interval=_INTERVAL_CHOICES)
    async def report_settings_command(
        self,
        interaction: discord.Interaction,
        enabled: Optional[bool] = None,
        interval: Optional[app_commands.Choice[int]] = None,
    ) -> None:
        """보고 수신 여부 및 주기를 DB에 저장한다.

        enabled 와 interval 중 하나만 전달해도 해당 항목만 업데이트한다.
        둘 다 생략하면 현재 설정을 조회해 응답한다.
        """
        user_id = str(interaction.user.id)

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == user_id))
            user = result.scalar_one_or_none()

            if user is None:
                await interaction.response.send_message(
                    "⚠️ 등록된 계정이 없습니다. `/키등록` 명령어로 먼저 등록해 주세요.",
                    ephemeral=True,
                )
                return

            # 변경 없음 — 현재 설정 조회만
            if enabled is None and interval is None:
                interval_str = f"{user.report_interval_hours}시간마다"
                status_str = "켜짐 ✅" if user.report_enabled else "꺼짐 ⛔"
                await interaction.response.send_message(
                    f"📋 **현재 보고 설정**\n"
                    f"수신 여부: **{status_str}**\n"
                    f"보고 주기: **{interval_str}**",
                    ephemeral=True,
                )
                return

            # 변경 사항 적용
            if enabled is not None:
                user.report_enabled = enabled
            if interval is not None:
                user.report_interval_hours = interval.value
                # 주기 변경 시 last_report_sent_at 초기화 → 변경 직후 즉시 1회 전송
                user.last_report_sent_at = None

            await db.commit()

        # 결과 메시지 빌드
        parts: list[str] = ["✅ **보고 설정이 변경되었습니다.**"]
        if enabled is not None:
            parts.append(f"수신 여부: **{'켜짐 ✅' if enabled else '꺼짐 ⛔'}**")
        if interval is not None:
            parts.append(f"보고 주기: **{interval.name}**")
            parts.append("_(주기 변경으로 다음 루프에서 즉시 보고가 전송됩니다)_")

        await interaction.response.send_message("\n".join(parts), ephemeral=True)

    # ------------------------------------------------------------------
    # /내포지션 슬래시 커맨드
    # ------------------------------------------------------------------

    @app_commands.command(
        name="내포지션",
        description="현재 보유 중인 AI 자동매매 포지션을 조회하고 수동 청산할 수 있습니다.",
    )
    async def my_positions_command(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """보유 중인 포지션(is_running=True, buy_price IS NOT NULL)을 조회해
        Embed + ManualSellView로 응답한다.

        포지션이 없으면 ephemeral 텍스트만 반환한다.
        포지션이 있으면 요약 Embed + ManualSellView(수동 청산 UI)를 ephemeral로 반환한다.
        """
        user_id = str(interaction.user.id)

        # ── DB 조회 ─────────────────────────────────────────────────
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(BotSetting).where(
                        BotSetting.user_id == user_id,
                        BotSetting.is_running.is_(True),
                        BotSetting.buy_price.is_not(None),
                    )
                )
                settings: list[BotSetting] = result.scalars().all()
        except Exception as exc:
            logger.error("내포지션 DB 조회 실패: user_id=%s err=%s", user_id, exc)
            await interaction.response.send_message(
                "포지션 조회 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
                ephemeral=True,
            )
            return

        logger.info("내포지션 조회: user_id=%s count=%d", user_id, len(settings))

        if not settings:
            await interaction.response.send_message(
                "현재 보유 중인 포지션이 없습니다.",
                ephemeral=True,
            )
            return

        # ── Embed 빌드 ───────────────────────────────────────────────
        ws_manager = UpbitWebsocketManager.get()
        embed = discord.Embed(
            title="📊 내 포지션 현황",
            description="현재 자동매매 중인 포지션 목록입니다.",
            color=discord.Color.blue(),
        )

        positions: list[dict] = []
        for s in settings:
            ws_price = ws_manager.get_price(s.symbol)
            mode_tag = "[모의]" if s.is_paper_trading else "[실전]"
            buy_price_f = float(s.buy_price)

            if ws_price is not None:
                profit_pct = (float(ws_price) - buy_price_f) / buy_price_f * 100
                status_icon = "🟢" if profit_pct >= 0 else "🔴"
                profit_str = f"{status_icon} **{profit_pct:+.2f}%**"
                current_price_str = f"{format_krw_price(ws_price)} KRW"
            else:
                profit_pct = 0.0
                profit_str = "-"
                current_price_str = "조회 중..."

            amount_str = (
                f"{float(s.amount_coin):.6f}" if s.amount_coin is not None else "-"
            )

            embed.add_field(
                name=f"{mode_tag} {s.symbol}",
                value=(
                    f"**매수가:** {format_krw_price(buy_price_f)} KRW\n"
                    f"**현재가:** {current_price_str}\n"
                    f"**수익률:** {profit_str}\n"
                    f"**수량:** {amount_str}"
                ),
                inline=False,
            )

            positions.append({
                "setting_id": s.id,
                "symbol": s.symbol,
                "is_paper": s.is_paper_trading,
                "profit_pct": profit_pct,
            })

        embed.set_footer(
            text="코인을 선택하면 즉시 청산됩니다 (1-Click)."
        )

        # ── ManualSellView 부착 (지역 import — 순환 임포트 방지) ─────
        from app.bot.views.manual_sell_view import ManualSellView  # noqa: PLC0415

        view = ManualSellView(bot=self.bot, user_id=user_id, positions=positions)

        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True,
        )
