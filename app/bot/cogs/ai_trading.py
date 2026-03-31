"""
/ai실전 슬래시 커맨드: PRO/VIP 등급별 AI 실전 자동 매매 펀드 매니저 기능 설정 (V2).

처리 흐름:
  [FREE ] max_active_engines == 0 → 즉시 차단 Embed 반환
  [PRO  ] 알트 엔진 1개 선택 버튼 View (SWING / SCALPING / OFF) 표시
           버튼 클릭 → SwingSettingsModal 또는 ScalpSettingsModal 직접 표시
  [VIP  ] 토글 버튼 View (SWING / SCALPING / MAJOR / OFF + 다음 →) 표시
           1개 선택 → 3필드 Modal, 2개 선택 → 4필드 Modal, 3개 선택 → 5필드 Modal

Discord API 제약:
  Modal 내부에는 TextInput 만 허용 (Select 불가, 최대 5개).
  VIP 3엔진 ALL 모드는 3개 예산 필드 + 공통비중 + 최대종목 = 5개로 정확히 부합.
  단독/복수 모드 선택 시 나머지 엔진의 예산·비중은 0으로 자동 초기화된다.
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
# 등급 안내 Embed
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


def _make_free_blocked_embed() -> discord.Embed:
    """FREE 등급 유저에게 AI 트레이딩 차단을 안내하는 Embed를 반환한다."""
    embed = discord.Embed(
        title="🔒 AI 트레이딩은 PRO 이상에서만 사용 가능합니다.",
        description=(
            "현재 등급: **FREE**  |  수동 매매만 이용 가능합니다.\n\n"
            "AI 펀드 매니저를 사용하려면 **PRO** 또는 **VIP** 구독이 필요합니다."
        ),
        color=discord.Color.red(),
    )
    embed.add_field(
        name="📋 등급별 AI 엔진 제공 현황",
        value=(
            "• **FREE** — AI 트레이딩 사용 불가\n"
            "• **PRO** — 알트 스윙 / 알트 스캘핑 중 **1개** 선택\n"
            "• **VIP** — 알트 스윙 + 알트 스캘핑 + 메이저 트렌드 **모두 사용 가능**"
        ),
        inline=False,
    )
    embed.set_footer(text="/구독 명령어로 업그레이드하세요.")
    return embed


# ------------------------------------------------------------------
# Step 1: PRO 전용 버튼 View (알트 엔진 1개 택 1)
# ------------------------------------------------------------------


class ProEngineSelectView(discord.ui.View):
    """PRO Step 1: 알트 엔진 1개를 버튼으로 선택하는 View.

    SWING / SCALPING 선택 시 즉시 해당 Modal 표시.
    OFF 선택 시 즉시 전체 비활성화.
    MAJOR 엔진 버튼 없음 (PRO 미제공).

    Attributes:
        _user: 요청 유저 User 객체.
    """

    def __init__(self, user: User) -> None:
        super().__init__(timeout=180)
        self._user = user

        # 현재 활성 엔진 판별 (버튼 강조용)
        current_engine = (getattr(user, "ai_engine_mode", None) or "SWING").upper()
        if current_engine == "BOTH":
            current_engine = "ALL"
        is_on = bool(user.ai_mode_enabled)

        swing_style = (
            discord.ButtonStyle.primary
            if is_on and current_engine in ("SWING",)
            else discord.ButtonStyle.secondary
        )
        scalp_style = (
            discord.ButtonStyle.primary
            if is_on and current_engine in ("SCALPING",)
            else discord.ButtonStyle.secondary
        )

        self._swing_btn = discord.ui.Button(
            label="📈 알트 스윙",
            style=swing_style,
            row=0,
        )
        self._scalp_btn = discord.ui.Button(
            label="⚡ 알트 스캘핑",
            style=scalp_style,
            row=0,
        )
        self._off_btn = discord.ui.Button(
            label="🔴 OFF (전체 중지)",
            style=discord.ButtonStyle.danger,
            row=0,
        )

        self._swing_btn.callback = self._on_swing
        self._scalp_btn.callback = self._on_scalp
        self._off_btn.callback = self._on_off

        self.add_item(self._swing_btn)
        self.add_item(self._scalp_btn)
        self.add_item(self._off_btn)

    async def _on_swing(self, interaction: discord.Interaction) -> None:
        """알트 스윙 버튼 클릭 — SwingSettingsModal 표시."""
        user = self._user
        modal = SwingSettingsModal(
            user_id=user.user_id,
            mode="ON",
            current_budget=int(getattr(user, "ai_swing_budget_krw", 500_000) or 500_000),
            current_weight=int(getattr(user, "ai_swing_weight_pct", 20) or 20),
            current_max_coins=user.ai_max_coins,
        )
        await interaction.response.send_modal(modal)

    async def _on_scalp(self, interaction: discord.Interaction) -> None:
        """알트 스캘핑 버튼 클릭 — ScalpSettingsModal 표시."""
        user = self._user
        modal = ScalpSettingsModal(
            user_id=user.user_id,
            mode="ON",
            current_budget=int(getattr(user, "ai_scalp_budget_krw", 500_000) or 500_000),
            current_weight=int(getattr(user, "ai_scalp_weight_pct", 20) or 20),
            current_max_coins=user.ai_max_coins,
        )
        await interaction.response.send_modal(modal)

    async def _on_off(self, interaction: discord.Interaction) -> None:
        """OFF 버튼 클릭 — 즉시 전체 비활성화."""
        user = self._user
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == user.user_id))
            db_user = result.scalar_one_or_none()
            if db_user:
                db_user.ai_mode_enabled = False
                db_user.is_major_enabled = False
                await db.commit()
        logger.info("PRO AI 실전 전체 비활성화: user_id=%s", user.user_id)
        total_budget = (
            int(getattr(user, "ai_swing_budget_krw", 0) or 0)
            + int(getattr(user, "ai_scalp_budget_krw", 0) or 0)
        )
        embed = _make_disabled_embed(user.ai_max_coins, total_budget)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ------------------------------------------------------------------
# Step 1: VIP 전용 토글 버튼 View (복수 선택)
# ------------------------------------------------------------------


class VipEngineToggleView(discord.ui.View):
    """VIP Step 1: 엔진을 토글 버튼으로 복수 선택하는 View.

    SWING / SCALPING / MAJOR 버튼을 클릭할 때마다 선택/해제 토글.
    OFF 버튼 클릭 시 즉시 전체 비활성화.
    [✅ 다음 →] 버튼 클릭 시 선택 엔진 수에 따른 VipDynamicModal 표시.

    Attributes:
        _user:     요청 유저 User 객체.
        _selected: 현재 선택된 엔진 집합 (예: {"SWING", "MAJOR"}).
    """

    def __init__(self, user: User) -> None:
        super().__init__(timeout=180)
        self._user = user

        # 현재 상태로 초기 선택 집합 구성
        engine_mode = (getattr(user, "ai_engine_mode", None) or "SWING").upper()
        if engine_mode == "BOTH":
            engine_mode = "ALL"
        is_on = bool(user.ai_mode_enabled)
        is_major = bool(getattr(user, "is_major_enabled", False))

        initial: set[str] = set()
        if is_on:
            if engine_mode in ("SWING", "ALL"):
                initial.add("SWING")
            if engine_mode in ("SCALPING", "ALL"):
                initial.add("SCALPING")
        if is_major:
            initial.add("MAJOR")
        self._selected: set[str] = initial

        self._swing_btn = discord.ui.Button(
            label="📈 알트 스윙",
            style=discord.ButtonStyle.primary if "SWING" in initial else discord.ButtonStyle.secondary,
            row=0,
        )
        self._scalp_btn = discord.ui.Button(
            label="⚡ 알트 스캘핑",
            style=discord.ButtonStyle.primary if "SCALPING" in initial else discord.ButtonStyle.secondary,
            row=0,
        )
        self._major_btn = discord.ui.Button(
            label="🏔️ 메이저 트렌드",
            style=discord.ButtonStyle.primary if "MAJOR" in initial else discord.ButtonStyle.secondary,
            row=0,
        )
        self._off_btn = discord.ui.Button(
            label="🔴 OFF (전체 중지)",
            style=discord.ButtonStyle.danger,
            row=0,
        )
        self._next_btn = discord.ui.Button(
            label="✅ 다음 →",
            style=discord.ButtonStyle.success,
            row=1,
        )

        self._swing_btn.callback = self._toggle_swing
        self._scalp_btn.callback = self._toggle_scalp
        self._major_btn.callback = self._toggle_major
        self._off_btn.callback = self._on_off
        self._next_btn.callback = self._on_next

        self.add_item(self._swing_btn)
        self.add_item(self._scalp_btn)
        self.add_item(self._major_btn)
        self.add_item(self._off_btn)
        self.add_item(self._next_btn)

    def _refresh_styles(self) -> None:
        """선택 상태에 따라 버튼 스타일을 갱신한다."""
        self._swing_btn.style = (
            discord.ButtonStyle.primary if "SWING" in self._selected else discord.ButtonStyle.secondary
        )
        self._scalp_btn.style = (
            discord.ButtonStyle.primary if "SCALPING" in self._selected else discord.ButtonStyle.secondary
        )
        self._major_btn.style = (
            discord.ButtonStyle.primary if "MAJOR" in self._selected else discord.ButtonStyle.secondary
        )

    async def _toggle_swing(self, interaction: discord.Interaction) -> None:
        """알트 스윙 토글."""
        if "SWING" in self._selected:
            self._selected.discard("SWING")
        else:
            self._selected.add("SWING")
        self._refresh_styles()
        await interaction.response.edit_message(view=self)

    async def _toggle_scalp(self, interaction: discord.Interaction) -> None:
        """알트 스캘핑 토글."""
        if "SCALPING" in self._selected:
            self._selected.discard("SCALPING")
        else:
            self._selected.add("SCALPING")
        self._refresh_styles()
        await interaction.response.edit_message(view=self)

    async def _toggle_major(self, interaction: discord.Interaction) -> None:
        """메이저 트렌드 토글."""
        if "MAJOR" in self._selected:
            self._selected.discard("MAJOR")
        else:
            self._selected.add("MAJOR")
        self._refresh_styles()
        await interaction.response.edit_message(view=self)

    async def _on_off(self, interaction: discord.Interaction) -> None:
        """OFF 버튼 — 즉시 전체 비활성화."""
        user = self._user
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == user.user_id))
            db_user = result.scalar_one_or_none()
            if db_user:
                db_user.ai_mode_enabled = False
                db_user.is_major_enabled = False
                await db.commit()
        logger.info("VIP AI 실전 전체 비활성화: user_id=%s", user.user_id)
        total_budget = (
            int(getattr(user, "ai_swing_budget_krw", 0) or 0)
            + int(getattr(user, "ai_scalp_budget_krw", 0) or 0)
            + int(getattr(user, "major_budget", 0) or 0)
        )
        embed = _make_disabled_embed(user.ai_max_coins, total_budget)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        """다음 버튼 — 선택 엔진 수에 따라 VipDynamicModal 표시."""
        if not self._selected:
            await interaction.response.send_message(
                "⚠️ 최소 1개 이상 엔진을 선택해 주세요.", ephemeral=True
            )
            return

        user = self._user
        selected_list = sorted(self._selected)  # 일관된 순서 보장
        modal = VipDynamicModal(
            user_id=user.user_id,
            selected_engines=selected_list,
            user=user,
        )
        await interaction.response.send_modal(modal)


# ------------------------------------------------------------------
# Step 2: VIP 동적 Modal (선택 엔진 수에 따라 필드 수 변동)
# ------------------------------------------------------------------


class VipDynamicModal(discord.ui.Modal):
    """VIP Step 2: 선택된 엔진 수에 따라 동적으로 필드가 구성되는 Modal.

    1개 선택: [예산 1개] + [비중] + [최대종목] = 3필드
    2개 선택: [예산A] + [예산B] + [공통비중] + [최대종목] = 4필드
    3개 선택: [예산A] + [예산B] + [예산C] + [공통비중] + [최대종목] = 5필드

    Args:
        user_id:          Discord 사용자 ID.
        selected_engines: 선택된 엔진 목록 (예: ["SWING", "MAJOR"]).
        user:             User 객체 (현재 설정 기본값 참조용).
    """

    _ENGINE_LABELS: dict[str, str] = {
        "SWING": "📈 알트 스윙 운용 예산 (KRW)",
        "SCALPING": "⚡ 알트 스캘핑 운용 예산 (KRW)",
        "MAJOR": "🏔️ 메이저 트렌드 운용 예산 (KRW)",
    }
    _ENGINE_BUDGET_ATTR: dict[str, str] = {
        "SWING": "ai_swing_budget_krw",
        "SCALPING": "ai_scalp_budget_krw",
        "MAJOR": "major_budget",
    }

    def __init__(
        self,
        user_id: str,
        selected_engines: list[str],
        user: User,
    ) -> None:
        # Modal 제목 구성
        if len(selected_engines) == 1:
            engine_name = {"SWING": "알트 스윙", "SCALPING": "알트 스캘핑", "MAJOR": "메이저 트렌드"}.get(
                selected_engines[0], selected_engines[0]
            )
            title = f"⚙️ {engine_name} 설정"
        elif len(selected_engines) == 2:
            title = "⚙️ 2엔진 동시 가동 설정"
        else:
            title = "⚙️ 3엔진 동시 가동 설정"

        super().__init__(title=title)
        self._user_id = user_id
        self._selected_engines = selected_engines

        # 엔진별 예산 필드 동적 추가
        self._budget_inputs: list[discord.ui.TextInput] = []
        for engine in selected_engines:
            attr = self._ENGINE_BUDGET_ATTR.get(engine, "ai_swing_budget_krw")
            current_val = int(getattr(user, attr, 500_000) or 500_000)
            budget_input = discord.ui.TextInput(
                label=self._ENGINE_LABELS.get(engine, f"{engine} 운용 예산 (KRW)"),
                placeholder="예: 500000  |  최소 50,000 ~ 최대 10,000,000 원",
                min_length=5,
                max_length=10,
                default=str(current_val),
            )
            self._budget_inputs.append(budget_input)
            self.add_item(budget_input)

        # 공통 비중 필드
        current_weight = int(getattr(user, "ai_swing_weight_pct", 20) or 20)
        self.weight = discord.ui.TextInput(
            label="공통 1회 진입 비중 (%) — 선택 엔진 동일 적용",
            placeholder="예: 20  |  10 ~ 100%",
            min_length=2,
            max_length=3,
            default=str(current_weight),
        )
        self.add_item(self.weight)

        # 최대 종목 수 필드
        current_max = int(getattr(user, "ai_max_coins", 3) or 3)
        self.max_coins = discord.ui.TextInput(
            label="최대 동시 보유 종목 수",
            placeholder="예: 3  (1 ~ 10)",
            min_length=1,
            max_length=2,
            default=str(current_max),
        )
        self.add_item(self.max_coins)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """제출 처리: 입력값 검증 → DB 업데이트 → 완료 Embed 반환."""
        await interaction.response.defer(ephemeral=True)

        # 예산 검증
        budgets: dict[str, int] = {}
        for i, engine in enumerate(self._selected_engines):
            val, err = _validate_budget_range(self._budget_inputs[i].value, 50_000, 10_000_000)
            if err:
                engine_label = {"SWING": "알트 스윙", "SCALPING": "알트 스캘핑", "MAJOR": "메이저 트렌드"}.get(engine, engine)
                await interaction.followup.send(f"[{engine_label} 예산] {err}", ephemeral=True)
                return
            budgets[engine] = val  # type: ignore[assignment]

        # 비중 검증
        weight, err = _validate_weight(self.weight.value)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        # 최대 종목 수 검증
        max_coins, err = _validate_max_coins(self.max_coins.value)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        # 잔고 검증
        total_budget = sum(budgets.values())
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

            # 선택 조합별 DB 업데이트
            sel = set(self._selected_engines)
            swing_budget = budgets.get("SWING", 0)
            scalp_budget = budgets.get("SCALPING", 0)
            major_budget_val = budgets.get("MAJOR", 0)

            has_swing = "SWING" in sel
            has_scalp = "SCALPING" in sel
            has_major = "MAJOR" in sel

            # 알트 엔진 모드 결정
            if has_swing and has_scalp:
                alt_engine_mode = "ALL"
            elif has_swing:
                alt_engine_mode = "SWING"
            elif has_scalp:
                alt_engine_mode = "SCALPING"
            else:
                # MAJOR 단독
                alt_engine_mode = "MAJOR"

            user.ai_mode_enabled = has_swing or has_scalp
            user.ai_engine_mode = alt_engine_mode
            user.ai_swing_budget_krw = swing_budget
            user.ai_swing_weight_pct = weight if has_swing else 0
            user.ai_scalp_budget_krw = scalp_budget
            user.ai_scalp_weight_pct = weight if has_scalp else 0
            user.is_major_enabled = has_major
            user.major_budget = major_budget_val
            user.major_trade_ratio = weight if has_major else user.major_trade_ratio
            user.ai_max_coins = max_coins
            user.ai_is_shutting_down = False
            await db.commit()

        logger.info(
            "VIP AI 동적 설정: user_id=%s engines=%s budgets=%s weight=%d%% max_coins=%d",
            self._user_id, self._selected_engines, budgets, weight, max_coins,
        )

        # 완료 Embed 생성
        embed = discord.Embed(
            title="🤖 AI 실전 엔진 설정 완료",
            description="선택한 엔진이 활성화되었습니다.",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="📊 알트 스윙",
            value=f"🟢 ON | **{swing_budget:,} KRW**" if "SWING" in sel else "⏸️ OFF",
            inline=True,
        )
        embed.add_field(
            name="⚡ 알트 스캘핑",
            value=f"🟢 ON | **{scalp_budget:,} KRW**" if "SCALPING" in sel else "⏸️ OFF",
            inline=True,
        )
        embed.add_field(
            name="🏔️ 메이저 트렌드",
            value=f"🟢 ON | **{major_budget_val:,} KRW**" if "MAJOR" in sel else "⏸️ OFF",
            inline=True,
        )
        embed.add_field(
            name="⚙️ 공통 설정",
            value=f"진입 비중: **{weight}%**  |  최대 종목: **{max_coins}개**",
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


# ------------------------------------------------------------------
# 공통 유효성 검사 헬퍼
# ------------------------------------------------------------------

def _validate_budget_range(
    raw: str,
    min_val: int = 50_000,
    max_val: int = 10_000_000,
) -> tuple[int | None, str | None]:
    """예산 문자열을 파싱하고 min_val ~ max_val 범위를 검증한다.

    Args:
        raw:     사용자 입력 문자열.
        min_val: 허용 최솟값 (기본 50,000 KRW).
        max_val: 허용 최댓값 (기본 10,000,000 KRW).

    Returns:
        (int 값, None) 또는 (None, 오류 메시지).
    """
    try:
        v = int(raw.replace(",", "").strip())
    except ValueError:
        return None, "❌ 예산은 숫자로 입력해 주세요."
    if not min_val <= v <= max_val:
        return (
            None,
            f"❌ 예산은 **최소 {min_val:,} ~ 최대 {max_val:,} KRW** 사이로 입력해 주세요.",
        )
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
            placeholder="예: 500000  |  최소 50,000 ~ 최대 10,000,000 원",
            min_length=5,
            max_length=10,
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

        budget, err = _validate_budget_range(self.budget.value, 50_000, 10_000_000)
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
            placeholder="예: 500000  |  최소 50,000 ~ 최대 10,000,000 원",
            min_length=5,
            max_length=10,
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

        budget, err = _validate_budget_range(self.budget.value, 50_000, 10_000_000)
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
            placeholder="예: 500000  |  최소 50,000 ~ 최대 10,000,000 원",
            min_length=5,
            max_length=10,
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

        budget, err = _validate_budget_range(self.budget.value, 50_000, 10_000_000)
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
            placeholder="예: 500000  |  최소 50,000 ~ 최대 10,000,000 원",
            min_length=5,
            max_length=10,
            default=str(current_swing_budget),
        )
        self.scalp_budget = discord.ui.TextInput(
            label="⚡ 알트 스캘핑 예산 (KRW)",
            placeholder="예: 500000  |  최소 50,000 ~ 최대 10,000,000 원",
            min_length=5,
            max_length=10,
            default=str(current_scalp_budget),
        )
        self.major_budget = discord.ui.TextInput(
            label="🏦 메이저 트렌드 예산 (KRW)",
            placeholder="예: 500000  |  최소 50,000 ~ 최대 10,000,000 원",
            min_length=5,
            max_length=10,
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

        swing_budget, err = _validate_budget_range(self.swing_budget.value, 50_000, 10_000_000)
        if err:
            await interaction.followup.send(f"[알트 스윙 예산] {err}", ephemeral=True)
            return

        scalp_budget, err = _validate_budget_range(self.scalp_budget.value, 50_000, 10_000_000)
        if err:
            await interaction.followup.send(f"[알트 스캘핑 예산] {err}", ephemeral=True)
            return

        major_budget_val, err = _validate_budget_range(self.major_budget.value, 50_000, 10_000_000)
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
        description="AI 실전 자동 매매 펀드 매니저를 설정합니다. (PRO/VIP 전용)",
    )
    async def ai_settings_command(self, interaction: discord.Interaction) -> None:
        """등급에 따라 분기하여 적합한 엔진 선택 View(1단계)를 표시한다.

        [FREE  ] max_active_engines == 0 → FREE 차단 Embed 반환.
        [PRO   ] 알트 엔진 버튼 View (SWING / SCALPING / OFF) 표시.
        [VIP   ] 토글 버튼 View (SWING / SCALPING / MAJOR / OFF + 다음 →) 표시.
        """
        user_id = str(interaction.user.id)

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == user_id))
            user = result.scalar_one_or_none()

        # ── FREE 차단 ────────────────────────────────────────────────
        # getattr default를 0으로 설정 — 컬럼 미존재 시 안전하게 차단
        max_engines = int(getattr(user, "max_active_engines", 0) if user else 0)
        if user is None or max_engines == 0:
            await interaction.response.send_message(
                embed=_make_free_blocked_embed(), ephemeral=True
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

        is_vip = user.subscription_tier == SubscriptionTier.VIP

        if is_vip:
            title = "🤖 AI 실전 자동 매매 설정 대시보드 (VIP)"
            desc = (
                "아래 버튼으로 가동할 **엔진을 선택** (복수 선택 가능)하고 **[다음 →]** 을 누르세요.\n"
                "OFF 버튼은 모든 엔진을 즉시 중지합니다."
            )
        else:
            title = "🤖 AI 실전 자동 매매 설정 대시보드 (PRO)"
            desc = (
                "아래 버튼에서 가동할 **알트 엔진 1개**를 선택하세요.\n"
                "*(PRO 등급은 알트 스윙 또는 알트 스캘핑 중 1개만 사용 가능합니다)*"
            )

        embed = discord.Embed(
            title=title,
            description=desc,
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
        if is_vip:
            embed.add_field(
                name="⚙️ [엔진 3] 메이저 트렌드 (4h)",
                value=_engine_status_line(major_on, major_budget, major_ratio),
                inline=False,
            )
        embed.set_footer(text="⏱️ 이 메시지는 3분 후 만료됩니다.")

        if is_vip:
            view: discord.ui.View = VipEngineToggleView(user=user)
        else:
            view = ProEngineSelectView(user=user)
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
