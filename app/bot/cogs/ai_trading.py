"""
/ai실전 슬래시 커맨드: VIP 전용 AI 실전 자동 매매 펀드 매니저 기능 설정.

처리 흐름 (2단계 UI):
  [Step 1] VIP 등급 검증 → 미달 시 업그레이드 유도 Embed 반환
  [Step 2] VIP 확인 → AISettingView 표시 (드롭다운: AI모드·투자성향)
  [Step 3] "다음 →" 버튼 클릭 → AIAmountModal 표시 (숫자 입력: 금액·종목수)
  [Step 4] 유저 제출 → DB 업데이트 → 완료 Embed 반환

Discord API 제약:
  Modal 내부에는 TextInput 만 허용 (Select 불가).
  드롭다운 선택지(ON/OFF, SNIPER/BEAST)는 Step 2 View 단계에서 처리하고,
  선택된 값을 Step 3 Modal 생성자에 전달한다.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.bot_setting import BotSetting
from app.models.user import SubscriptionTier, User
from app.services.trading_worker import WorkerRegistry
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
            "• Claude Sonnet 듀얼 전략 엔진 (추세 돌파 + 낙폭 반등 자동 전환)\n"
            "• 최대 N개 종목 자동 매수 및 워커 자동 등록\n"
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
    """투자 비중 모드(SNIPER / BEAST) 드롭다운.

    v7 알트코인 전략 기준:
      SNIPER — 시드 20% 투입 (안전 모드)
      BEAST  — 시드 70% 투입 (공격 모드)
    두 모드 모두 동일한 v7 4h 엔진을 사용한다.
    """

    def __init__(self, current_style: str) -> None:
        options = [
            discord.SelectOption(
                label="🛡️ SNIPER — 시드 20% 안전 모드",
                value="SNIPER",
                description="듀얼 전략 자동 전환 | 알트코인 집중 | 안정 우상향",
                default=current_style in ("SNIPER", "SWING"),
            ),
            discord.SelectOption(
                label="🔥 BEAST — 시드 70% 공격 모드",
                value="BEAST",
                description="듀얼 전략 자동 전환 | 고비중 투입 | 하이리스크 하이리턴",
                default=current_style in ("BEAST", "SCALPING"),
            ),
        ]
        super().__init__(placeholder="투자 비중 모드를 선택하세요", options=options, row=1)

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
        style_value: 현재 선택된 투자 비중 모드 ("SNIPER" / "BEAST").
    """

    def __init__(self, user: User) -> None:
        super().__init__(timeout=180)
        self._user = user
        # Select 콜백이 업데이트할 인스턴스 변수 (초기값 = 기존 DB 설정)
        self.mode_value: str = "ON" if user.ai_mode_enabled else "OFF"
        self.style_value: str = getattr(user, "ai_trade_style", "SNIPER")

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
            current_max_coins=self._user.ai_max_coins,
            current_budget=int(getattr(self._user, "ai_budget_krw", 0)),
        )
        await interaction.response.send_modal(modal)


# ------------------------------------------------------------------
# Step 2: Modal (숫자 입력 + DB 저장)
# ------------------------------------------------------------------

class AIAmountModal(discord.ui.Modal, title="AI 실전 — 예산 설정"):
    """2단계: 총 운용 예산·최대 종목 수를 입력받아 DB에 저장하는 Modal.

    Step 1 View에서 선택된 mode·style 값을 생성자로 받아 함께 저장한다.
    1회 매수금액은 선택한 모드 비중(SNIPER=20%, BEAST=70%)에 따라
    ``total_budget × weight_pct / 100`` 으로 파이썬 로직 내에서 자동 산정된다.

    Args:
        user_id:           Discord 사용자 ID.
        mode:              "ON" 또는 "OFF" (Step 1 에서 선택).
        style:             "SNIPER" 또는 "BEAST" (Step 1 에서 선택, 하위 호환: "SWING"/"SCALPING").
        current_max_coins: 현재 DB 값 (pre-fill 용).
        current_budget:    현재 DB 값 (pre-fill 용).
    """

    def __init__(
        self,
        user_id: str,
        mode: str,
        style: str,
        current_max_coins: int,
        current_budget: int = 0,
    ) -> None:
        super().__init__()
        self._user_id = user_id
        self._mode = mode
        self._style = style

        self.max_coins = discord.ui.TextInput(
            label="최대 동시 보유 종목 수",
            placeholder="예: 3  (1 ~ 10)",
            min_length=1,
            max_length=2,
            default=str(current_max_coins),
        )
        self.budget = discord.ui.TextInput(
            label="총 운용 예산 (KRW)",
            placeholder="예: 500000  (이 금액 기준으로 1회 매수금액 자동 산정)",
            min_length=4,
            max_length=12,
            default=str(current_budget) if current_budget > 0 else "",
        )
        self.add_item(self.max_coins)
        self.add_item(self.budget)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        # ── 최대 종목 수 검증 ──────────────────────────────────────────
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

        # ── 총 운용 예산 검증 ──────────────────────────────────────────
        try:
            budget_raw = (self.budget.value or "").replace(",", "").strip()
            budget = int(budget_raw) if budget_raw else 0
        except ValueError:
            await interaction.followup.send(
                "❌ 총 운용 예산은 숫자로 입력해 주세요.", ephemeral=True
            )
            return

        if budget < 6_000:
            await interaction.followup.send(
                "❌ 총 운용 예산은 **최소 6,000 KRW** 이상이어야 합니다.\n"
                "(업비트 최소 주문 한도 + 손절 하락분 고려)",
                ephemeral=True,
            )
            return

        # ── 모드 비중 기반 1회 매수금액 자동 산정 ────────────────────
        # SNIPER(20%) / BEAST(70%) 비중에 따라 budget 의 일정 % 를 1회 매수에 사용
        weight_pct = 20 if self._style in ("SNIPER", "SWING") else 70
        trade_amount = max(6_000, int(budget * weight_pct / 100))

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
            # 1회 매수금액 = 예산 × 모드 비중 (자동 산정)
            user.ai_trade_amount = trade_amount
            user.ai_max_coins = max_coins
            user.ai_trade_style = self._style
            user.ai_budget_krw = float(budget)
            user.ai_is_shutting_down = False  # 설정 변경 시 종료 모드 초기화
            await db.commit()

        logger.info(
            "AI 실전 설정 업데이트: user_id=%s enabled=%s "
            "budget=%d weight=%d%% trade_amount=%d max_coins=%d style=%s",
            self._user_id, enabled, budget, weight_pct, trade_amount, max_coins, self._style,
        )

        # ── 모드별 동적 Embed 생성 (SNIPER / BEAST 분기) ─────────────
        budget_str = f"{budget:,} KRW"
        trade_str  = f"{trade_amount:,} KRW (예산의 {weight_pct}% 자동 산정)"

        if not enabled:
            # OFF 선택: 간단한 비활성화 확인 Embed
            embed = discord.Embed(
                title="⏸️ AI 실전 자동 매매 비활성화",
                description="신규 AI 매수가 중단됩니다. 기존 실행 중인 워커는 계속 동작합니다.",
                color=discord.Color.greyple(),
            )
            embed.add_field(name="AI 모드", value="⏸️ 비활성화", inline=True)
            embed.add_field(name="최대 보유 종목", value=f"{max_coins}개", inline=True)
            embed.add_field(name="총 운용 예산", value=budget_str, inline=True)
            embed.set_footer(text="AI 자동 매매가 중지되었습니다. 기존 워커는 계속 동작합니다.")
        elif self._style in ("SNIPER", "SWING"):
            # SNIPER 모드 ON
            embed = discord.Embed(
                title="🛡️ 인텔리전트 스나이퍼 모드 가동",
                description=(
                    "가용 시드의 **20%** 투입. "
                    "MDD(최대 낙폭)를 최소화하며 안정적인 우상향을 추구하는 **안전 모드**입니다."
                ),
                color=discord.Color.blue(),
            )
            embed.add_field(name="AI 모드", value="✅ 활성화", inline=True)
            embed.add_field(name="최대 보유 종목", value=f"{max_coins}개", inline=True)
            embed.add_field(name="총 운용 예산", value=budget_str, inline=True)
            embed.add_field(name="💡 1회 매수금액 (자동)", value=trade_str, inline=False)
            # ── 공통 전략 설명 필드 ───────────────────────────────────
            embed.add_field(
                name="📋 전략",
                value=(
                    "**전략A** 추세 돌파 (MA50 상승 + RSI 55~70) — 익절 **6.0%** / 손절 **4.0%** (R:R 1.5:1)\n"
                    "**전략B** 낙폭 반등 (MA50 하락 + RSI < 25) — 익절 **3.0%** / 손절 **2.5%** (R:R 1.2:1)\n"
                    "BTC 상태에 따라 전략A/B 자동 전환 | 메이저 코인 거래 차단"
                ),
                inline=False,
            )
            next_time = get_next_run_time_for_style(self._style)
            embed.set_footer(text=f"⏳ 다음 AI 분석: {next_time} | 01·05·09·13·17·21시 실행 (4h 봉 기준)")
        else:
            # BEAST / SCALPING 모드 ON (하위 호환)
            embed = discord.Embed(
                title="🔥 야수의 심장 모드 가동",
                description=(
                    "가용 시드의 **70%** 투입. "
                    "리스크를 감수하고 폭발적인 수익을 노리는 "
                    "**하이리스크 하이리턴 공격 모드**입니다."
                ),
                color=discord.Color.red(),
            )
            embed.add_field(name="AI 모드", value="✅ 활성화", inline=True)
            embed.add_field(name="최대 보유 종목", value=f"{max_coins}개", inline=True)
            embed.add_field(name="총 운용 예산", value=budget_str, inline=True)
            embed.add_field(name="💡 1회 매수금액 (자동)", value=trade_str, inline=False)
            # ── 공통 전략 설명 필드 ───────────────────────────────────
            embed.add_field(
                name="📋 전략",
                value=(
                    "**전략A** 추세 돌파 (MA50 상승 + RSI 55~70) — 익절 **6.0%** / 손절 **4.0%** (R:R 1.5:1)\n"
                    "**전략B** 낙폭 반등 (MA50 하락 + RSI < 25) — 익절 **3.0%** / 손절 **2.5%** (R:R 1.2:1)\n"
                    "BTC 상태에 따라 전략A/B 자동 전환 | 메이저 코인 거래 차단"
                ),
                inline=False,
            )
            next_time = get_next_run_time_for_style(self._style)
            embed.set_footer(text=f"⏳ 다음 AI 분석: {next_time} | 01·05·09·13·17·21시 실행 (4h 봉 기준)")

        await interaction.followup.send(embed=embed, ephemeral=True)


# ------------------------------------------------------------------
# AI 종료 View (연착륙 / 즉시 종료 버튼)
# ------------------------------------------------------------------


class AIShutdownView(discord.ui.View):
    """AI 펀드 매니저 종료 방식을 선택하는 View.

    timeout=60초 후 버튼 자동 비활성화.

    Attributes:
        _user_id: 종료 요청 Discord 사용자 ID.
    """

    def __init__(self, user_id: str) -> None:
        super().__init__(timeout=60)
        self._user_id = user_id

    async def _disable_all(self, interaction: discord.Interaction) -> None:
        """버튼 전체를 비활성화하고 원본 메시지를 업데이트한다 (이중 클릭 방지)."""
        for child in self.children:
            child.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except Exception:
            pass

    @discord.ui.button(
        label="🟡 연착륙 (마저 팔고 종료)",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def graceful_stop(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """신규 매수를 즉시 중단하고, 기존 포지션은 익절/손절 기준으로 자동 매도되기를 기다린다.

        ai_is_shutting_down=True 로 설정하면 ai_manager 가 다음 사이클부터
        analyze_market 및 _buy_new_coins 를 완전히 건너뛰고,
        모든 포지션이 청산되면 ai_mode_enabled=False 로 자동 전환한다.
        """
        await interaction.response.defer(ephemeral=True)
        await self._disable_all(interaction)

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == self._user_id))
            db_user = result.scalar_one_or_none()
            if db_user is None:
                await interaction.followup.send("❌ 유저 정보를 찾을 수 없습니다.", ephemeral=True)
                return
            db_user.ai_is_shutting_down = True
            await db.commit()

        logger.info("AI 연착륙 시작: user_id=%s", self._user_id)
        embed = discord.Embed(
            title="🟡 연착륙 시작",
            description=(
                "신규 매수를 **즉시 중단**했습니다.\n\n"
                "보유 중인 코인이 모두 매도되면 AI가 완전히 종료됩니다.\n"
                "기존 포지션은 설정한 익절/손절 기준으로 자동 매도됩니다.\n"
                "AI 리포트는 계속 전송되며, 모든 포지션 청산 시 완료 알림이 옵니다."
            ),
            color=discord.Color.yellow(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="🔴 즉시 종료 (전부 시장가 매도)",
        style=discord.ButtonStyle.danger,
        row=0,
    )
    async def emergency_stop(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """현재 보유 중인 실전 AI 포지션을 즉시 시장가로 전량 매도한다.

        매수 대기 중인 워커도 취소(DB 초기화)한다.
        ai_mode_enabled=False, ai_is_shutting_down=False 로 완전 비활성화.
        """
        await interaction.response.defer(ephemeral=True)
        await self._disable_all(interaction)

        registry = WorkerRegistry.get()
        # 반복 중 딕셔너리 변경 방지를 위해 먼저 복사·필터링
        real_ai_workers = [
            w for w in list(registry._workers.values())
            if w.user_id == self._user_id and not w.is_paper_trading
        ]

        sold_symbols: list[str] = []
        failed_symbols: list[str] = []

        for worker in real_ai_workers:
            try:
                if worker._position is not None:
                    # 포지션 보유 중 → 즉시 시장가 청산
                    ok = await worker.force_sell("🔴 AI 즉시 종료")
                    if ok:
                        registry._workers.pop(worker.setting_id, None)
                        sold_symbols.append(worker.symbol)
                        logger.info(
                            "AI 즉시 청산 완료: user_id=%s symbol=%s",
                            self._user_id, worker.symbol,
                        )
                    else:
                        failed_symbols.append(worker.symbol)
                        logger.warning(
                            "AI 즉시 청산 실패 (force_sell=False): user_id=%s symbol=%s",
                            self._user_id, worker.symbol,
                        )
                else:
                    # 아직 매수 대기 중인 워커 → 취소 + DB 초기화
                    await registry.unregister(worker.setting_id)
                    sold_symbols.append(f"{worker.symbol} (매수 대기 취소)")
                    logger.info(
                        "AI 매수 대기 워커 취소: user_id=%s symbol=%s",
                        self._user_id, worker.symbol,
                    )
            except Exception as exc:
                failed_symbols.append(worker.symbol)
                logger.error(
                    "AI 즉시 청산 오류: user_id=%s symbol=%s err=%s",
                    self._user_id, worker.symbol, exc,
                )

        # DB: AI 모드 완전 비활성화
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == self._user_id))
            db_user = result.scalar_one_or_none()
            if db_user:
                db_user.ai_mode_enabled = False
                db_user.ai_is_shutting_down = False
                await db.commit()

        logger.info("AI 즉시 종료 완료: user_id=%s", self._user_id)

        lines: list[str] = ["AI 실전 자동 매매가 **즉시 종료**되었습니다."]
        if sold_symbols:
            lines.append(f"✅ 청산·취소: {', '.join(sold_symbols)}")
        if failed_symbols:
            lines.append(f"⚠️ 청산 실패 (업비트에서 직접 확인): {', '.join(failed_symbols)}")
        if not real_ai_workers:
            lines.append("실행 중인 AI 포지션이 없었습니다.")

        embed = discord.Embed(
            title="🔴 AI 즉시 종료 완료",
            description="\n".join(lines),
            color=discord.Color.red(),
        )
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
        current_style = getattr(user, "ai_trade_style", "SNIPER")
        style_label = (
            "🔥 야수 모드 (BEAST, 70%)"
            if current_style in ("BEAST", "SCALPING")
            else "🛡️ 스나이퍼 모드 (SNIPER, 20%)"
        )
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

    @app_commands.command(
        name="ai종료",
        description="실전 AI 펀드 매니저를 종료합니다. (연착륙 또는 즉시 강제 종료)",
    )
    async def ai_shutdown_command(self, interaction: discord.Interaction) -> None:
        """AI 종료 방식을 선택하는 View를 표시한다.

        - VIP + ai_mode_enabled 상태인 유저만 사용 가능.
        - 연착륙: 신규 매수 중단, 기존 포지션은 익절/손절 기준 자동 매도 후 자동 비활성화.
        - 즉시 종료: 보유 포지션 전량 시장가 매도 후 AI 즉시 비활성화.
        """
        user_id = str(interaction.user.id)

        async with AsyncSessionLocal() as db:
            user_result = await db.execute(select(User).where(User.user_id == user_id))
            user = user_result.scalar_one_or_none()

            if user is None or user.subscription_tier != SubscriptionTier.VIP:
                await interaction.response.send_message(
                    embed=_make_vip_required_embed(), ephemeral=True
                )
                return

            if not user.ai_mode_enabled:
                await interaction.response.send_message(
                    "ℹ️ AI 실전 자동 매매가 현재 **비활성화** 상태입니다.\n"
                    "`/ai실전` 에서 먼저 AI 모드를 ON으로 설정해 주세요.",
                    ephemeral=True,
                )
                return

            # 현재 실행 중인 실전 AI 포지션 수 조회
            pos_result = await db.execute(
                select(BotSetting).where(
                    BotSetting.user_id == user_id,
                    BotSetting.is_running.is_(True),
                    BotSetting.is_ai_managed.is_(True),
                    BotSetting.is_paper_trading.is_(False),
                )
            )
            ai_positions = pos_result.scalars().all()

        is_shutting_down = bool(getattr(user, "ai_is_shutting_down", False))
        position_count = len(ai_positions)

        status_lines = [f"현재 실전 AI 포지션: **{position_count}개** 운용 중"]
        if is_shutting_down:
            status_lines.append("⚠️ 현재 **연착륙 진행 중**입니다.")

        embed = discord.Embed(
            title="⚠️ AI 펀드 매니저 종료",
            description=(
                "\n".join(status_lines) + "\n\n"
                "종료 방식을 선택하세요.\n\n"
                "🟡 **연착륙** — 신규 매수만 중단. 기존 포지션은 익절/손절 기준으로 자동 매도됩니다.\n"
                "🔴 **즉시 종료** — 보유 포지션을 **즉시 전량 시장가 매도** 후 AI를 비활성화합니다."
            ),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="⏱️ 이 메시지는 60초 후 만료됩니다.")

        view = AIShutdownView(user_id=user_id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
