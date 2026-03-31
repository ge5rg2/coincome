"""
모의투자·AI 통계 슬래시 커맨드 Cog (V2 — 모듈형 엔진 선택).

/ai모의        : AI 모의투자 ON/OFF 설정 (모든 등급 사용 가능, VIP 등급 체크 없음).
                 API 키 없이 가상 잔고(virtual_krw)로 AI가 자동 종목 선정·매수.
                 엔진 선택: SWING / SCALPING / MAJOR (단독) / ALL (3엔진 통합).
/ai모의초기화  : 모의투자 전체 초기화.
                 가상 잔고 1,000만 원 리셋 + 모의 워커 중지 + BotSetting/TradeHistory 삭제.
/ai통계        : AI 매매 성과 Embed 리포트.
                 VIP(ai_mode_enabled=True) → 실전 AI 통계 + 모의투자 통계 모두 표시.
                 그 외 / 실전 기록 없는 유저 → 모의투자 통계만 표시.

처리 흐름 (2단계 UI):
  [Step 1] PaperSettingView 표시 (AI 모드 ON/OFF, 엔진 선택 드롭다운)
  [Step 2] "다음 →" 버튼 클릭 → 엔진에 따라 다른 Modal(팝업창) 표시
           SWING    → PaperSwingSettingsModal   (스윙 가상 예산·비중·최대종목) — 단독 모드
           SCALPING → PaperScalpSettingsModal   (스캘핑 가상 예산·비중·최대종목) — 단독 모드
           MAJOR    → PaperMajorSettingsModal   (메이저 가상 예산·비중·최대종목) — 단독 모드
           ALL      → PaperAllEnginesModal      (3엔진 예산+공통비중+최대종목 5필드)
  [Step 3] 유저 제출 → DB 업데이트 (타 엔진 강제 OFF 포함) → 완료 Embed 반환

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
from app.services.exchange import ExchangeService
from app.services.trading_worker import WorkerRegistry
from app.services.websocket import UpbitWebsocketManager
from app.utils.format import format_krw_price
from app.utils.time import get_next_run_time_for_style

logger = logging.getLogger(__name__)

# 모의투자 초기 가상 잔고 (표시·초기화 전용 — 실제 기본값은 User.virtual_krw 에 저장)
_INITIAL_VIRTUAL_KRW = 10_000_000.0


def _portfolio_bar(coin_pct: float, total: int = 10) -> str:
    """코인 비중을 10칸 블록 바로 반환한다. 예: '████░░░░░░'"""
    filled = min(total, max(0, round(coin_pct / 100 * total)))
    return "█" * filled + "░" * (total - filled)


# ------------------------------------------------------------------
# FREE 차단 + 공통 모의투자 OFF Embed 헬퍼
# ------------------------------------------------------------------


def _make_paper_blocked_embed() -> discord.Embed:
    """FREE 등급 유저에게 AI 모의투자 차단을 안내하는 Embed를 반환한다."""
    embed = discord.Embed(
        title="🔒 AI 모의투자는 PRO 이상에서만 사용 가능합니다.",
        description=(
            "현재 등급: **FREE**  |  수동 매매만 이용 가능합니다.\n\n"
            "AI 모의투자를 사용하려면 **PRO** 또는 **VIP** 구독이 필요합니다."
        ),
        color=discord.Color.red(),
    )
    embed.add_field(
        name="📋 등급별 AI 모의투자 현황",
        value=(
            "• **FREE** — AI 모의투자 사용 불가\n"
            "• **PRO** — 알트 스윙 / 알트 스캘핑 중 **1개** 선택\n"
            "• **VIP** — 알트 스윙 + 알트 스캘핑 + 메이저 트렌드 **모두 사용 가능**"
        ),
        inline=False,
    )
    embed.set_footer(text="/구독 명령어로 업그레이드하세요.")
    return embed


def _make_paper_disabled_embed() -> discord.Embed:
    """AI 모의투자 비활성화 완료 Embed를 반환한다."""
    embed = discord.Embed(
        title="⏸️ AI 모의투자 종료",
        description=(
            "AI 모의투자가 **비활성화**되었습니다.\n"
            "현재 진행 중인 모의 포지션은 익절/손절 도달까지 계속 감시됩니다.\n"
            "가상 잔고는 그대로 유지됩니다."
        ),
        color=discord.Color.greyple(),
    )
    embed.set_footer(text="다시 켜려면 /ai모의 를 실행하세요.")
    return embed


# ------------------------------------------------------------------
# Step 1: PRO 전용 버튼 View (알트 엔진 1개 택 1, 모의투자)
# ------------------------------------------------------------------


class ProPaperEngineSelectView(discord.ui.View):
    """PRO Step 1 (모의): 알트 엔진 1개를 버튼으로 선택하는 View.

    SWING / SCALPING 선택 시 즉시 해당 Paper Modal 표시.
    OFF 선택 시 즉시 모의투자 비활성화.
    MAJOR 버튼 없음 (PRO 미제공).

    Attributes:
        _user: 요청 유저 User 객체.
    """

    def __init__(self, user: User) -> None:
        super().__init__(timeout=180)
        self._user = user

        current_engine = (getattr(user, "ai_engine_mode", None) or "SWING").upper()
        if current_engine == "BOTH":
            current_engine = "ALL"
        is_on = bool(user.ai_paper_mode_enabled)

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
        """알트 스윙 버튼 클릭 — PaperSwingSettingsModal 표시."""
        user = self._user
        modal = PaperSwingSettingsModal(
            user_id=user.user_id,
            current_budget=int(getattr(user, "ai_swing_budget_krw", 500_000) or 500_000),
            current_weight=int(getattr(user, "ai_swing_weight_pct", 20) or 20),
            current_max_coins=user.ai_max_coins,
            current_virtual_krw=float(user.virtual_krw),
        )
        await interaction.response.send_modal(modal)

    async def _on_scalp(self, interaction: discord.Interaction) -> None:
        """알트 스캘핑 버튼 클릭 — PaperScalpSettingsModal 표시."""
        user = self._user
        modal = PaperScalpSettingsModal(
            user_id=user.user_id,
            current_budget=int(getattr(user, "ai_scalp_budget_krw", 500_000) or 500_000),
            current_weight=int(getattr(user, "ai_scalp_weight_pct", 20) or 20),
            current_max_coins=user.ai_max_coins,
            current_virtual_krw=float(user.virtual_krw),
        )
        await interaction.response.send_modal(modal)

    async def _on_off(self, interaction: discord.Interaction) -> None:
        """OFF 버튼 클릭 — 즉시 모의투자 비활성화."""
        user = self._user
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == user.user_id))
            db_user = result.scalar_one_or_none()
            if db_user:
                db_user.ai_paper_mode_enabled = False
                await db.commit()
        logger.info("PRO AI 모의투자 비활성화: user_id=%s", user.user_id)
        await interaction.response.send_message(embed=_make_paper_disabled_embed(), ephemeral=True)


# ------------------------------------------------------------------
# Step 1: VIP 전용 토글 버튼 View (복수 선택, 모의투자)
# ------------------------------------------------------------------


class VipPaperEngineToggleView(discord.ui.View):
    """VIP Step 1 (모의): 엔진을 토글 버튼으로 복수 선택하는 View.

    OFF 버튼 클릭 시 즉시 모의투자 비활성화.
    [✅ 다음 →] 클릭 시 선택 수에 따른 VipPaperDynamicModal 표시.

    Attributes:
        _user:     요청 유저 User 객체.
        _selected: 현재 선택된 엔진 집합.
    """

    def __init__(self, user: User) -> None:
        super().__init__(timeout=180)
        self._user = user

        engine_mode = (getattr(user, "ai_engine_mode", None) or "SWING").upper()
        if engine_mode == "BOTH":
            engine_mode = "ALL"
        is_on = bool(user.ai_paper_mode_enabled)

        # 모의투자는 is_major_enabled 무관 — ai_engine_mode 기준으로 초기화
        initial: set[str] = set()
        if is_on:
            if engine_mode in ("SWING", "ALL"):
                initial.add("SWING")
            if engine_mode in ("SCALPING", "ALL"):
                initial.add("SCALPING")
            if engine_mode in ("MAJOR", "ALL"):
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
        if "SWING" in self._selected:
            self._selected.discard("SWING")
        else:
            self._selected.add("SWING")
        self._refresh_styles()
        await interaction.response.edit_message(view=self)

    async def _toggle_scalp(self, interaction: discord.Interaction) -> None:
        if "SCALPING" in self._selected:
            self._selected.discard("SCALPING")
        else:
            self._selected.add("SCALPING")
        self._refresh_styles()
        await interaction.response.edit_message(view=self)

    async def _toggle_major(self, interaction: discord.Interaction) -> None:
        if "MAJOR" in self._selected:
            self._selected.discard("MAJOR")
        else:
            self._selected.add("MAJOR")
        self._refresh_styles()
        await interaction.response.edit_message(view=self)

    async def _on_off(self, interaction: discord.Interaction) -> None:
        """OFF 버튼 — 즉시 모의투자 비활성화."""
        user = self._user
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == user.user_id))
            db_user = result.scalar_one_or_none()
            if db_user:
                db_user.ai_paper_mode_enabled = False
                await db.commit()
        logger.info("VIP AI 모의투자 비활성화: user_id=%s", user.user_id)
        await interaction.response.send_message(embed=_make_paper_disabled_embed(), ephemeral=True)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        """다음 버튼 — 선택 엔진 수에 따라 VipPaperDynamicModal 표시."""
        if not self._selected:
            await interaction.response.send_message(
                "⚠️ 최소 1개 이상 엔진을 선택해 주세요.", ephemeral=True
            )
            return

        user = self._user
        selected_list = sorted(self._selected)
        modal = VipPaperDynamicModal(
            user_id=user.user_id,
            selected_engines=selected_list,
            user=user,
        )
        await interaction.response.send_modal(modal)


# ------------------------------------------------------------------
# Step 2: VIP 동적 Modal (모의투자 전용)
# ------------------------------------------------------------------


class VipPaperDynamicModal(discord.ui.Modal):
    """VIP Step 2 (모의): 선택된 엔진 수에 따라 동적으로 필드가 구성되는 Modal.

    1개 선택: [가상 예산 1개] + [비중] + [최대종목] = 3필드
    2개 선택: [가상 예산A] + [가상 예산B] + [공통비중] + [최대종목] = 4필드
    3개 선택: [가상 예산A] + [가상 예산B] + [가상 예산C] + [공통비중] + [최대종목] = 5필드

    V2 불변 원칙: is_major_enabled, ai_mode_enabled 절대 수정 금지.

    Args:
        user_id:          Discord 사용자 ID.
        selected_engines: 선택된 엔진 목록.
        user:             User 객체 (기본값 참조용).
    """

    _ENGINE_LABELS: dict[str, str] = {
        "SWING": "📈 알트 스윙 가상 예산 (KRW)",
        "SCALPING": "⚡ 알트 스캘핑 가상 예산 (KRW)",
        "MAJOR": "🏔️ 메이저 트렌드 가상 예산 (KRW)",
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
        if len(selected_engines) == 1:
            engine_name = {"SWING": "알트 스윙", "SCALPING": "알트 스캘핑", "MAJOR": "메이저 트렌드"}.get(
                selected_engines[0], selected_engines[0]
            )
            title = f"🎮 [모의] {engine_name} 설정"
        elif len(selected_engines) == 2:
            title = "🎮 [모의] 2엔진 동시 가동 설정"
        else:
            title = "🎮 [모의] 3엔진 동시 가동 설정"

        super().__init__(title=title)
        self._user_id = user_id
        self._selected_engines = selected_engines

        self._budget_inputs: list[discord.ui.TextInput] = []
        for engine in selected_engines:
            attr = self._ENGINE_BUDGET_ATTR.get(engine, "ai_swing_budget_krw")
            current_val = int(getattr(user, attr, 500_000) or 500_000)
            budget_input = discord.ui.TextInput(
                label=self._ENGINE_LABELS.get(engine, f"{engine} 가상 예산 (KRW)"),
                placeholder="예: 500000  |  최소 500,000 ~ 최대 10,000,000 원",
                min_length=6,
                max_length=10,
                default=str(current_val),
            )
            self._budget_inputs.append(budget_input)
            self.add_item(budget_input)

        current_weight = int(getattr(user, "ai_swing_weight_pct", 20) or 20)
        self.weight = discord.ui.TextInput(
            label="공통 1회 진입 비중 (%) — 선택 엔진 동일 적용",
            placeholder="예: 20  |  10 ~ 100%",
            min_length=2,
            max_length=3,
            default=str(current_weight),
        )
        self.add_item(self.weight)

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
        """제출 처리: 입력값 검증 → DB 업데이트 (실전 플래그 불변) → 완료 Embed 반환."""
        await interaction.response.defer(ephemeral=True)

        # 가상 예산 검증 (모의: 500,000 ~ 10,000,000)
        budgets: dict[str, int] = {}
        for i, engine in enumerate(self._selected_engines):
            val, err = _validate_budget_range(self._budget_inputs[i].value, 500_000, 10_000_000)
            if err:
                engine_label = {"SWING": "알트 스윙", "SCALPING": "알트 스캘핑", "MAJOR": "메이저 트렌드"}.get(engine, engine)
                await interaction.followup.send(f"[{engine_label} 예산] {err}", ephemeral=True)
                return
            budgets[engine] = val  # type: ignore[assignment]

        weight, err = _validate_weight(self.weight.value)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        max_coins, err = _validate_max_coins(self.max_coins.value)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        total_budget = sum(budgets.values())

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == self._user_id))
            user = result.scalar_one_or_none()
            if user is None:
                await interaction.followup.send("❌ 유저 정보를 찾을 수 없습니다.", ephemeral=True)
                return

            # 가상 잔고 오토리필 (3엔진 예산 합산 기준)
            auto_refilled = False
            if float(user.virtual_krw) < total_budget:
                user.virtual_krw = float(total_budget)
                auto_refilled = True

            sel = set(self._selected_engines)
            swing_budget = budgets.get("SWING", 0)
            scalp_budget = budgets.get("SCALPING", 0)
            major_budget_val = budgets.get("MAJOR", 0)

            # 알트 엔진 모드 결정 (모의 전용 — ai_mode_enabled, is_major_enabled 건드리지 않음)
            has_swing = "SWING" in sel
            has_scalp = "SCALPING" in sel
            has_major = "MAJOR" in sel

            if has_swing and has_scalp and has_major:
                alt_engine_mode = "ALL"
            elif has_swing and has_scalp:
                alt_engine_mode = "ALL"
            elif has_swing and has_major:
                alt_engine_mode = "SWING"  # MAJOR는 engine_mode가 아닌 major_budget으로 제어
            elif has_scalp and has_major:
                alt_engine_mode = "SCALPING"
            elif has_swing:
                alt_engine_mode = "SWING"
            elif has_scalp:
                alt_engine_mode = "SCALPING"
            else:
                alt_engine_mode = "MAJOR"

            # ── V2 불변 원칙: ai_mode_enabled, is_major_enabled 절대 수정 금지 ──
            user.ai_paper_mode_enabled = True
            user.ai_engine_mode = alt_engine_mode
            user.ai_swing_budget_krw = swing_budget
            user.ai_swing_weight_pct = weight if has_swing else user.ai_swing_weight_pct
            user.ai_scalp_budget_krw = scalp_budget
            user.ai_scalp_weight_pct = weight if has_scalp else user.ai_scalp_weight_pct
            user.major_budget = major_budget_val
            user.major_trade_ratio = weight if has_major else user.major_trade_ratio
            user.ai_max_coins = max_coins
            await db.commit()
            final_virtual_krw = float(user.virtual_krw)

        logger.info(
            "VIP AI 모의투자 동적 설정: user_id=%s engines=%s budgets=%s weight=%d%% max_coins=%d auto_refilled=%s",
            self._user_id, self._selected_engines, budgets, weight, max_coins, auto_refilled,
        )

        sel_str = " + ".join(
            {"SWING": "알트 스윙", "SCALPING": "알트 스캘핑", "MAJOR": "메이저 트렌드"}.get(e, e)
            for e in self._selected_engines
        )
        embed = discord.Embed(
            title="🎮 AI 모의투자 엔진 설정 완료",
            description=f"**{sel_str}** 엔진이 모의투자로 활성화되었습니다.",
            color=discord.Color.purple(),
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
        embed.add_field(
            name="💰 가상 잔고",
            value=f"**{final_virtual_krw:,.0f} KRW**",
            inline=True,
        )
        if auto_refilled:
            embed.add_field(
                name="💡 가상 잔고 자동 충전",
                value=f"가상 잔고가 설정 예산보다 부족하여 **{total_budget:,} KRW**로 자동 충전되었습니다.",
                inline=False,
            )
        embed.add_field(
            name="📌 모의투자 안내",
            value="실제 업비트 API 키는 사용되지 않습니다.\n매매 성과는 `/ai통계`에서 확인하세요.",
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


# ------------------------------------------------------------------
# 공통 유효성 검사 헬퍼
# ------------------------------------------------------------------


def _validate_budget_range(
    raw: str,
    min_val: int = 500_000,
    max_val: int = 10_000_000,
) -> tuple[int | None, str | None]:
    """가상 예산 문자열을 파싱하고 min_val ~ max_val 범위를 검증한다.

    Args:
        raw:     사용자 입력 문자열.
        min_val: 허용 최솟값 (기본 500,000 KRW — 모의투자 최소).
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
# Step 2: SWING 전용 Modal (모의투자)
# ------------------------------------------------------------------


class PaperSwingSettingsModal(discord.ui.Modal, title="🎮 [모의] [단독] 알트 스윙 엔진 설정"):
    """알트 스윙 단독 모드(모의): 제출 시 스캘핑·메이저 엔진을 자동으로 OFF합니다."""

    def __init__(
        self,
        user_id: str,
        current_budget: int,
        current_weight: int,
        current_max_coins: int,
        current_virtual_krw: float,
    ) -> None:
        super().__init__()
        self._user_id = user_id
        self._virtual_krw = current_virtual_krw

        self.budget = discord.ui.TextInput(
            label="알트 스윙 가상 예산 (KRW)",
            placeholder="예: 1000000  |  [모의투자] 최소 500,000 ~ 최대 10,000,000 원",
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
            placeholder="예: 3  (1 ~ 10) | 가상 잔고가 종목별로 분산 투자됩니다.",
            min_length=1,
            max_length=2,
            default=str(current_max_coins),
        )
        self.add_item(self.budget)
        self.add_item(self.weight)
        self.add_item(self.max_coins)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """모달 제출 처리: 입력값 검증 → DB 업데이트 → 완료 Embed 반환."""
        await interaction.response.defer(ephemeral=True)

        budget, err = _validate_budget_range(self.budget.value, 500_000, 10_000_000)
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

        trade_amount = max(5_000, int(budget * weight / 100))

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == self._user_id))
            user = result.scalar_one_or_none()
            if user is None:
                await interaction.followup.send("❌ 유저 정보를 찾을 수 없습니다.", ephemeral=True)
                return

            # 가상 잔고 오토리필 (예산 > 잔고 시 자동 충전)
            auto_refilled = False
            if float(user.virtual_krw) < budget:
                user.virtual_krw = float(budget)
                auto_refilled = True

            # 모의 SWING 단독: ai_engine_mode="SWING"으로 제어 — 실전 플래그(is_major_enabled 등)는 건드리지 않음
            user.ai_paper_mode_enabled = True
            user.ai_engine_mode = "SWING"
            user.ai_swing_budget_krw = budget
            user.ai_swing_weight_pct = weight
            user.ai_max_coins = max_coins
            await db.commit()
            final_virtual_krw = float(user.virtual_krw)

        logger.info(
            "AI 모의투자 알트 스윙(단독) 설정: user_id=%s budget=%d weight=%d%% max_coins=%d auto_refilled=%s",
            self._user_id, budget, weight, max_coins, auto_refilled,
        )

        next_time = get_next_run_time_for_style("SWING")
        embed = discord.Embed(
            title="📊 [모의] [단독] 알트 스윙 엔진 가동",
            description=(
                "**알트 스윙 엔진만** 모의투자로 활성화되었습니다.\n"
                "알트 스캘핑·메이저 트렌드 엔진은 자동으로 OFF 되었습니다."
            ),
            color=discord.Color.blue(),
        )
        embed.add_field(name="📊 알트 스윙", value="🟢 ON", inline=True)
        embed.add_field(name="⚡ 알트 스캘핑", value="⏸️ OFF", inline=True)
        embed.add_field(name="🏦 메이저 트렌드", value="⏸️ OFF", inline=True)
        embed.add_field(
            name="💰 설정 (가상)",
            value=(
                f"가상 예산: **{budget:,} KRW**  |  진입 비중: **{weight}%**\n"
                f"1회 매수 기준금액: **{trade_amount:,} KRW**  |  최대 종목: **{max_coins}개**\n"
                f"현재 가상 잔고: **{final_virtual_krw:,.0f} KRW**"
            ),
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
        embed.add_field(
            name="📌 모의투자 안내",
            value=(
                "실제 업비트 API 키는 사용되지 않습니다.\n"
                "매매 성과는 `/ai통계`에서 확인하세요."
            ),
            inline=False,
        )
        if auto_refilled:
            embed.add_field(
                name="💡 가상 잔고 자동 충전",
                value=f"가상 잔고가 설정 예산보다 부족하여 **{budget:,} KRW**로 자동 충전되었습니다.",
                inline=False,
            )
        embed.set_footer(text=f"⏳ 다음 AI 분석: {next_time} | 01·05·09·13·17·21시 실행")
        await interaction.followup.send(embed=embed, ephemeral=True)


# ------------------------------------------------------------------
# Step 2: SCALPING 전용 Modal (모의투자)
# ------------------------------------------------------------------


class PaperScalpSettingsModal(discord.ui.Modal, title="🎮 [모의] [단독] 알트 스캘핑 엔진 설정"):
    """알트 스캘핑 단독 모드(모의): 제출 시 스윙·메이저 엔진을 자동으로 OFF합니다."""

    def __init__(
        self,
        user_id: str,
        current_budget: int,
        current_weight: int,
        current_max_coins: int,
        current_virtual_krw: float,
    ) -> None:
        super().__init__()
        self._user_id = user_id
        self._virtual_krw = current_virtual_krw

        self.budget = discord.ui.TextInput(
            label="알트 스캘핑 가상 예산 (KRW)",
            placeholder="예: 1000000  |  [모의투자] 최소 500,000 ~ 최대 10,000,000 원",
            min_length=7,
            max_length=12,
            default=str(current_budget),
        )
        self.weight = discord.ui.TextInput(
            label="알트 스캘핑 1회 진입 비중 (%)",
            placeholder="예: 30  |  단타 특성상 30~50% 권장 (손절 -1.5% 타이트).",
            min_length=2,
            max_length=3,
            default=str(current_weight),
        )
        self.max_coins = discord.ui.TextInput(
            label="최대 동시 보유 종목 수",
            placeholder="예: 3  (1 ~ 10) | 가상 잔고가 종목별로 분산 투자됩니다.",
            min_length=1,
            max_length=2,
            default=str(current_max_coins),
        )
        self.add_item(self.budget)
        self.add_item(self.weight)
        self.add_item(self.max_coins)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """모달 제출 처리: 입력값 검증 → DB 업데이트 → 완료 Embed 반환."""
        await interaction.response.defer(ephemeral=True)

        budget, err = _validate_budget_range(self.budget.value, 500_000, 10_000_000)
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

        trade_amount = max(5_000, int(budget * weight / 100))

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == self._user_id))
            user = result.scalar_one_or_none()
            if user is None:
                await interaction.followup.send("❌ 유저 정보를 찾을 수 없습니다.", ephemeral=True)
                return

            # 가상 잔고 오토리필
            auto_refilled = False
            if float(user.virtual_krw) < budget:
                user.virtual_krw = float(budget)
                auto_refilled = True

            # 모의 SCALPING 단독: ai_engine_mode="SCALPING"으로 제어 — 실전 플래그는 건드리지 않음
            user.ai_paper_mode_enabled = True
            user.ai_engine_mode = "SCALPING"
            user.ai_scalp_budget_krw = budget
            user.ai_scalp_weight_pct = weight
            user.ai_max_coins = max_coins
            await db.commit()
            final_virtual_krw = float(user.virtual_krw)

        logger.info(
            "AI 모의투자 알트 스캘핑(단독) 설정: user_id=%s budget=%d weight=%d%% max_coins=%d auto_refilled=%s",
            self._user_id, budget, weight, max_coins, auto_refilled,
        )

        next_time = get_next_run_time_for_style("SCALPING")
        embed = discord.Embed(
            title="⚡ [모의] [단독] 알트 스캘핑 엔진 가동",
            description=(
                "**알트 스캘핑 엔진만** 모의투자로 활성화되었습니다.\n"
                "알트 스윙·메이저 트렌드 엔진은 자동으로 OFF 되었습니다."
            ),
            color=discord.Color.orange(),
        )
        embed.add_field(name="📊 알트 스윙", value="⏸️ OFF", inline=True)
        embed.add_field(name="⚡ 알트 스캘핑", value="🟢 ON", inline=True)
        embed.add_field(name="🏦 메이저 트렌드", value="⏸️ OFF", inline=True)
        embed.add_field(
            name="💰 설정 (가상)",
            value=(
                f"가상 예산: **{budget:,} KRW**  |  진입 비중: **{weight}%**\n"
                f"1회 매수 기준금액: **{trade_amount:,} KRW**  |  최대 종목: **{max_coins}개**\n"
                f"현재 가상 잔고: **{final_virtual_krw:,.0f} KRW**"
            ),
            inline=False,
        )
        embed.add_field(
            name="📋 전략",
            value="진입: Close > MA20 AND RSI 60~75\n익절: +2.0% / 손절: -1.5% (R:R 1.33:1)",
            inline=False,
        )
        embed.add_field(
            name="📌 모의투자 안내",
            value="실제 업비트 API 키는 사용되지 않습니다.\n매매 성과는 `/ai통계`에서 확인하세요.",
            inline=False,
        )
        if auto_refilled:
            embed.add_field(
                name="💡 가상 잔고 자동 충전",
                value=f"가상 잔고가 설정 예산보다 부족하여 **{budget:,} KRW**로 자동 충전되었습니다.",
                inline=False,
            )
        embed.set_footer(text=f"⏳ 다음 AI 분석: {next_time} | 매시 정각 실행")
        await interaction.followup.send(embed=embed, ephemeral=True)


# ------------------------------------------------------------------
# Step 2: ALL(3엔진 통합) Modal (모의투자)
# ------------------------------------------------------------------


class PaperAllEnginesModal(discord.ui.Modal, title="🎮 [모의] [통합] 3엔진 동시 가동 설정"):
    """3개 엔진 동시 가동 모달(모의) — 5필드로 모든 엔진을 한 번에 설정합니다."""

    def __init__(
        self,
        user_id: str,
        current_swing_budget: int,
        current_scalp_budget: int,
        current_major_budget: int,
        current_ratio: int,
        current_max_coins: int,
        current_virtual_krw: float,
    ) -> None:
        super().__init__()
        self._user_id = user_id
        self._virtual_krw = current_virtual_krw

        self.swing_budget = discord.ui.TextInput(
            label="📊 알트 스윙 가상 예산 (KRW)",
            placeholder="예: 1000000  |  [모의투자] 최소 500,000 ~ 최대 10,000,000 원",
            min_length=7,
            max_length=12,
            default=str(current_swing_budget),
        )
        self.scalp_budget = discord.ui.TextInput(
            label="⚡ 알트 스캘핑 가상 예산 (KRW)",
            placeholder="예: 1000000  |  [모의투자] 최소 500,000 ~ 최대 10,000,000 원",
            min_length=7,
            max_length=12,
            default=str(current_scalp_budget),
        )
        self.major_budget = discord.ui.TextInput(
            label="🏦 메이저 트렌드 가상 예산 (KRW)",
            placeholder="예: 1000000  |  [모의투자] 최소 500,000 ~ 최대 10,000,000 원",
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
        """모달 제출 처리: 입력값 검증 → DB 업데이트 → 완료 Embed 반환."""
        await interaction.response.defer(ephemeral=True)

        swing_budget, err = _validate_budget_range(self.swing_budget.value, 500_000, 10_000_000)
        if err:
            await interaction.followup.send(f"[알트 스윙 예산] {err}", ephemeral=True)
            return

        scalp_budget, err = _validate_budget_range(self.scalp_budget.value, 500_000, 10_000_000)
        if err:
            await interaction.followup.send(f"[알트 스캘핑 예산] {err}", ephemeral=True)
            return

        major_budget_val, err = _validate_budget_range(self.major_budget.value, 500_000, 10_000_000)
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

        swing_trade_amount = max(5_000, int(swing_budget * ratio / 100))
        scalp_trade_amount = max(5_000, int(scalp_budget * ratio / 100))
        major_trade_amount = max(5_000, int(major_budget_val * ratio / 100))
        total_budget = swing_budget + scalp_budget + major_budget_val

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == self._user_id))
            user = result.scalar_one_or_none()
            if user is None:
                await interaction.followup.send("❌ 유저 정보를 찾을 수 없습니다.", ephemeral=True)
                return

            # 가상 잔고 오토리필 (3엔진 예산 합산 기준)
            auto_refilled = False
            if float(user.virtual_krw) < total_budget:
                user.virtual_krw = float(total_budget)
                auto_refilled = True

            # 모의 3엔진 동시 활성화: ai_engine_mode="ALL"로 제어 — is_major_enabled는 실전 전용 플래그, 건드리지 않음
            user.ai_paper_mode_enabled = True
            user.ai_engine_mode = "ALL"
            user.ai_swing_budget_krw = swing_budget
            user.ai_swing_weight_pct = ratio
            user.ai_scalp_budget_krw = scalp_budget
            user.ai_scalp_weight_pct = ratio
            user.major_budget = major_budget_val
            user.major_trade_ratio = ratio
            user.ai_max_coins = max_coins
            await db.commit()
            final_virtual_krw = float(user.virtual_krw)

        logger.info(
            "AI 모의투자 3엔진 통합 설정: user_id=%s swing=%d scalp=%d major=%d ratio=%d%% max_coins=%d auto_refilled=%s",
            self._user_id, swing_budget, scalp_budget, major_budget_val, ratio, max_coins, auto_refilled,
        )

        embed = discord.Embed(
            title="🔥 [모의] [통합] 3엔진 동시 가동 활성화",
            description=(
                "**알트 스윙 + 알트 스캘핑 + 메이저 트렌드** 3개 엔진이 모의투자로 동시에 가동됩니다.\n"
                "각 엔진의 가상 예산은 분리되어 독립적으로 운용됩니다."
            ),
            color=discord.Color.red(),
        )
        embed.add_field(name="📊 알트 스윙", value="🟢 ON", inline=True)
        embed.add_field(name="⚡ 알트 스캘핑", value="🟢 ON", inline=True)
        embed.add_field(name="🏦 메이저 트렌드", value="🟢 ON", inline=True)
        embed.add_field(
            name="📊 알트 스윙 설정 (가상)",
            value=f"예산: **{swing_budget:,} KRW**  →  1회: **{swing_trade_amount:,} KRW**",
            inline=False,
        )
        embed.add_field(
            name="⚡ 알트 스캘핑 설정 (가상)",
            value=f"예산: **{scalp_budget:,} KRW**  →  1회: **{scalp_trade_amount:,} KRW**",
            inline=False,
        )
        embed.add_field(
            name="🏦 메이저 트렌드 설정 (가상)",
            value=f"예산: **{major_budget_val:,} KRW**  →  1회: **{major_trade_amount:,} KRW**",
            inline=False,
        )
        embed.add_field(
            name="⚙️ 공통 설정",
            value=f"진입 비중: **{ratio}%** (3엔진 동일)  |  최대 종목: **{max_coins}개** (합산)",
            inline=False,
        )
        embed.add_field(
            name="💰 현재 가상 잔고",
            value=f"**{final_virtual_krw:,.0f} KRW**",
            inline=True,
        )
        embed.add_field(
            name="📋 전략 요약",
            value=(
                "**알트 스윙** 전략A 추세돌파 + 전략B 낙폭반등 자동전환 | 6회/일\n"
                "**알트 스캘핑** Close>MA20 + RSI 60~75 | TP 2% / SL 1.5% | 24회/일"
            ),
            inline=False,
        )
        embed.add_field(
            name="📌 모의투자 안내",
            value=(
                "실제 업비트 API 키는 사용되지 않습니다.\n"
                "매매 성과는 `/ai통계`에서 확인하세요.\n"
                "초기화가 필요하면 `/ai모의초기화`를 사용하세요."
            ),
            inline=False,
        )
        if auto_refilled:
            embed.add_field(
                name="💡 가상 잔고 자동 충전",
                value=f"가상 잔고가 설정 예산보다 부족하여 **{total_budget:,} KRW**로 자동 충전되었습니다.",
                inline=False,
            )
        embed.set_footer(text="알트 스윙: 01·05·09·13·17·21시 KST | 알트 스캘핑: 매시 정각 실행")
        await interaction.followup.send(embed=embed, ephemeral=True)


# ------------------------------------------------------------------
# Step 2: MAJOR 메이저 코인 전용 Modal (모의투자)
# ------------------------------------------------------------------


class PaperMajorSettingsModal(discord.ui.Modal, title="🎮 [모의투자] 메이저 트렌드 설정"):
    """메이저 코인 Trend Catcher 엔진 가상 예산·비중·최대종목을 입력받아 DB에 저장하는 Modal."""

    def __init__(
        self,
        user_id: str,
        current_budget: int,
        current_ratio: int,
        current_max_coins: int,
        current_virtual_krw: float,
    ) -> None:
        super().__init__()
        self._user_id = user_id
        self._current_virtual_krw = current_virtual_krw

        self.budget = discord.ui.TextInput(
            label="메이저 가상 예산 (KRW)",
            placeholder="예: 1000000  |  최소 500,000 ~ 최대 10,000,000 원",
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

        budget, err = _validate_budget_range(self.budget.value, 500_000, 10_000_000)
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

        trade_amount = max(5_000, int(budget * ratio / 100))

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == self._user_id))
            user = result.scalar_one_or_none()
            if user is None:
                await interaction.followup.send("❌ 유저 정보를 찾을 수 없습니다.", ephemeral=True)
                return

            # 가상 잔고 오토리필
            auto_refilled = False
            if float(user.virtual_krw) < budget:
                user.virtual_krw = float(budget)
                auto_refilled = True

            # 모의 MAJOR 단독: ai_engine_mode="MAJOR"으로 제어 — is_major_enabled는 실전 전용 플래그, 건드리지 않음
            user.ai_paper_mode_enabled = True
            user.ai_engine_mode = "MAJOR"
            user.major_budget = budget
            user.major_trade_ratio = ratio
            user.ai_max_coins = max_coins
            await db.commit()
            final_virtual_krw = float(user.virtual_krw)

        logger.info(
            "AI 모의투자 MAJOR 설정 업데이트: user_id=%s enabled=True budget=%d ratio=%d%% max_coins=%d auto_refilled=%s",
            self._user_id, budget, ratio, max_coins, auto_refilled,
        )

        next_time = get_next_run_time_for_style("SWING")
        embed = discord.Embed(
            title="🏦 [모의투자] 메이저 트렌드 엔진 가동",
            description=(
                "EMA200 장기 추세 + 정배열 + BB 상단 돌파 **3중 필터**가 모의투자로 활성화되었습니다.\n"
                "BTC·ETH 등 메이저 코인 전용 Trend Catcher — TP 4.0% / SL 2.0% (손익비 2:1)\n"
                "**API 키 없이 가상 잔고**로 메이저 전략을 검증할 수 있습니다."
            ),
            color=discord.Color.gold(),
        )
        embed.add_field(name="AI 모의투자", value="✅ 활성화", inline=True)
        embed.add_field(name="가동 엔진", value="🏦 메이저 트렌드", inline=True)
        embed.add_field(name="최대 보유 종목", value=f"{max_coins}개", inline=True)
        embed.add_field(
            name="💰 메이저 설정 (가상)",
            value=(
                f"가상 예산: **{budget:,} KRW**  |  1회 진입 비중: **{ratio}%**\n"
                f"→ 1회 매수 기준금액: **{trade_amount:,} KRW**\n"
                f"현재 가상 잔고: **{final_virtual_krw:,.0f} KRW**"
            ),
            inline=False,
        )
        embed.add_field(
            name="📋 메이저 전략",
            value=(
                "**3중 필터**: Close > EMA200 AND EMA20 > EMA50 AND Close > BB Upper(2σ)\n"
                "**진입**: 필터 통과 후 AI가 Fakeout vs 진짜 돌파 판별\n"
                "**익절**: +4.0% / **손절**: -2.0% (R:R 2:1 하드 고정)"
            ),
            inline=False,
        )
        if auto_refilled:
            embed.add_field(
                name="💡 가상 잔고 자동 충전",
                value=f"가상 잔고가 설정 예산보다 부족하여 **{budget:,} KRW**로 자동 충전되었습니다.",
                inline=False,
            )
        embed.set_footer(text=f"⏳ 다음 AI 분석: {next_time} | 01·05·09·13·17·21시 실행 (4h 봉 기준)")
        await interaction.followup.send(embed=embed, ephemeral=True)


# ------------------------------------------------------------------
# Cog
# ------------------------------------------------------------------


class PaperTradingCog(commands.Cog):
    """AI 모의투자·통계 관련 슬래시 커맨드 Cog (V2 — 모듈형 엔진 선택)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /ai모의 — AI 모의투자 ON/OFF 설정
    # ------------------------------------------------------------------

    @app_commands.command(
        name="ai모의",
        description="API 키 없이 AI가 가상 잔고로 자동 매매하는 모의투자 모드를 설정합니다. (PRO/VIP 전용)",
    )
    async def paper_trading_command(self, interaction: discord.Interaction) -> None:
        """등급에 따라 분기하여 적합한 엔진 선택 View(1단계)를 표시한다.

        [FREE ] max_active_engines == 0 → FREE 차단 Embed 반환.
        [PRO  ] 알트 엔진 버튼 View (SWING / SCALPING / OFF) 표시.
        [VIP  ] 토글 버튼 View (SWING / SCALPING / MAJOR / OFF + 다음 →) 표시.
        """
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

        # ── FREE 차단 ────────────────────────────────────────────────
        max_engines = int(getattr(user, "max_active_engines", 1) or 1)
        if max_engines == 0:
            await interaction.response.send_message(
                embed=_make_paper_blocked_embed(), ephemeral=True
            )
            return

        # 현재 엔진 모드 파악 (BOTH → ALL 레거시 마이그레이션)
        engine_mode = (getattr(user, "ai_engine_mode", None) or "SWING").upper()
        if engine_mode == "BOTH":
            engine_mode = "ALL"
        if engine_mode not in ("SWING", "SCALPING", "MAJOR", "ALL"):
            engine_mode = "SWING"

        swing_budget = int(getattr(user, "ai_swing_budget_krw", 0) or 0)
        swing_weight = int(getattr(user, "ai_swing_weight_pct", 20) or 20)
        scalp_budget = int(getattr(user, "ai_scalp_budget_krw", 0) or 0)
        scalp_weight = int(getattr(user, "ai_scalp_weight_pct", 20) or 20)
        major_budget = int(getattr(user, "major_budget", 0) or 0)
        major_ratio  = int(getattr(user, "major_trade_ratio", 10) or 10)
        paper_on     = bool(user.ai_paper_mode_enabled)

        # 엔진별 ON/OFF 판단 (모의투자는 is_major_enabled 무관 — ai_engine_mode 기준)
        swing_on  = paper_on and engine_mode in ("SWING", "ALL") and swing_budget > 0
        scalp_on  = paper_on and engine_mode in ("SCALPING", "ALL") and scalp_budget > 0
        major_on  = paper_on and engine_mode in ("MAJOR", "ALL") and major_budget > 0

        overall_status = "🟢 활성화" if paper_on else "⏸️ 비활성화"

        def _engine_status_line(on: bool, budget: int, ratio: int) -> str:
            if on and budget > 0:
                return f"🟢 ON | 가상 예산: **{budget:,} KRW** (진입 비중 **{ratio}%**)"
            return "⏸️ OFF | 미설정 (가동 중지)"

        is_vip = user.subscription_tier == SubscriptionTier.VIP

        if is_vip:
            title = "🎮 AI 모의투자 설정 대시보드 (VIP)"
            desc = (
                "아래 버튼으로 가동할 **엔진을 선택** (복수 선택 가능)하고 **[다음 →]** 을 누르세요.\n"
                "OFF 버튼은 모의투자를 즉시 중지합니다."
            )
        else:
            title = "🎮 AI 모의투자 설정 대시보드 (PRO)"
            desc = (
                "아래 버튼에서 가동할 **알트 엔진 1개**를 선택하세요.\n"
                "*(PRO 등급은 알트 스윙 또는 알트 스캘핑 중 1개만 사용 가능합니다)*"
            )

        embed = discord.Embed(
            title=title,
            description=desc,
            color=discord.Color.purple() if paper_on else discord.Color.greyple(),
        )
        embed.add_field(name="🔹 현재 상태", value=overall_status, inline=True)
        embed.add_field(name="🔹 최대 보유 종목", value=f"**{user.ai_max_coins}개**", inline=True)
        embed.add_field(name="💰 가상 잔고", value=f"**{float(user.virtual_krw):,.0f} KRW**", inline=True)
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
            view: discord.ui.View = VipPaperEngineToggleView(user=user)
        else:
            view = ProPaperEngineSelectView(user=user)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

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
                "AI 모의투자 ON/OFF 설정과 엔진 설정은 유지됩니다.\n"
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
          - is_real_active (VIP + ai_mode_enabled): [실전 AI 통계] 섹션 렌더링
          - is_paper_active (ai_paper_mode_enabled) OR 모의 데이터 존재: [모의투자 통계] 섹션 렌더링
          - 둘 다 활성: 구분선(━━━)으로 분리해 단일 Embed에 모두 표시

        잔고 계산 기준:
          실전) 총자산 = 업비트 KRW 잔고 + 보유 코인 현재 평가금액 합계
          모의) 총자산 = virtual_krw + 보유 코인 매수 원금 + 미실현 손익
        """
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)

        async with AsyncSessionLocal() as db:
            user_result = await db.execute(select(User).where(User.user_id == user_id))
            user = user_result.scalar_one_or_none()

            if user is None:
                await interaction.followup.send(
                    "❌ 등록된 계정이 없습니다.\n"
                    "`/ai모의` 명령어로 설정을 먼저 완료해 주세요.",
                    ephemeral=True,
                )
                return

            is_real_active = (
                user.subscription_tier == SubscriptionTier.VIP
                and user.ai_mode_enabled
            )
            is_paper_active = bool(user.ai_paper_mode_enabled)

            # ── 실전 데이터 조회 ───────────────────────────────────────
            real_histories: list[TradeHistory] = []
            real_open: list[BotSetting] = []
            if is_real_active:
                rh = await db.execute(
                    select(TradeHistory)
                    .where(
                        TradeHistory.user_id == user_id,
                        TradeHistory.is_paper_trading.is_(False),
                    )
                    .order_by(desc(TradeHistory.created_at))
                )
                real_histories = rh.scalars().all()

                ro = await db.execute(
                    select(BotSetting).where(
                        BotSetting.user_id == user_id,
                        BotSetting.is_running.is_(True),
                        BotSetting.is_paper_trading.is_(False),
                        BotSetting.is_ai_managed.is_(True),
                    )
                )
                real_open = ro.scalars().all()

            # ── 모의 데이터 조회 (항상) ───────────────────────────────
            ph = await db.execute(
                select(TradeHistory)
                .where(
                    TradeHistory.user_id == user_id,
                    TradeHistory.is_paper_trading.is_(True),
                )
                .order_by(desc(TradeHistory.created_at))
            )
            paper_histories: list[TradeHistory] = ph.scalars().all()

            po = await db.execute(
                select(BotSetting).where(
                    BotSetting.user_id == user_id,
                    BotSetting.is_running.is_(True),
                    BotSetting.is_paper_trading.is_(True),
                )
            )
            paper_open: list[BotSetting] = po.scalars().all()

        ws_manager = UpbitWebsocketManager.get()

        # ════════════════════════════════════════════════════════════
        # 실전 통계 계산
        # ════════════════════════════════════════════════════════════
        real_total = len(real_histories)
        real_wins = sum(1 for h in real_histories if h.profit_pct > 0)
        real_win_rate = real_wins / real_total * 100 if real_total > 0 else 0.0
        real_total_pnl = sum(h.profit_krw for h in real_histories)
        real_total_invested = sum(h.buy_amount_krw for h in real_histories)
        real_cum_pct = (
            real_total_pnl / real_total_invested * 100
            if real_total_invested > 0 else 0.0
        )

        # 실전 총자산 = 업비트 KRW 잔고 + 보유 코인 현재 평가금액
        actual_krw = 0.0
        if is_real_active and user.upbit_access_key and user.upbit_secret_key:
            try:
                _exchange = ExchangeService(
                    access_key=user.upbit_access_key,
                    secret_key=user.upbit_secret_key,
                )
                actual_krw = await _exchange.fetch_krw_balance()
            except Exception as exc:
                logger.warning(
                    "실전 KRW 잔고 조회 실패: user_id=%s err=%s", user_id, exc
                )

        real_coin_value = 0.0
        real_open_lines: list[str] = []
        for s in real_open:
            current_price = ws_manager.get_price(s.symbol)
            if s.buy_price is not None and current_price is not None:
                pct = (current_price - float(s.buy_price)) / float(s.buy_price) * 100
                coin_val = current_price * float(s.amount_coin or 0)
                real_coin_value += coin_val
                icon = "🟢" if pct >= 0 else "🔴"
                real_open_lines.append(
                    f"{icon} **{s.symbol}** | "
                    f"{format_krw_price(float(s.buy_price))} → "
                    f"{format_krw_price(current_price)} KRW | **{pct:+.2f}%**"
                )
            elif s.buy_price is None:
                real_open_lines.append(f"⏳ **{s.symbol}** | 매수 대기 중...")
            else:
                real_open_lines.append(f"❓ **{s.symbol}** | 시세 수신 대기 중...")

        real_total_asset = actual_krw + real_coin_value
        real_coin_pct = (
            real_coin_value / real_total_asset * 100
        ) if real_total_asset > 0 else 0.0

        # ════════════════════════════════════════════════════════════
        # 모의 통계 계산
        # ════════════════════════════════════════════════════════════
        virtual_krw = float(user.virtual_krw)
        paper_total = len(paper_histories)
        paper_wins = sum(1 for h in paper_histories if h.profit_pct > 0)
        paper_win_rate = paper_wins / paper_total * 100 if paper_total > 0 else 0.0
        paper_total_pnl_closed = sum(h.profit_krw for h in paper_histories)
        paper_total_invested_closed = sum(h.buy_amount_krw for h in paper_histories)
        paper_cum_pct = (
            paper_total_pnl_closed / paper_total_invested_closed * 100
            if paper_total_invested_closed > 0 else 0.0
        )

        # 보유 코인 매수 원금 + 미실현 손익 계산
        paper_coin_invested = 0.0
        paper_unrealized_pnl = 0.0
        paper_open_lines: list[str] = []
        for s in paper_open:
            current_price = ws_manager.get_price(s.symbol)
            paper_coin_invested += float(s.buy_amount_krw or 0)
            if s.buy_price is not None and current_price is not None:
                pct = (current_price - float(s.buy_price)) / float(s.buy_price) * 100
                pnl = (current_price - float(s.buy_price)) * float(s.amount_coin or 0)
                paper_unrealized_pnl += pnl
                icon = "🟢" if pct >= 0 else "🔴"
                paper_open_lines.append(
                    f"{icon} **{s.symbol}**\n"
                    f"  매수: {format_krw_price(float(s.buy_price))} → "
                    f"현재: {format_krw_price(current_price)} KRW"
                    f" | **{pct:+.2f}%** ({pnl:+,.0f} KRW)"
                )
            elif s.buy_price is None:
                paper_open_lines.append(f"⏳ **{s.symbol}** | 매수 대기 중...")
            else:
                paper_open_lines.append(f"❓ **{s.symbol}** | 시세 수신 대기 중...")

        # 모의 총자산 = 현금 잔고 + 코인 매수 원금 + 미실현 손익
        paper_total_asset = virtual_krw + paper_coin_invested + paper_unrealized_pnl

        # 동적 초기 시드: 현재 활성화된 엔진 예산 합산 (V2 다중 엔진 지원)
        # SWING 또는 ALL → ai_swing_budget_krw 포함
        # SCALPING 또는 ALL → ai_scalp_budget_krw 포함
        # MAJOR 또는 ALL (또는 is_major_enabled) → major_budget 포함
        _active_engine = (getattr(user, "ai_engine_mode", "SWING") or "SWING").upper()
        if _active_engine == "BOTH":
            _active_engine = "ALL"
        _paper_initial_seed: int = 0
        if _active_engine in ("SWING", "ALL"):
            _paper_initial_seed += int(getattr(user, "ai_swing_budget_krw", 0) or 0)
        if _active_engine in ("SCALPING", "ALL"):
            _paper_initial_seed += int(getattr(user, "ai_scalp_budget_krw", 0) or 0)
        if _active_engine in ("MAJOR", "ALL") or getattr(user, "is_major_enabled", False):
            _paper_initial_seed += int(getattr(user, "major_budget", 0) or 0)
        # 설정된 예산이 없으면 _INITIAL_VIRTUAL_KRW(1,000만) 폴백
        paper_initial_seed = float(_paper_initial_seed) if _paper_initial_seed > 0 else _INITIAL_VIRTUAL_KRW

        paper_balance_change = paper_total_asset - paper_initial_seed
        paper_coin_pct = (
            paper_coin_invested / paper_total_asset * 100
        ) if paper_total_asset > 0 else 0.0

        # ── Embed 색상·제목 ───────────────────────────────────────────
        if is_real_active:
            pnl_ref = real_total_pnl
        else:
            pnl_ref = paper_balance_change
        pnl_color = discord.Color.green() if pnl_ref >= 0 else discord.Color.red()
        embed = discord.Embed(title="📊 AI 매매 성과 리포트", color=pnl_color)

        show_paper = is_paper_active or bool(paper_histories or paper_open)

        # ════════════════════════════════════════════════════════════
        # 실전 AI 섹션
        # ════════════════════════════════════════════════════════════
        if is_real_active:
            # 포트폴리오 비중 차트
            bar = _portfolio_bar(real_coin_pct)
            embed.add_field(
                name="👑 실전 AI — 포트폴리오 현황",
                value=(
                    f"📊 포트폴리오 비중\n"
                    f"[{bar}] 코인 {real_coin_pct:.0f}% | 현금 {100 - real_coin_pct:.0f}%\n"
                    f"💰 AI 운용 총자산: **{real_total_asset:,.0f} KRW**\n"
                    f"  현금: {actual_krw:,.0f} KRW | 코인 평가액: {real_coin_value:,.0f} KRW"
                ),
                inline=False,
            )

            # 완료 거래 요약
            if real_total > 0:
                real_stats_value = (
                    f"총 거래: **{real_total}회** | "
                    f"**{real_wins}승 {real_total - real_wins}패** | "
                    f"승률: **{real_win_rate:.1f}%**\n"
                    f"누적 손익: **{real_total_pnl:+,.0f} KRW** ({real_cum_pct:+.2f}%)"
                )
            else:
                real_stats_value = "아직 실전 AI 거래 이력이 없습니다."
            embed.add_field(name="👑 실전 AI 완료 거래", value=real_stats_value, inline=False)

            # 진행 중인 실전 포지션
            if real_open_lines:
                embed.add_field(
                    name=f"💼 실전 진행 중 ({len(real_open)}건)",
                    value="\n".join(real_open_lines),
                    inline=False,
                )
            else:
                embed.add_field(
                    name="💼 실전 진행 중",
                    value="현재 실전 AI 포지션이 없습니다.",
                    inline=False,
                )

            # 최근 실전 거래 3건
            if real_histories:
                rec_lines: list[str] = []
                for h in real_histories[:3]:
                    icon = "🟢" if h.profit_pct > 0 else "🔴"
                    date_str = h.created_at.strftime("%m/%d %H:%M") if h.created_at else "-"
                    rec_lines.append(
                        f"{icon} **{h.symbol}** `{date_str}` | "
                        f"{format_krw_price(h.buy_price)} → "
                        f"{format_krw_price(h.sell_price)} KRW"
                        f" | **{h.profit_pct:+.2f}%**"
                    )
                embed.add_field(
                    name="📋 최근 실전 거래 (최대 3건)",
                    value="\n".join(rec_lines),
                    inline=False,
                )

            # 모의 섹션이 이어질 경우 구분선
            if show_paper:
                embed.add_field(
                    name="━━━━━━━━━━━━━━━━━━━━━━",
                    value="\u200b",
                    inline=False,
                )

        # ════════════════════════════════════════════════════════════
        # 모의투자 섹션
        # ════════════════════════════════════════════════════════════
        if show_paper:
            balance_icon = "📈" if paper_balance_change >= 0 else "📉"
            bar = _portfolio_bar(paper_coin_pct)
            embed.add_field(
                name="🎮 모의투자 — 포트폴리오 현황",
                value=(
                    f"📊 포트폴리오 비중\n"
                    f"[{bar}] 코인 {paper_coin_pct:.0f}% | 현금 {100 - paper_coin_pct:.0f}%\n"
                    f"💰 총 보유 자산: **{paper_total_asset:,.0f} KRW**"
                    f"  {balance_icon} 초기 시드 대비: **{paper_balance_change:+,.0f} KRW**\n"
                    f"  (기준 시드: {paper_initial_seed:,.0f} KRW — 현재 활성 엔진 예산 합산)\n"
                    f"  현금: {virtual_krw:,.0f} | 코인 원금: {paper_coin_invested:,.0f}"
                    f" | 미실현: {paper_unrealized_pnl:+,.0f} KRW"
                ),
                inline=False,
            )

            # 완료 거래 요약
            if paper_total > 0:
                paper_stats_value = (
                    f"총 거래: **{paper_total}회**\n"
                    f"승/패: **{paper_wins}승 {paper_total - paper_wins}패**\n"
                    f"승률: **{paper_win_rate:.1f}%**\n"
                    f"누적 손익: **{paper_total_pnl_closed:+,.0f} KRW**"
                    f" ({paper_cum_pct:+.2f}%)"
                )
            else:
                paper_stats_value = "아직 완료된 모의 거래가 없습니다."
            embed.add_field(name="🎮 모의 완료 거래 성과", value=paper_stats_value, inline=True)

            # 진행 중인 모의 포지션 + 미실현 손익 합계
            if paper_open_lines:
                embed.add_field(
                    name=f"👀 현재 진행 중인 모의투자 ({len(paper_open)}건)",
                    value="\n".join(paper_open_lines)
                    + f"\n\n미실현 손익 합계: **{paper_unrealized_pnl:+,.0f} KRW**",
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

            # 최근 모의 거래 5건
            if paper_histories:
                rec_lines_paper: list[str] = []
                for h in paper_histories[:5]:
                    icon = "🟢" if h.profit_pct > 0 else "🔴"
                    date_str = h.created_at.strftime("%m/%d %H:%M") if h.created_at else "-"
                    rec_lines_paper.append(
                        f"{icon} **{h.symbol}** `{date_str}`\n"
                        f"  {format_krw_price(h.buy_price)} → "
                        f"{format_krw_price(h.sell_price)} KRW"
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

        if not is_real_active and not show_paper:
            embed.description = (
                "활성화된 AI 투자 모드가 없습니다.\n"
                "`/ai모의` 또는 `/ai실전`을 사용해 시작하세요."
            )

        embed.set_footer(
            text=(
                "💡 /ai실전(VIP) 으로 실전 자동매매 | "
                "/ai모의 로 모의투자 ON/OFF | "
                "/ai모의초기화 로 리셋"
            )
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
