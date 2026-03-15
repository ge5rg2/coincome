"""
/ai실전 슬래시 커맨드: VIP 전용 AI 실전 자동 매매 펀드 매니저 기능 설정.

처리 흐름 (2단계 UI):
  [Step 1] VIP 등급 검증 → 미달 시 업그레이드 유도 Embed 반환
  [Step 2] VIP 확인 → AISettingView 표시 (드롭다운: AI모드·투자성향)
  [Step 3] "다음 →" 버튼 클릭 → AIAmountModal 표시 (숫자 입력: 금액·종목수)
  [Step 4] 유저 제출 → DB 업데이트 → 완료 Embed 반환

Discord API 제약:
  Modal 내부에는 TextInput 만 허용 (Select 불가).
  드롭다운 선택지(ON/OFF, SWING/SCALPING)는 Step 2 View 단계에서 처리하고,
  선택된 값을 Step 3 Modal 생성자에 전달한다.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.user import SubscriptionTier, User
from app.utils.time import get_next_run_time_for_style

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
# Step 1: 드롭다운 Select 컴포넌트
# ------------------------------------------------------------------

class ModeSelect(discord.ui.Select):
    """AI 모드(ON / OFF) 드롭다운."""

    def __init__(self, current_enabled: bool) -> None:
        options = [
            discord.SelectOption(
                label="✅ ON — AI 자동매매 활성화",
                value="ON",
                default=current_enabled,
            ),
            discord.SelectOption(
                label="⏸️ OFF — AI 자동매매 비활성화",
                value="OFF",
                default=not current_enabled,
            ),
        ]
        super().__init__(placeholder="AI 모드를 선택하세요", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.view.mode_value = self.values[0]
        await interaction.response.defer()


class StyleSelect(discord.ui.Select):
    """투자 성향(SWING / SCALPING) 드롭다운."""

    def __init__(self, current_style: str) -> None:
        options = [
            discord.SelectOption(
                label="📊 SWING — 4h 보수 스윙",
                value="SWING",
                description="4시간 봉 RSI·MA 기반 보수적 스윙 매매",
                default=current_style == "SWING",
            ),
            discord.SelectOption(
                label="⚡ SCALPING — 1h 공격 단타",
                value="SCALPING",
                description="1시간 봉 모멘텀 기반 빠른 단타 매매",
                default=current_style == "SCALPING",
            ),
        ]
        super().__init__(placeholder="투자 성향을 선택하세요", options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.view.style_value = self.values[0]
        await interaction.response.defer()


# ------------------------------------------------------------------
# Step 1: View (드롭다운 + "다음" 버튼)
# ------------------------------------------------------------------

class AISettingView(discord.ui.View):
    """1단계: AI 모드·투자 성향을 드롭다운으로 선택하는 View.

    "다음 →" 버튼 클릭 시 선택된 값을 AIAmountModal(2단계)에 전달한다.
    timeout=180 초 (이후 버튼 비활성화).

    Attributes:
        mode_value:  현재 선택된 AI 모드 ("ON" / "OFF").
        style_value: 현재 선택된 투자 성향 ("SWING" / "SCALPING").
    """

    def __init__(self, user: User) -> None:
        super().__init__(timeout=180)
        self._user = user
        # Select 콜백이 업데이트할 인스턴스 변수 (초기값 = 기존 DB 설정)
        self.mode_value: str = "ON" if user.ai_mode_enabled else "OFF"
        self.style_value: str = getattr(user, "ai_trade_style", "SWING")

        self.add_item(ModeSelect(current_enabled=user.ai_mode_enabled))
        self.add_item(StyleSelect(current_style=self.style_value))

    @discord.ui.button(label="다음 →", style=discord.ButtonStyle.primary, emoji="⚙️", row=2)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """선택된 모드·성향을 AIAmountModal 에 넘겨 Modal 을 표시한다."""
        modal = AIAmountModal(
            user_id=self._user.user_id,
            mode=self.mode_value,
            style=self.style_value,
            current_amount=int(self._user.ai_trade_amount),
            current_max_coins=self._user.ai_max_coins,
        )
        await interaction.response.send_modal(modal)


# ------------------------------------------------------------------
# Step 2: Modal (숫자 입력 + DB 저장)
# ------------------------------------------------------------------

class AIAmountModal(discord.ui.Modal, title="AI 실전 — 금액 설정"):
    """2단계: 매수 금액과 최대 종목 수를 입력받아 DB에 저장하는 Modal.

    Step 1 View에서 선택된 mode·style 값을 생성자로 받아 함께 저장한다.

    Args:
        user_id:           Discord 사용자 ID.
        mode:              "ON" 또는 "OFF" (Step 1 에서 선택).
        style:             "SWING" 또는 "SCALPING" (Step 1 에서 선택).
        current_amount:    현재 DB 값 (pre-fill 용).
        current_max_coins: 현재 DB 값 (pre-fill 용).
    """

    def __init__(
        self,
        user_id: str,
        mode: str,
        style: str,
        current_amount: int,
        current_max_coins: int,
    ) -> None:
        super().__init__()
        self._user_id = user_id
        self._mode = mode
        self._style = style

        self.trade_amount = discord.ui.TextInput(
            label="1회 매수 금액 (KRW)",
            placeholder="예: 10000  (최소 6,000)",
            min_length=4,
            max_length=10,
            default=str(current_amount),
        )
        self.max_coins = discord.ui.TextInput(
            label="최대 동시 보유 종목 수",
            placeholder="예: 3  (1 ~ 10)",
            min_length=1,
            max_length=2,
            default=str(current_max_coins),
        )
        self.add_item(self.trade_amount)
        self.add_item(self.max_coins)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        # ── 입력값 검증 ───────────────────────────────────────────────
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

        enabled = self._mode == "ON"

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
            user.ai_trade_style = self._style
            await db.commit()

        logger.info(
            "AI 실전 설정 업데이트: user_id=%s enabled=%s amount=%d max_coins=%d style=%s",
            self._user_id, enabled, amount, max_coins, self._style,
        )

        # ── 완료 Embed 반환 ───────────────────────────────────────────
        status = "✅ 활성화" if enabled else "⏸️ 비활성화"
        style_label = "📊 스윙 (4h 봉)" if self._style == "SWING" else "⚡ 단타 (1h 봉)"
        embed = discord.Embed(
            title="🤖 AI 실전 자동 매매 설정 완료",
            color=discord.Color.green() if enabled else discord.Color.greyple(),
        )
        embed.add_field(name="AI 모드", value=status, inline=True)
        embed.add_field(name="1회 매수 금액", value=f"{amount:,} KRW", inline=True)
        embed.add_field(name="최대 보유 종목", value=f"{max_coins}개", inline=True)
        embed.add_field(name="투자 성향", value=style_label, inline=True)

        if enabled:
            next_time = get_next_run_time_for_style(self._style)
            schedule_desc = (
                "매시 정각 실행 (1h 봉 기준 단타)" if self._style == "SCALPING"
                else "01·05·09·13·17·21시 실행 (4h 봉 기준 스윙)"
            )
            embed.set_footer(text=f"⏳ 다음 AI 분석: {next_time} | {schedule_desc}")
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
        name="ai실전",
        description="VIP 전용 AI 실전 자동 매매 펀드 매니저를 설정합니다.",
    )
    async def ai_settings_command(self, interaction: discord.Interaction) -> None:
        """VIP 여부를 확인한 뒤 드롭다운 선택 View(1단계)를 띄운다.

        [VIP 검증] FREE / PRO 등급이면 업그레이드 유도 Embed로 즉시 반환.
        [설정 UI ] 드롭다운(모드·성향) → "다음 →" 버튼 → Modal(금액·종목수) 2단계 흐름.
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

        # ── 현재 설정값 요약 Embed + View 표시 ───────────────────────
        current_style = getattr(user, "ai_trade_style", "SWING")
        style_label = "⚡ 단타 (1h 봉)" if current_style == "SCALPING" else "📊 스윙 (4h 봉)"
        embed = discord.Embed(
            title="🤖 AI 실전 자동 매매 설정",
            description=(
                "드롭다운에서 **AI 모드**와 **투자 성향**을 선택한 뒤\n"
                "**⚙️ 다음 →** 버튼을 눌러 매수 금액을 입력하세요."
            ),
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="현재 설정",
            value=(
                f"AI 모드: **{'ON' if user.ai_mode_enabled else 'OFF'}**\n"
                f"투자 성향: **{style_label}**\n"
                f"1회 매수: **{int(user.ai_trade_amount):,} KRW** "
                f"| 최대 종목: **{user.ai_max_coins}개**"
            ),
            inline=False,
        )
        embed.set_footer(text="⏱️ 이 메시지는 3분 후 만료됩니다.")

        view = AISettingView(user=user)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
