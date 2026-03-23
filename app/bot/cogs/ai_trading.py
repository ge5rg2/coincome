"""
/ai실전 슬래시 커맨드: VIP 전용 AI 실전 자동 매매 펀드 매니저 기능 설정 (V2).

처리 흐름 (2단계 UI):
  [Step 1] VIP 등급 검증 → 미달 시 업그레이드 유도 Embed 반환
  [Step 2] VIP 확인 → AISettingView 표시 (AI 모드 ON/OFF, 엔진 선택 드롭다운)
  [Step 3] "다음 →" 버튼 클릭 → 엔진에 따라 다른 Modal(팝업창) 표시
           SWING    → SwingSettingsModal   (스윙 예산·비중·최대종목) — 단독 모드
           SCALPING → ScalpSettingsModal   (스캘핑 예산·비중·최대종목) — 단독 모드
           MAJOR    → MajorSettingsModal   (메이저 예산·비중·최대종목) — 단독 모드
           ALL      → AllEnginesModal      (3엔진 예산+공통비중+최대종목 5필드)
  [Step 4] 유저 제출 → DB 업데이트 (타 엔진 강제 OFF 포함) → 완료 Embed 반환

Discord API 제약:
  Modal 내부에는 TextInput 만 허용 (Select 불가, 최대 5개).
  ALL 모드는 3개 예산 필드 + 공통비중 + 최대종목 = 5개로 정확히 부합.
  단독 모드 선택 시 다른 엔진의 예산·비중은 0으로 자동 초기화된다.
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
from app.services.exchange import ExchangeService
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
            "• **📊 알트 스윙 (4h)** — 추세 돌파 + 낙폭 반등 자동 전환 (6회/일)\n"
            "• **⚡ 알트 스캘핑 (1h)** — 단기 모멘텀 포착 (24회/일)\n"
            "• **🔥 동시 가동** — 알트 스윙+알트 스캘핑 독립 운용, 예산·비중 각각 설정\n"
            "• **🏦 MAJOR 트렌드** — BTC·ETH 등 메이저 전용 Trend Catcher\n"
            "• 엔진별 운용 예산 및 1회 진입 비중 자유 설정\n"
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


class EngineSelect(discord.ui.Select):
    """가동 엔진 선택 드롭다운 (SWING / SCALPING / MAJOR / ALL).

    V2 단독/통합 엔진 모델:
    - SWING/SCALPING/MAJOR: 단독 모드 — 선택 시 나머지 엔진 자동 OFF
    - ALL: 통합 모드 — 3개 엔진 동시 가동
    각 단독 모드는 다른 엔진의 예산·비중을 0으로 초기화하여 상태 혼합을 방지한다.
    """

    def __init__(self, current_engine: str) -> None:
        options = [
            discord.SelectOption(
                label="[단독] 알트 스윙 엔진",
                value="SWING",
                description="4h 봉 | 추세돌파+낙폭반등 양방향 | 다른 엔진은 자동 OFF",
                default=current_engine == "SWING",
            ),
            discord.SelectOption(
                label="[단독] 알트 스캘핑 엔진",
                value="SCALPING",
                description="1h 봉 | 상승 모멘텀 단타 | 다른 엔진은 자동 OFF",
                default=current_engine == "SCALPING",
            ),
            discord.SelectOption(
                label="[단독] 메이저 트렌드 엔진",
                value="MAJOR",
                description="4h 봉 | BTC·ETH 등 메이저 8종 돌파 추세 | 다른 엔진은 자동 OFF",
                default=current_engine == "MAJOR",
            ),
            discord.SelectOption(
                label="[통합] 3엔진 동시 가동",
                value="ALL",
                description="알트 스윙 + 알트 스캘핑 + 메이저 트렌드 동시 가동",
                default=current_engine == "ALL",
            ),
        ]
        super().__init__(placeholder="가동 엔진 모드를 선택하세요", options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.view.engine_value = self.values[0]
        await interaction.response.defer()


# ------------------------------------------------------------------
# Step 1: View (드롭다운 + "다음" 버튼)
# ------------------------------------------------------------------

class AISettingView(discord.ui.View):
    """1단계: AI 모드·엔진을 드롭다운으로 선택하는 View.

    "다음 →" 버튼 클릭 시 선택된 엔진에 맞는 Modal(2단계)을 표시한다.
    timeout=180 초 (이후 버튼 비활성화).

    Attributes:
        mode_value:   현재 선택된 AI 모드 ("ON" / "OFF").
        engine_value: 현재 선택된 엔진 ("SWING" / "SCALPING" / "MAJOR" / "ALL").
    """

    def __init__(self, user: User) -> None:
        super().__init__(timeout=180)
        self._user = user
        current_engine = (getattr(user, "ai_engine_mode", None) or "SWING").upper()
        if current_engine not in ("SWING", "SCALPING", "MAJOR", "ALL", "BOTH"):
            current_engine = "SWING"
        # Migrate legacy BOTH → ALL
        if current_engine == "BOTH":
            current_engine = "ALL"
        self.mode_value: str = "ON" if user.ai_mode_enabled else "OFF"
        self.engine_value: str = current_engine

        self.add_item(ModeSelect(current_enabled=user.ai_mode_enabled))
        self.add_item(EngineSelect(current_engine=current_engine))

    @discord.ui.button(label="다음 →", style=discord.ButtonStyle.primary, emoji="⚙️", row=2)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """선택된 엔진에 맞는 Modal을 표시한다. OFF 선택 시 즉시 DB 저장 후 종료."""
        user = self._user
        engine = self.engine_value

        # ── OFF 패스트트랙: Modal 없이 즉시 전체 비활성화 ─────────────────
        if self.mode_value == "OFF":
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(User).where(User.user_id == user.user_id))
                db_user = result.scalar_one_or_none()
                if db_user:
                    db_user.ai_mode_enabled = False
                    db_user.is_major_enabled = False
                    await db.commit()
            logger.info("AI 실전 자동매매 전체 비활성화: user_id=%s", user.user_id)
            total_budget = (
                int(getattr(user, "ai_swing_budget_krw", 0) or 0)
                + int(getattr(user, "ai_scalp_budget_krw", 0) or 0)
                + int(getattr(user, "major_budget", 0) or 0)
            )
            embed = _make_disabled_embed(user.ai_max_coins, total_budget)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if engine == "SWING":
            modal = SwingSettingsModal(
                user_id=user.user_id,
                mode=self.mode_value,
                current_budget=int(getattr(user, "ai_swing_budget_krw", 1_000_000) or 1_000_000),
                current_weight=int(getattr(user, "ai_swing_weight_pct", 20) or 20),
                current_max_coins=user.ai_max_coins,
            )
        elif engine == "SCALPING":
            modal = ScalpSettingsModal(
                user_id=user.user_id,
                mode=self.mode_value,
                current_budget=int(getattr(user, "ai_scalp_budget_krw", 1_000_000) or 1_000_000),
                current_weight=int(getattr(user, "ai_scalp_weight_pct", 20) or 20),
                current_max_coins=user.ai_max_coins,
            )
        elif engine == "MAJOR":
            modal = MajorSettingsModal(
                user_id=user.user_id,
                mode=self.mode_value,
                current_budget=int(getattr(user, "major_budget", 1_000_000) or 1_000_000),
                current_ratio=int(getattr(user, "major_trade_ratio", 10) or 10),
                current_max_coins=user.ai_max_coins,
            )
        else:  # ALL
            modal = AllEnginesModal(
                user_id=user.user_id,
                mode=self.mode_value,
                current_swing_budget=int(getattr(user, "ai_swing_budget_krw", 1_000_000) or 1_000_000),
                current_scalp_budget=int(getattr(user, "ai_scalp_budget_krw", 1_000_000) or 1_000_000),
                current_major_budget=int(getattr(user, "major_budget", 1_000_000) or 1_000_000),
                current_ratio=int(getattr(user, "ai_swing_weight_pct", 20) or 20),
                current_max_coins=user.ai_max_coins,
            )

        await interaction.response.send_modal(modal)


# ------------------------------------------------------------------
# 공통 유효성 검사 헬퍼
# ------------------------------------------------------------------

def _validate_budget(raw: str) -> tuple[int | None, str | None]:
    """예산 문자열을 파싱하고 범위를 검증한다.

    Returns:
        (int 값, None) 또는 (None, 오류 메시지).
    """
    try:
        v = int(raw.replace(",", "").strip())
    except ValueError:
        return None, "❌ 예산은 숫자로 입력해 주세요."
    if not 1_000_000 <= v <= 100_000_000:
        return None, "❌ 예산은 **최소 1,000,000 ~ 최대 100,000,000 KRW** 사이로 입력해 주세요."
    return v, None


def _validate_weight(raw: str) -> tuple[int | None, str | None]:
    """비중 문자열을 파싱하고 범위를 검증한다.

    Returns:
        (int 값, None) 또는 (None, 오류 메시지).
    """
    try:
        v = int(raw.replace("%", "").strip())
    except ValueError:
        return None, "❌ 비중은 숫자(정수)로 입력해 주세요."
    if not 10 <= v <= 100:
        return None, "❌ 비중은 **10 ~ 100%** 사이로 입력해 주세요."
    return v, None


def _validate_max_coins(raw: str) -> tuple[int | None, str | None]:
    """최대 종목 수 문자열을 파싱하고 범위를 검증한다."""
    try:
        v = int(raw.strip())
    except ValueError:
        return None, "❌ 최대 보유 종목 수는 숫자로 입력해 주세요."
    if not 1 <= v <= 10:
        return None, "❌ 최대 보유 종목 수는 **1 ~ 10** 사이로 입력해 주세요."
    return v, None


# ------------------------------------------------------------------
# Step 2: SWING 전용 Modal
# ------------------------------------------------------------------

class SwingSettingsModal(discord.ui.Modal, title="📊 [단독] 알트 스윙 엔진 설정"):
    """알트 스윙 단독 모드: 제출 시 스캘핑·메이저 엔진을 자동으로 OFF합니다."""

    def __init__(
        self,
        user_id: str,
        mode: str,
        current_budget: int,
        current_weight: int,
        current_max_coins: int,
    ) -> None:
        super().__init__()
        self._user_id = user_id
        self._mode = mode

        self.budget = discord.ui.TextInput(
            label="알트 스윙 운용 예산 (KRW)",
            placeholder="예: 3000000  |  최소 1,000,000 ~ 최대 100,000,000 원",
            min_length=7,
            max_length=12,
            default=str(current_budget),
        )
        self.weight = discord.ui.TextInput(
            label="알트 스윙 1회 진입 비중 (%)",
            placeholder="예: 20  |  20%는 안전 지향, 70% 이상은 공격적 성향입니다.",
            min_length=2,
            max_length=3,
            default=str(current_weight),
        )
        self.max_coins = discord.ui.TextInput(
            label="최대 동시 보유 종목 수",
            placeholder="예: 3  (1 ~ 10)",
            min_length=1,
            max_length=2,
            default=str(current_max_coins),
        )
        self.add_item(self.budget)
        self.add_item(self.weight)
        self.add_item(self.max_coins)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        budget, err = _validate_budget(self.budget.value)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        weight, err = _validate_weight(self.weight.value)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        max_coins, err = _validate_max_coins(self.max_coins.value)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        enabled = self._mode == "ON"
        trade_amount = max(5_000, int(budget * weight / 100))

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == self._user_id))
            user = result.scalar_one_or_none()
            if user is None:
                await interaction.followup.send("❌ 유저 정보를 찾을 수 없습니다.", ephemeral=True)
                return

            # 잔고 검증
            actual_krw: float | None = None
            try:
                if user.upbit_access_key and user.upbit_secret_key:
                    _ex = ExchangeService(
                        access_key=user.upbit_access_key,
                        secret_key=user.upbit_secret_key,
                    )
                    actual_krw = await _ex.fetch_krw_balance()
            except Exception as exc:
                logger.warning("KRW 잔고 조회 실패 (검증 스킵): user_id=%s err=%s", self._user_id, exc)

            if actual_krw is not None and budget > actual_krw:
                await interaction.followup.send(
                    f"❌ 설정 예산(**{budget:,}원**)이 실제 잔고(**{actual_krw:,.0f}원**)보다 큽니다.",
                    ephemeral=True,
                )
                return

            # 단독 모드: 스캘핑·메이저 엔진 강제 OFF
            user.ai_mode_enabled = enabled
            user.ai_engine_mode = "SWING"
            user.ai_swing_budget_krw = budget
            user.ai_swing_weight_pct = weight
            user.ai_scalp_budget_krw = 0
            user.ai_scalp_weight_pct = 0
            user.is_major_enabled = False
            user.major_budget = 0
            user.ai_max_coins = max_coins
            user.ai_is_shutting_down = False
            await db.commit()

        logger.info(
            "AI 알트 스윙(단독) 설정: user_id=%s enabled=%s budget=%d weight=%d%% max_coins=%d",
            self._user_id, enabled, budget, weight, max_coins,
        )

        if not enabled:
            embed = _make_disabled_embed(max_coins, budget)
        else:
            next_time = get_next_run_time_for_style("SWING")
            embed = discord.Embed(
                title="📊 [단독] 알트 스윙 엔진 가동",
                description=(
                    "**알트 스윙 엔진만** 활성화되었습니다.\n"
                    "알트 스캘핑·메이저 트렌드 엔진은 자동으로 OFF 되었습니다."
                ),
                color=discord.Color.blue(),
            )
            embed.add_field(name="📊 알트 스윙", value="🟢 ON", inline=True)
            embed.add_field(name="⚡ 알트 스캘핑", value="⏸️ OFF", inline=True)
            embed.add_field(name="🏦 메이저 트렌드", value="⏸️ OFF", inline=True)
            embed.add_field(
                name="💰 설정",
                value=f"예산: **{budget:,} KRW**  |  진입 비중: **{weight}%**\n"
                      f"1회 매수 기준금액: **{trade_amount:,} KRW**  |  최대 종목: **{max_coins}개**",
                inline=False,
            )
            embed.add_field(
                name="📋 전략",
                value=(
                    "전략A 추세 돌파 (RSI 55~70) — 익절 6% / 손절 4%\n"
                    "전략B 낙폭 반등 (RSI < 25) — 익절 3% / 손절 2.5%"
                ),
                inline=False,
            )
            embed.set_footer(text=f"⏳ 다음 AI 분석: {next_time} | 01·05·09·13·17·21시 실행")

        await interaction.followup.send(embed=embed, ephemeral=True)


# ------------------------------------------------------------------
# Step 2: SCALPING 전용 Modal
# ------------------------------------------------------------------

class ScalpSettingsModal(discord.ui.Modal, title="⚡ [단독] 알트 스캘핑 엔진 설정"):
    """알트 스캘핑 단독 모드: 제출 시 스윙·메이저 엔진을 자동으로 OFF합니다."""

    def __init__(
        self,
        user_id: str,
        mode: str,
        current_budget: int,
        current_weight: int,
        current_max_coins: int,
    ) -> None:
        super().__init__()
        self._user_id = user_id
        self._mode = mode

        self.budget = discord.ui.TextInput(
            label="알트 스캘핑 운용 예산 (KRW)",
            placeholder="예: 2000000  |  최소 1,000,000 ~ 최대 100,000,000 원",
            min_length=7,
            max_length=12,
            default=str(current_budget),
        )
        self.weight = discord.ui.TextInput(
            label="알트 스캘핑 1회 진입 비중 (%)",
            placeholder="예: 30  |  20%는 안전 지향, 70% 이상은 공격적 성향입니다.",
            min_length=2,
            max_length=3,
            default=str(current_weight),
        )
        self.max_coins = discord.ui.TextInput(
            label="최대 동시 보유 종목 수",
            placeholder="예: 3  (1 ~ 10)",
            min_length=1,
            max_length=2,
            default=str(current_max_coins),
        )
        self.add_item(self.budget)
        self.add_item(self.weight)
        self.add_item(self.max_coins)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        budget, err = _validate_budget(self.budget.value)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        weight, err = _validate_weight(self.weight.value)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        max_coins, err = _validate_max_coins(self.max_coins.value)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        enabled = self._mode == "ON"
        trade_amount = max(5_000, int(budget * weight / 100))

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == self._user_id))
            user = result.scalar_one_or_none()
            if user is None:
                await interaction.followup.send("❌ 유저 정보를 찾을 수 없습니다.", ephemeral=True)
                return

            actual_krw: float | None = None
            try:
                if user.upbit_access_key and user.upbit_secret_key:
                    _ex = ExchangeService(
                        access_key=user.upbit_access_key,
                        secret_key=user.upbit_secret_key,
                    )
                    actual_krw = await _ex.fetch_krw_balance()
            except Exception as exc:
                logger.warning("KRW 잔고 조회 실패 (검증 스킵): user_id=%s err=%s", self._user_id, exc)

            if actual_krw is not None and budget > actual_krw:
                await interaction.followup.send(
                    f"❌ 설정 예산(**{budget:,}원**)이 실제 잔고(**{actual_krw:,.0f}원**)보다 큽니다.",
                    ephemeral=True,
                )
                return

            # 단독 모드: 스윙·메이저 엔진 강제 OFF
            user.ai_mode_enabled = enabled
            user.ai_engine_mode = "SCALPING"
            user.ai_scalp_budget_krw = budget
            user.ai_scalp_weight_pct = weight
            user.ai_swing_budget_krw = 0
            user.ai_swing_weight_pct = 0
            user.is_major_enabled = False
            user.major_budget = 0
            user.ai_max_coins = max_coins
            user.ai_is_shutting_down = False
            await db.commit()

        logger.info(
            "AI 알트 스캘핑(단독) 설정: user_id=%s enabled=%s budget=%d weight=%d%% max_coins=%d",
            self._user_id, enabled, budget, weight, max_coins,
        )

        if not enabled:
            embed = _make_disabled_embed(max_coins, budget)
        else:
            next_time = get_next_run_time_for_style("SCALPING")
            embed = discord.Embed(
                title="⚡ [단독] 알트 스캘핑 엔진 가동",
                description=(
                    "**알트 스캘핑 엔진만** 활성화되었습니다.\n"
                    "알트 스윙·메이저 트렌드 엔진은 자동으로 OFF 되었습니다."
                ),
                color=discord.Color.orange(),
            )
            embed.add_field(name="📊 알트 스윙", value="⏸️ OFF", inline=True)
            embed.add_field(name="⚡ 알트 스캘핑", value="🟢 ON", inline=True)
            embed.add_field(name="🏦 메이저 트렌드", value="⏸️ OFF", inline=True)
            embed.add_field(
                name="💰 설정",
                value=f"예산: **{budget:,} KRW**  |  진입 비중: **{weight}%**\n"
                      f"1회 매수 기준금액: **{trade_amount:,} KRW**  |  최대 종목: **{max_coins}개**",
                inline=False,
            )
            embed.add_field(
                name="📋 전략",
                value="진입: Close > MA20 AND RSI 60~75\n익절: +2.0% / 손절: -1.5% (R:R 1.33:1)",
                inline=False,
            )
            embed.set_footer(text=f"⏳ 다음 AI 분석: {next_time} | 매시 정각 실행")

        await interaction.followup.send(embed=embed, ephemeral=True)


# ------------------------------------------------------------------
# Step 2: MAJOR 메이저 코인 전용 Modal
# ------------------------------------------------------------------

class MajorSettingsModal(discord.ui.Modal, title="🏦 [단독] 메이저 트렌드 엔진 설정"):
    """메이저 트렌드 단독 모드: 제출 시 알트 스윙·스캘핑 엔진을 자동으로 OFF합니다."""

    def __init__(
        self,
        user_id: str,
        mode: str,
        current_budget: int,
        current_ratio: int,
        current_max_coins: int,
    ) -> None:
        super().__init__()
        self._user_id = user_id
        self._mode = mode

        self.budget = discord.ui.TextInput(
            label="메이저 트렌드 운용 예산 (KRW)",
            placeholder="예: 5000000  |  최소 1,000,000 ~ 최대 100,000,000 원",
            min_length=7,
            max_length=12,
            default=str(current_budget),
        )
        self.ratio = discord.ui.TextInput(
            label="1회 진입 비중 (%)",
            placeholder="예: 10  |  10%는 분산 안전, 50% 이상은 집중 공격형",
            min_length=2,
            max_length=3,
            default=str(current_ratio),
        )
        self.max_coins = discord.ui.TextInput(
            label="최대 동시 보유 종목 수",
            placeholder="예: 3  (1 ~ 10)",
            min_length=1,
            max_length=2,
            default=str(current_max_coins),
        )
        self.add_item(self.budget)
        self.add_item(self.ratio)
        self.add_item(self.max_coins)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        budget, err = _validate_budget(self.budget.value)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        ratio, err = _validate_weight(self.ratio.value)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        max_coins, err = _validate_max_coins(self.max_coins.value)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        enabled = self._mode == "ON"
        trade_amount = max(5_000, int(budget * ratio / 100))

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == self._user_id))
            user = result.scalar_one_or_none()
            if user is None:
                await interaction.followup.send("❌ 유저 정보를 찾을 수 없습니다.", ephemeral=True)
                return

            actual_krw: float | None = None
            try:
                if user.upbit_access_key and user.upbit_secret_key:
                    _ex = ExchangeService(
                        access_key=user.upbit_access_key,
                        secret_key=user.upbit_secret_key,
                    )
                    actual_krw = await _ex.fetch_krw_balance()
            except Exception as exc:
                logger.warning("KRW 잔고 조회 실패 (검증 스킵): user_id=%s err=%s", self._user_id, exc)

            if actual_krw is not None and budget > actual_krw:
                await interaction.followup.send(
                    f"❌ 설정 예산(**{budget:,}원**)이 실제 잔고(**{actual_krw:,.0f}원**)보다 큽니다.",
                    ephemeral=True,
                )
                return

            # 단독 모드: 알트 스윙·스캘핑 강제 OFF
            user.ai_engine_mode = "MAJOR"
            user.ai_mode_enabled = False          # 알트 엔진 완전 비활성
            user.ai_swing_budget_krw = 0
            user.ai_swing_weight_pct = 0
            user.ai_scalp_budget_krw = 0
            user.ai_scalp_weight_pct = 0
            user.is_major_enabled = enabled
            user.major_budget = budget
            user.major_trade_ratio = ratio
            user.ai_max_coins = max_coins
            user.ai_is_shutting_down = False
            await db.commit()

        logger.info(
            "AI 메이저 트렌드(단독) 설정: user_id=%s enabled=%s budget=%d ratio=%d%% max_coins=%d",
            self._user_id, enabled, budget, ratio, max_coins,
        )

        if not enabled:
            embed = discord.Embed(
                title="⏸️ 메이저 트렌드 엔진 비활성화",
                description="메이저 코인 Trend Catcher 신규 매수가 중단됩니다.",
                color=discord.Color.greyple(),
            )
        else:
            next_time = get_next_run_time_for_style("SWING")
            embed = discord.Embed(
                title="🏦 [단독] 메이저 트렌드 엔진 가동",
                description=(
                    "**메이저 트렌드 엔진만** 활성화되었습니다.\n"
                    "알트 스윙·알트 스캘핑 엔진은 자동으로 OFF 되었습니다."
                ),
                color=discord.Color.teal(),
            )
            embed.add_field(name="📊 알트 스윙", value="⏸️ OFF", inline=True)
            embed.add_field(name="⚡ 알트 스캘핑", value="⏸️ OFF", inline=True)
            embed.add_field(name="🏦 메이저 트렌드", value="🟢 ON", inline=True)
            embed.add_field(
                name="💰 설정",
                value=f"예산: **{budget:,} KRW**  |  진입 비중: **{ratio}%**\n"
                      f"1회 매수 기준금액: **{trade_amount:,} KRW**  |  최대 종목: **{max_coins}개**",
                inline=False,
            )
            embed.add_field(
                name="📋 Trend Catcher 전략",
                value=(
                    "3중 필터: Close > EMA200 AND EMA20 > EMA50 AND Close > BB Upper(2σ)\n"
                    "대상: BTC·ETH·XRP·SOL·DOGE·ADA·SUI·PEPE\n"
                    "익절: +4.0% / 손절: -2.0% (R:R 2:1 하드 고정)"
                ),
                inline=False,
            )
            embed.set_footer(text=f"⏳ 다음 AI 분석: {next_time} | 01·05·09·13·17·21시 실행")

        await interaction.followup.send(embed=embed, ephemeral=True)


# ------------------------------------------------------------------
# Step 2: ALL(3엔진 통합) Modal
# ------------------------------------------------------------------

class AllEnginesModal(discord.ui.Modal, title="🔥 [통합] 3엔진 동시 가동 설정"):
    """3개 엔진 동시 가동 모달 — 5필드로 모든 엔진을 한 번에 설정합니다."""

    def __init__(
        self,
        user_id: str,
        mode: str,
        current_swing_budget: int,
        current_scalp_budget: int,
        current_major_budget: int,
        current_ratio: int,
        current_max_coins: int,
    ) -> None:
        super().__init__()
        self._user_id = user_id
        self._mode = mode

        self.swing_budget = discord.ui.TextInput(
            label="📊 알트 스윙 예산 (KRW)",
            placeholder="예: 3000000  |  최소 1,000,000 원",
            min_length=7,
            max_length=12,
            default=str(current_swing_budget),
        )
        self.scalp_budget = discord.ui.TextInput(
            label="⚡ 알트 스캘핑 예산 (KRW)",
            placeholder="예: 2000000  |  최소 1,000,000 원",
            min_length=7,
            max_length=12,
            default=str(current_scalp_budget),
        )
        self.major_budget = discord.ui.TextInput(
            label="🏦 메이저 트렌드 예산 (KRW)",
            placeholder="예: 5000000  |  최소 1,000,000 원",
            min_length=7,
            max_length=12,
            default=str(current_major_budget),
        )
        self.ratio = discord.ui.TextInput(
            label="공통 1회 진입 비중 (%) — 3엔진 동일 적용",
            placeholder="예: 20  |  3개 엔진에 동일하게 적용됩니다.",
            min_length=2,
            max_length=3,
            default=str(current_ratio),
        )
        self.max_coins = discord.ui.TextInput(
            label="최대 동시 보유 종목 수 (전체 통합)",
            placeholder="예: 9  (1 ~ 10, 3엔진 합산)",
            min_length=1,
            max_length=2,
            default=str(current_max_coins),
        )
        self.add_item(self.swing_budget)
        self.add_item(self.scalp_budget)
        self.add_item(self.major_budget)
        self.add_item(self.ratio)
        self.add_item(self.max_coins)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        swing_budget, err = _validate_budget(self.swing_budget.value)
        if err:
            await interaction.followup.send(f"[알트 스윙 예산] {err}", ephemeral=True)
            return

        scalp_budget, err = _validate_budget(self.scalp_budget.value)
        if err:
            await interaction.followup.send(f"[알트 스캘핑 예산] {err}", ephemeral=True)
            return

        major_budget_val, err = _validate_budget(self.major_budget.value)
        if err:
            await interaction.followup.send(f"[메이저 트렌드 예산] {err}", ephemeral=True)
            return

        ratio, err = _validate_weight(self.ratio.value)
        if err:
            await interaction.followup.send(f"[공통 비중] {err}", ephemeral=True)
            return

        max_coins, err = _validate_max_coins(self.max_coins.value)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        enabled = self._mode == "ON"
        total_budget = swing_budget + scalp_budget + major_budget_val
        swing_trade_amt = max(5_000, int(swing_budget * ratio / 100))
        scalp_trade_amt = max(5_000, int(scalp_budget * ratio / 100))
        major_trade_amt = max(5_000, int(major_budget_val * ratio / 100))

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == self._user_id))
            user = result.scalar_one_or_none()
            if user is None:
                await interaction.followup.send("❌ 유저 정보를 찾을 수 없습니다.", ephemeral=True)
                return

            actual_krw: float | None = None
            try:
                if user.upbit_access_key and user.upbit_secret_key:
                    _ex = ExchangeService(
                        access_key=user.upbit_access_key,
                        secret_key=user.upbit_secret_key,
                    )
                    actual_krw = await _ex.fetch_krw_balance()
            except Exception as exc:
                logger.warning("KRW 잔고 조회 실패 (검증 스킵): user_id=%s err=%s", self._user_id, exc)

            if actual_krw is not None and total_budget > actual_krw:
                await interaction.followup.send(
                    f"❌ 총 운용 예산(**{total_budget:,}원**)이 실제 잔고(**{actual_krw:,.0f}원**)보다 큽니다.",
                    ephemeral=True,
                )
                return

            # 3엔진 동시 활성화
            user.ai_mode_enabled = enabled
            user.ai_engine_mode = "ALL"
            user.ai_swing_budget_krw = swing_budget
            user.ai_swing_weight_pct = ratio
            user.ai_scalp_budget_krw = scalp_budget
            user.ai_scalp_weight_pct = ratio
            user.is_major_enabled = enabled
            user.major_budget = major_budget_val
            user.major_trade_ratio = ratio
            user.ai_max_coins = max_coins
            user.ai_is_shutting_down = False
            await db.commit()

        logger.info(
            "AI 3엔진 통합 설정: user_id=%s enabled=%s swing=%d scalp=%d major=%d ratio=%d%% max_coins=%d",
            self._user_id, enabled, swing_budget, scalp_budget, major_budget_val, ratio, max_coins,
        )

        if not enabled:
            embed = _make_disabled_embed(max_coins, total_budget)
        else:
            embed = discord.Embed(
                title="🔥 [통합] 3엔진 동시 가동 활성화",
                description=(
                    "**알트 스윙 + 알트 스캘핑 + 메이저 트렌드** 3개 엔진이 동시에 가동됩니다.\n"
                    "각 엔진의 예산은 분리되어 독립적으로 운용됩니다."
                ),
                color=discord.Color.red(),
            )
            embed.add_field(name="📊 알트 스윙", value="🟢 ON", inline=True)
            embed.add_field(name="⚡ 알트 스캘핑", value="🟢 ON", inline=True)
            embed.add_field(name="🏦 메이저 트렌드", value="🟢 ON", inline=True)
            embed.add_field(
                name="📊 알트 스윙 설정",
                value=f"예산: **{swing_budget:,} KRW**  →  1회: **{swing_trade_amt:,} KRW**",
                inline=False,
            )
            embed.add_field(
                name="⚡ 알트 스캘핑 설정",
                value=f"예산: **{scalp_budget:,} KRW**  →  1회: **{scalp_trade_amt:,} KRW**",
                inline=False,
            )
            embed.add_field(
                name="🏦 메이저 트렌드 설정",
                value=f"예산: **{major_budget_val:,} KRW**  →  1회: **{major_trade_amt:,} KRW**",
                inline=False,
            )
            embed.add_field(
                name="⚙️ 공통 설정",
                value=f"진입 비중: **{ratio}%** (3엔진 동일)  |  최대 종목: **{max_coins}개** (합산)",
                inline=False,
            )
            embed.set_footer(text="스윙·메이저: 01·05·09·13·17·21시 | 스캘핑: 매시 정각")

        await interaction.followup.send(embed=embed, ephemeral=True)


def _make_disabled_embed(max_coins: int, total_budget: int) -> discord.Embed:
    embed = discord.Embed(
        title="⏸️ AI 실전 자동 매매 비활성화",
        description="신규 AI 매수가 중단됩니다. 기존 실행 중인 워커는 계속 동작합니다.",
        color=discord.Color.greyple(),
    )
    embed.add_field(name="AI 모드", value="⏸️ 비활성화", inline=True)
    embed.add_field(name="최대 보유 종목", value=f"{max_coins}개", inline=True)
    embed.add_field(name="설정 예산", value=f"{total_budget:,} KRW", inline=True)
    embed.set_footer(text="AI 자동 매매가 중지되었습니다. 기존 워커는 계속 동작합니다.")
    return embed


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
        """신규 매수를 즉시 중단하고, 기존 포지션은 익절/손절 기준으로 자동 매도되기를 기다린다."""
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
        """현재 보유 중인 실전 AI 포지션을 즉시 시장가로 전량 매도한다."""
        await interaction.response.defer(ephemeral=True)
        await self._disable_all(interaction)

        registry = WorkerRegistry.get()
        real_ai_workers = [
            w for w in list(registry._workers.values())
            if w.user_id == self._user_id and not w.is_paper_trading
        ]

        sold_symbols: list[str] = []
        failed_symbols: list[str] = []

        for worker in real_ai_workers:
            try:
                if worker._position is not None:
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
    """AI 자동 매매 관련 슬래시 커맨드 Cog (V2 — 모듈형 엔진 선택)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="ai실전",
        description="VIP 전용 AI 실전 자동 매매 펀드 매니저를 설정합니다.",
    )
    async def ai_settings_command(self, interaction: discord.Interaction) -> None:
        """VIP 여부를 확인한 뒤 엔진 선택 View(1단계)를 띄운다.

        [VIP 검증] FREE / PRO 등급이면 업그레이드 유도 Embed로 즉시 반환.
        [설정 UI ] 드롭다운(모드·엔진) → "다음 →" 버튼 → Modal(예산·비중) 2단계 흐름.
        """
        user_id = str(interaction.user.id)

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == user_id))
            user = result.scalar_one_or_none()

        if user is None or user.subscription_tier != SubscriptionTier.VIP:
            await interaction.response.send_message(
                embed=_make_vip_required_embed(), ephemeral=True
            )
            return

        engine_mode = (getattr(user, "ai_engine_mode", None) or "SWING").upper()
        if engine_mode == "BOTH":
            engine_mode = "ALL"  # migrate legacy

        swing_budget = int(getattr(user, "ai_swing_budget_krw", 0) or 0)
        swing_weight = int(getattr(user, "ai_swing_weight_pct", 20) or 20)
        scalp_budget = int(getattr(user, "ai_scalp_budget_krw", 0) or 0)
        scalp_weight = int(getattr(user, "ai_scalp_weight_pct", 20) or 20)
        major_budget = int(getattr(user, "major_budget", 0) or 0)
        major_ratio  = int(getattr(user, "major_trade_ratio", 10) or 10)
        is_major_on  = bool(getattr(user, "is_major_enabled", False))
        alt_on       = bool(user.ai_mode_enabled)

        # 전체 AI 상태 판단
        any_on = alt_on or is_major_on
        overall_status = "🟢 활성화" if any_on else "⏸️ 비활성화"

        # 엔진별 ON/OFF
        swing_on  = alt_on and engine_mode in ("SWING", "ALL") and swing_budget > 0
        scalp_on  = alt_on and engine_mode in ("SCALPING", "ALL") and scalp_budget > 0
        major_on  = is_major_on and major_budget > 0

        def _engine_status_line(on: bool, budget: int, ratio: int) -> str:
            if on and budget > 0:
                return f"🟢 ON | 예산: **{budget:,} KRW** (진입 비중 **{ratio}%**)"
            return "⏸️ OFF | 미설정 (가동 중지)"

        embed = discord.Embed(
            title="🤖 AI 실전 자동 매매 설정 대시보드 (V2)",
            description=(
                "아래 드롭다운에서 가동할 **엔진 모드**를 선택하세요.\n"
                "*(단독 모드 선택 시 다른 엔진은 자동으로 OFF 됩니다)*"
            ),
            color=discord.Color.blue() if any_on else discord.Color.greyple(),
        )
        embed.add_field(name="🔹 현재 상태", value=overall_status, inline=True)
        embed.add_field(name="🔹 최대 보유 종목", value=f"**{user.ai_max_coins}개**", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(
            name="⚙️ [엔진 1] 알트 스윙 (4h)",
            value=_engine_status_line(swing_on, swing_budget, swing_weight),
            inline=False,
        )
        embed.add_field(
            name="⚙️ [엔진 2] 알트 스캘핑 (1h)",
            value=_engine_status_line(scalp_on, scalp_budget, scalp_weight),
            inline=False,
        )
        embed.add_field(
            name="⚙️ [엔진 3] 메이저 트렌드 (4h)",
            value=_engine_status_line(major_on, major_budget, major_ratio),
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
        """AI 종료 방식을 선택하는 View를 표시한다."""
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
