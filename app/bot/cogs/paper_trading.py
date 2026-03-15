"""
모의·AI 통계 슬래시 커맨드 Cog.

/ai모의        : AI 모의투자 ON/OFF 설정 (모든 등급 사용 가능).
                 API 키 없이 가상 잔고(virtual_krw)로 AI가 자동 종목 선정·매수.
/ai모의초기화  : 모의투자 전체 초기화.
                 가상 잔고 1,000만 원 리셋 + 모의 워커 중지 + BotSetting/TradeHistory 삭제.
/ai통계        : AI 매매 성과 Embed 리포트.
                 VIP(ai_mode_enabled=True) → 실전 AI 통계 + 모의투자 통계 모두 표시.
                 그 외 / 실전 기록 없는 유저 → 모의투자 통계만 표시.

격리 정책:
  - BotSetting.is_paper_trading 플래그로 실전·모의 포지션 완전 분리.
  - BotSetting.is_ai_managed 플래그로 수동 봇 설정과 AI 관리 포지션 분리.
  - 실전 슬롯과 모의 슬롯은 각각 독립 카운트.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, desc, select

from app.database import AsyncSessionLocal
from app.models.bot_setting import BotSetting
from app.models.trade_history import TradeHistory
from app.models.user import SubscriptionTier, User
from app.services.trading_worker import WorkerRegistry
from app.services.websocket import UpbitWebsocketManager
from app.utils.time import get_next_ai_run_time

logger = logging.getLogger(__name__)

# 모의투자 초기 가상 잔고 (표시·초기화 전용 — 실제 기본값은 User.virtual_krw 에 저장)
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
    """AI 모의투자·통계 관련 슬래시 커맨드 Cog."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /ai모의 — AI 모의투자 ON/OFF 설정
    # ------------------------------------------------------------------

    @app_commands.command(
        name="ai모의",
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
    # /ai모의초기화 — 모의투자 전체 리셋
    # ------------------------------------------------------------------

    @app_commands.command(
        name="ai모의초기화",
        description="모의투자를 초기화합니다. 가상 잔고·진행 중인 포지션·거래 이력이 모두 삭제됩니다.",
    )
    async def paper_reset_command(self, interaction: discord.Interaction) -> None:
        """모의투자 초기화: 워커 중지 → BotSetting/TradeHistory 삭제 → 잔고 리셋.

        처리 순서:
          1. WorkerRegistry에서 해당 유저의 모의투자 워커 태스크 취소.
          2. DB에서 is_paper_trading=True BotSetting 레코드 전체 삭제.
          3. DB에서 is_paper_trading=True TradeHistory 레코드 전체 삭제.
          4. User.virtual_krw 를 10,000,000 으로 초기화.
        """
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == user_id))
            user = result.scalar_one_or_none()

            if user is None:
                await interaction.followup.send(
                    "❌ 등록된 계정이 없습니다.\n"
                    "`/ai모의` 명령어로 설정을 먼저 완료해 주세요.",
                    ephemeral=True,
                )
                return

        # ── 1. 모의투자 워커 태스크 취소 (DB 조작 없이 태스크만 종료) ─
        registry = WorkerRegistry.get()
        await registry.stop_paper_for_user(user_id)

        # ── 2·3. BotSetting·TradeHistory 모의 레코드 삭제 + 잔고 초기화 ─
        async with AsyncSessionLocal() as db:
            # 모의 포지션 전체 삭제
            await db.execute(
                delete(BotSetting).where(
                    BotSetting.user_id == user_id,
                    BotSetting.is_paper_trading.is_(True),
                )
            )
            # 모의 거래 이력 전체 삭제
            await db.execute(
                delete(TradeHistory).where(
                    TradeHistory.user_id == user_id,
                    TradeHistory.is_paper_trading.is_(True),
                )
            )
            # 가상 잔고 초기화
            result = await db.execute(select(User).where(User.user_id == user_id))
            user = result.scalar_one_or_none()
            if user is not None:
                user.virtual_krw = _INITIAL_VIRTUAL_KRW
            await db.commit()

        logger.info(
            "모의투자 초기화 완료: user_id=%s (잔고 %.0f KRW 리셋)",
            user_id, _INITIAL_VIRTUAL_KRW,
        )

        embed = discord.Embed(
            title="🔄 AI 모의투자 초기화 완료",
            description="모의투자 데이터가 모두 삭제되고 처음 상태로 돌아갔습니다.",
            color=discord.Color.orange(),
        )
        embed.add_field(
            name="💰 가상 잔고",
            value=f"**{_INITIAL_VIRTUAL_KRW:,.0f} KRW** (초기값으로 리셋)",
            inline=True,
        )
        embed.add_field(
            name="📋 삭제 항목",
            value="• 진행 중인 모의 포지션 전체\n• 모의 거래 이력 전체",
            inline=True,
        )
        embed.add_field(
            name="📌 안내",
            value=(
                "AI 모의투자 ON/OFF 설정은 유지됩니다.\n"
                "다음 AI 스케줄러 실행 시부터 새 시드로 다시 시작합니다."
            ),
            inline=False,
        )
        embed.set_footer(text="💡 /ai모의 로 설정 변경, /ai통계 로 성과 확인")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /ai통계 — AI 성과 리포트 (동적 렌더링)
    # ------------------------------------------------------------------

    @app_commands.command(
        name="ai통계",
        description="AI 매매 성과를 확인합니다. VIP 실전 통계 + 모의투자 통계를 표시합니다.",
    )
    async def ai_stats_command(self, interaction: discord.Interaction) -> None:
        """AI 매매 성과 리포트 Embed를 전송한다.

        렌더링 정책:
          - VIP + ai_mode_enabled=True: [실전 AI 통계] + [모의투자 통계]
          - 그 외(또는 실전 기록 없음): [모의투자 통계]만 표시

        조회 항목:
          실전) TradeHistory(is_paper=False) + BotSetting(is_running, is_paper=False, is_ai_managed=True)
          모의) TradeHistory(is_paper=True)  + BotSetting(is_running, is_paper=True)
               + User.virtual_krw
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
                    "`/ai모의` 명령어로 설정을 먼저 완료해 주세요.",
                    ephemeral=True,
                )
                return

            # ── 실전 AI 섹션 표시 여부 결정 ───────────────────────────
            show_real = (
                user.subscription_tier == SubscriptionTier.VIP
                and user.ai_mode_enabled
            )

            # ── 실전 데이터 조회 (VIP + ai_mode_enabled 시에만) ───────
            real_histories: list[TradeHistory] = []
            real_open: list[BotSetting] = []
            if show_real:
                real_hist_result = await db.execute(
                    select(TradeHistory)
                    .where(
                        TradeHistory.user_id == user_id,
                        TradeHistory.is_paper_trading.is_(False),
                    )
                    .order_by(desc(TradeHistory.created_at))
                )
                real_histories = real_hist_result.scalars().all()

                real_open_result = await db.execute(
                    select(BotSetting).where(
                        BotSetting.user_id == user_id,
                        BotSetting.is_running.is_(True),
                        BotSetting.is_paper_trading.is_(False),
                        BotSetting.is_ai_managed.is_(True),
                    )
                )
                real_open = real_open_result.scalars().all()

            # ── 모의투자 데이터 조회 (항상) ───────────────────────────
            paper_hist_result = await db.execute(
                select(TradeHistory)
                .where(
                    TradeHistory.user_id == user_id,
                    TradeHistory.is_paper_trading.is_(True),
                )
                .order_by(desc(TradeHistory.created_at))
            )
            paper_histories: list[TradeHistory] = paper_hist_result.scalars().all()

            paper_open_result = await db.execute(
                select(BotSetting).where(
                    BotSetting.user_id == user_id,
                    BotSetting.is_running.is_(True),
                    BotSetting.is_paper_trading.is_(True),
                )
            )
            paper_open: list[BotSetting] = paper_open_result.scalars().all()

        ws_manager = UpbitWebsocketManager.get()
        virtual_krw = float(user.virtual_krw)

        # ── 실전 통계 계산 ────────────────────────────────────────────
        real_total = len(real_histories)
        real_wins = sum(1 for h in real_histories if h.profit_pct > 0)
        real_win_rate = real_wins / real_total * 100 if real_total > 0 else 0.0
        real_total_pnl = sum(h.profit_krw for h in real_histories)
        real_total_invested = sum(h.buy_amount_krw for h in real_histories)
        real_cum_pct = (
            real_total_pnl / real_total_invested * 100
            if real_total_invested > 0
            else 0.0
        )

        # ── 모의 통계 계산 ────────────────────────────────────────────
        paper_total = len(paper_histories)
        paper_wins = sum(1 for h in paper_histories if h.profit_pct > 0)
        paper_win_rate = paper_wins / paper_total * 100 if paper_total > 0 else 0.0
        paper_total_pnl = sum(h.profit_krw for h in paper_histories)
        paper_total_invested = sum(h.buy_amount_krw for h in paper_histories)
        paper_cum_pct = (
            paper_total_pnl / paper_total_invested * 100
            if paper_total_invested > 0
            else 0.0
        )
        balance_change = virtual_krw - _INITIAL_VIRTUAL_KRW

        # ── Embed 구성 ────────────────────────────────────────────────
        # 컬러: 가상 잔고 손익 기준
        pnl_color = discord.Color.green() if balance_change >= 0 else discord.Color.red()
        title = (
            "📊 AI 매매 성과 리포트"
            if show_real
            else "📊 AI 모의투자 성과 리포트"
        )
        embed = discord.Embed(title=title, color=pnl_color)

        # ════════════════════════════════════════════════════════════
        # 실전 AI 섹션 (VIP + ai_mode_enabled 시에만)
        # ════════════════════════════════════════════════════════════
        if show_real:
            # 실전 완료 거래 요약
            if real_total > 0:
                real_stats_value = (
                    f"총 거래: **{real_total}회** | "
                    f"**{real_wins}승 {real_total - real_wins}패** | "
                    f"승률: **{real_win_rate:.1f}%**\n"
                    f"누적 손익: **{real_total_pnl:+,.0f} KRW** ({real_cum_pct:+.2f}%)"
                )
            else:
                real_stats_value = "아직 실전 AI 거래 이력이 없습니다."

            embed.add_field(
                name="👑 실전 AI 완료 거래",
                value=real_stats_value,
                inline=False,
            )

            # 현재 실전 오픈 포지션
            if real_open:
                real_lines: list[str] = []
                for s in real_open:
                    current_price = ws_manager.get_price(s.symbol)
                    if s.buy_price is not None and current_price is not None:
                        pct = (current_price - float(s.buy_price)) / float(s.buy_price) * 100
                        icon = "🟢" if pct >= 0 else "🔴"
                        real_lines.append(
                            f"{icon} **{s.symbol}** | "
                            f"{float(s.buy_price):,.0f} → {current_price:,.0f} KRW"
                            f" | **{pct:+.2f}%**"
                        )
                    elif s.buy_price is None:
                        real_lines.append(f"⏳ **{s.symbol}** | 매수 대기 중...")
                    else:
                        real_lines.append(f"❓ **{s.symbol}** | 시세 수신 대기 중...")
                embed.add_field(
                    name=f"💼 실전 진행 중 ({len(real_open)}건)",
                    value="\n".join(real_lines),
                    inline=False,
                )
            else:
                embed.add_field(
                    name="💼 실전 진행 중",
                    value="현재 실전 AI 포지션이 없습니다.",
                    inline=False,
                )

            # 최근 실전 거래 (최대 3건)
            if real_histories:
                rec_lines: list[str] = []
                for h in real_histories[:3]:
                    icon = "🟢" if h.profit_pct > 0 else "🔴"
                    date_str = h.created_at.strftime("%m/%d %H:%M") if h.created_at else "-"
                    rec_lines.append(
                        f"{icon} **{h.symbol}** `{date_str}` | "
                        f"{h.buy_price:,.0f} → {h.sell_price:,.0f} KRW"
                        f" | **{h.profit_pct:+.2f}%**"
                    )
                embed.add_field(
                    name="📋 최근 실전 거래 (최대 3건)",
                    value="\n".join(rec_lines),
                    inline=False,
                )

            # 구분선 (모의투자 섹션이 이어짐)
            embed.add_field(
                name="━━━━━━━━━━━━━━━━━━━━━━",
                value="\u200b",
                inline=False,
            )

        # ════════════════════════════════════════════════════════════
        # 모의투자 섹션 (항상 표시)
        # ════════════════════════════════════════════════════════════

        # 1) 가상 잔고 현황
        balance_icon = "📈" if balance_change >= 0 else "📉"
        embed.add_field(
            name="🎮 모의투자 가상 잔고",
            value=(
                f"**{virtual_krw:,.0f} KRW**\n"
                f"{balance_icon} 초기 대비: **{balance_change:+,.0f} KRW**\n"
                f"_(초기 잔고: {_INITIAL_VIRTUAL_KRW:,.0f} KRW)_"
            ),
            inline=True,
        )

        # 2) 모의 누적 성과
        if paper_total > 0:
            paper_stats_value = (
                f"총 거래: **{paper_total}회**\n"
                f"승/패: **{paper_wins}승 {paper_total - paper_wins}패**\n"
                f"승률: **{paper_win_rate:.1f}%**\n"
                f"누적 손익: **{paper_total_pnl:+,.0f} KRW** ({paper_cum_pct:+.2f}%)"
            )
        else:
            paper_stats_value = "아직 완료된 모의 거래가 없습니다."

        embed.add_field(name="🎮 모의 완료 거래 성과", value=paper_stats_value, inline=True)

        # 3) 현재 진행 중인 모의 오픈 포지션 + 미실현 손익
        if paper_open:
            paper_lines: list[str] = []
            unrealized_pnl = 0.0
            for s in paper_open:
                current_price = ws_manager.get_price(s.symbol)
                if s.buy_price is not None and current_price is not None:
                    pct = (current_price - float(s.buy_price)) / float(s.buy_price) * 100
                    pnl = (current_price - float(s.buy_price)) * float(s.amount_coin or 0)
                    unrealized_pnl += pnl
                    icon = "🟢" if pct >= 0 else "🔴"
                    paper_lines.append(
                        f"{icon} **{s.symbol}**\n"
                        f"  매수: {float(s.buy_price):,.0f} → 현재: {current_price:,.0f} KRW"
                        f" | **{pct:+.2f}%** ({pnl:+,.0f} KRW)"
                    )
                elif s.buy_price is None:
                    paper_lines.append(f"⏳ **{s.symbol}** | 매수 대기 중...")
                else:
                    paper_lines.append(f"❓ **{s.symbol}** | 시세 수신 대기 중...")
            unrealized_str = f"\n\n미실현 손익 합계: **{unrealized_pnl:+,.0f} KRW**"
            embed.add_field(
                name=f"👀 현재 진행 중인 모의투자 ({len(paper_open)}건)",
                value="\n".join(paper_lines) + unrealized_str,
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

        # 4) 최근 모의 완료 거래 (최대 5건)
        if paper_histories:
            rec_lines_paper: list[str] = []
            for h in paper_histories[:5]:
                icon = "🟢" if h.profit_pct > 0 else "🔴"
                date_str = h.created_at.strftime("%m/%d %H:%M") if h.created_at else "-"
                rec_lines_paper.append(
                    f"{icon} **{h.symbol}** `{date_str}`\n"
                    f"  {h.buy_price:,.0f} → {h.sell_price:,.0f} KRW"
                    f" | **{h.profit_pct:+.2f}%** ({h.profit_krw:+,.0f} KRW)"
                )
            embed.add_field(
                name="📋 최근 모의 거래 기록 (최대 5건)",
                value="\n".join(rec_lines_paper),
                inline=False,
            )
        else:
            embed.add_field(
                name="📋 최근 모의 거래 기록",
                value=(
                    "아직 완료된 모의 거래가 없습니다.\n"
                    "`/ai모의`를 ON 으로 설정하고 AI 스케줄러를 기다리세요!"
                ),
                inline=False,
            )

        embed.set_footer(
            text=(
                "💡 /ai실전(VIP) 으로 실전 자동매매 | "
                "/ai모의 로 모의투자 ON/OFF | "
                "/ai모의초기화 로 리셋"
            )
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
