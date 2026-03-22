"""
모의투자·AI 통계 슬래시 커맨드 Cog (V2 — 모듈형 엔진 선택).

/ai모의        : AI 모의투자 ON/OFF 설정 (모든 등급 사용 가능, VIP 등급 체크 없음).
                 API 키 없이 가상 잔고(virtual_krw)로 AI가 자동 종목 선정·매수.
                 엔진 선택: SWING (4h 봉) / SCALPING (1h 봉) / BOTH (동시 가동).
/ai모의초기화  : 모의투자 전체 초기화.
                 가상 잔고 1,000만 원 리셋 + 모의 워커 중지 + BotSetting/TradeHistory 삭제.
/ai통계        : AI 매매 성과 Embed 리포트.
                 VIP(ai_mode_enabled=True) → 실전 AI 통계 + 모의투자 통계 모두 표시.
                 그 외 / 실전 기록 없는 유저 → 모의투자 통계만 표시.

처리 흐름 (2단계 UI):
  [Step 1] PaperSettingView 표시 (AI 모드 ON/OFF, 엔진 선택 드롭다운)
  [Step 2] "다음 →" 버튼 클릭 → 엔진에 따라 다른 Modal(팝업창) 표시
           SWING    → PaperSwingSettingsModal   (스윙 가상 예산·비중·최대종목)
           SCALPING → PaperScalpSettingsModal   (스캘핑 가상 예산·비중·최대종목)
           BOTH     → PaperBothSettingsModal    (4가지 + 최대종목)
  [Step 3] 유저 제출 → DB 업데이트 → 완료 Embed 반환

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
# Step 1: 드롭다운 Select 컴포넌트
# ------------------------------------------------------------------


class PaperModeSelect(discord.ui.Select):
    """AI 모의투자 모드(ON / OFF) 드롭다운."""

    def __init__(self, current_enabled: bool) -> None:
        options = [
            discord.SelectOption(
                label="✅ ON — AI 모의투자 활성화",
                value="ON",
                default=current_enabled,
            ),
            discord.SelectOption(
                label="⏸️ OFF — AI 모의투자 비활성화",
                value="OFF",
                default=not current_enabled,
            ),
        ]
        super().__init__(placeholder="AI 모의투자 모드를 선택하세요", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.view.mode_value = self.values[0]
        await interaction.response.defer()


class PaperEngineSelect(discord.ui.Select):
    """가동 엔진 선택 드롭다운 (SWING / SCALPING / BOTH).

    ai_trading.py의 EngineSelect와 동일한 엔진 선택지이며,
    모의투자 전용 placeholder 텍스트를 사용한다.
    """

    def __init__(self, current_engine: str) -> None:
        options = [
            discord.SelectOption(
                label="📊 4h 듀얼 스윙 [모의]",
                value="SWING",
                description="추세 돌파 + 낙폭 반등 자동 전환 | 01·05·09·13·17·21시 실행",
                default=current_engine == "SWING",
            ),
            discord.SelectOption(
                label="⚡ 1h 스캘핑 [모의]",
                value="SCALPING",
                description="단기 모멘텀 포착 | TP 2% / SL 1.5% | 매시 정각 실행",
                default=current_engine == "SCALPING",
            ),
            discord.SelectOption(
                label="🔥 동시 가동 [모의] (스윙+스캘핑)",
                value="BOTH",
                description="두 엔진 독립 운용 | 가상 예산·비중 각각 설정 가능",
                default=current_engine == "BOTH",
            ),
        ]
        super().__init__(
            placeholder="[모의투자] 가동 엔진을 선택하세요", options=options, row=1
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.view.engine_value = self.values[0]
        await interaction.response.defer()


# ------------------------------------------------------------------
# Step 1: View (드롭다운 + "다음" 버튼)
# ------------------------------------------------------------------


class PaperSettingView(discord.ui.View):
    """1단계: AI 모드·엔진을 드롭다운으로 선택하는 View.

    "다음 →" 버튼 클릭 시 선택된 엔진에 맞는 Modal(2단계)을 표시한다.
    timeout=180 초 (이후 버튼 비활성화).

    Attributes:
        mode_value:   현재 선택된 AI 모드 ("ON" / "OFF").
        engine_value: 현재 선택된 엔진 ("SWING" / "SCALPING" / "BOTH").
    """

    def __init__(self, user: User) -> None:
        super().__init__(timeout=180)
        self._user = user
        current_engine = (getattr(user, "ai_engine_mode", None) or "SWING").upper()
        # 하위 호환: SNIPER/BEAST → SWING/SCALPING
        if current_engine not in ("SWING", "SCALPING", "BOTH"):
            old_style = (getattr(user, "ai_trade_style", "SWING") or "SWING").upper()
            current_engine = "SCALPING" if old_style in ("BEAST", "SCALPING") else "SWING"

        self.mode_value: str = "ON" if user.ai_paper_mode_enabled else "OFF"
        self.engine_value: str = current_engine

        self.add_item(PaperModeSelect(current_enabled=user.ai_paper_mode_enabled))
        self.add_item(PaperEngineSelect(current_engine=current_engine))

    @discord.ui.button(label="다음 →", style=discord.ButtonStyle.primary, emoji="⚙️", row=2)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """OFF 선택 시 즉시 DB 저장 후 종료. ON 선택 시 엔진별 Modal 표시."""
        user = self._user
        engine = self.engine_value

        # ── OFF 선택: 모달 없이 즉시 DB 저장 + 완료 메시지 ─────────
        if self.mode_value == "OFF":
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(User).where(User.user_id == user.user_id)
                )
                db_user = result.scalar_one_or_none()
                if db_user:
                    db_user.ai_paper_mode_enabled = False
                    await db.commit()
            logger.info("AI 모의투자 비활성화: user_id=%s", user.user_id)
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
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # ── ON 선택: 엔진별 Modal 표시 ────────────────────────────────
        if engine == "SWING":
            modal = PaperSwingSettingsModal(
                user_id=user.user_id,
                current_budget=int(getattr(user, "ai_swing_budget_krw", 1_000_000) or 1_000_000),
                current_weight=int(getattr(user, "ai_swing_weight_pct", 20) or 20),
                current_max_coins=user.ai_max_coins,
                current_virtual_krw=float(user.virtual_krw),
            )
        elif engine == "SCALPING":
            modal = PaperScalpSettingsModal(
                user_id=user.user_id,
                current_budget=int(getattr(user, "ai_scalp_budget_krw", 1_000_000) or 1_000_000),
                current_weight=int(getattr(user, "ai_scalp_weight_pct", 20) or 20),
                current_max_coins=user.ai_max_coins,
                current_virtual_krw=float(user.virtual_krw),
            )
        else:  # BOTH
            modal = PaperBothSettingsModal(
                user_id=user.user_id,
                current_swing_budget=int(getattr(user, "ai_swing_budget_krw", 1_000_000) or 1_000_000),
                current_swing_weight=int(getattr(user, "ai_swing_weight_pct", 20) or 20),
                current_scalp_budget=int(getattr(user, "ai_scalp_budget_krw", 1_000_000) or 1_000_000),
                current_scalp_weight=int(getattr(user, "ai_scalp_weight_pct", 20) or 20),
                current_max_coins=user.ai_max_coins,
                current_virtual_krw=float(user.virtual_krw),
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
# Step 2: SWING 전용 Modal (모의투자)
# ------------------------------------------------------------------


class PaperSwingSettingsModal(discord.ui.Modal, title="🎮 [모의투자] 4h 듀얼 스윙 설정"):
    """스윙 엔진 가상 예산·비중·최대종목을 입력받아 DB에 저장하는 Modal.

    Args:
        user_id:             Discord 사용자 ID.
        current_budget:      현재 ai_swing_budget_krw DB 값 (pre-fill 용).
        current_weight:      현재 ai_swing_weight_pct DB 값 (pre-fill 용).
        current_max_coins:   현재 ai_max_coins DB 값 (pre-fill 용).
        current_virtual_krw: 현재 가상 잔고 (완료 Embed 표시용).
    """

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
            label="스윙 가상 예산 (KRW)",
            placeholder="예: 3000000  |  [모의투자] 가상 잔고 기준, 최소 1,000,000 원",
            min_length=7,
            max_length=12,
            default=str(current_budget),
        )
        self.weight = discord.ui.TextInput(
            label="스윙 1회 진입 비중 (%)",
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

        trade_amount = max(5_000, int(budget * weight / 100))

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == self._user_id))
            user = result.scalar_one_or_none()
            if user is None:
                await interaction.followup.send("❌ 유저 정보를 찾을 수 없습니다.", ephemeral=True)
                return

            # ── 가상 잔고 오토리필 (예산 > 잔고 시 자동 충전) ──────────
            auto_refilled = False
            if float(user.virtual_krw) < budget:
                user.virtual_krw = float(budget)
                auto_refilled = True

            user.ai_paper_mode_enabled = True
            user.ai_engine_mode = "SWING"
            user.ai_swing_budget_krw = budget
            user.ai_swing_weight_pct = weight
            user.ai_max_coins = max_coins
            user.ai_trade_style = "SWING"         # 하위 호환
            user.ai_trade_amount = trade_amount   # 하위 호환
            await db.commit()
            final_virtual_krw = float(user.virtual_krw)

        logger.info(
            "AI 모의투자 스윙 설정 업데이트: user_id=%s enabled=True budget=%d weight=%d%% max_coins=%d auto_refilled=%s",
            self._user_id, budget, weight, max_coins, auto_refilled,
        )

        next_time = get_next_run_time_for_style("SWING")
        embed = discord.Embed(
            title="📊 [모의투자] 4h 듀얼 스윙 엔진 가동",
            description=(
                "4시간 봉 기반 **듀얼 전략 엔진**이 모의투자로 활성화되었습니다.\n"
                "추세 돌파(전략A)와 낙폭과대 반등(전략B)을 시장 상황에 따라 자동 전환합니다.\n"
                "**API 키 없이 가상 잔고**로 안전하게 전략을 검증할 수 있습니다."
            ),
            color=discord.Color.blue(),
        )
        embed.add_field(name="AI 모의투자", value="✅ 활성화", inline=True)
        embed.add_field(name="가동 엔진", value="📊 4h 듀얼 스윙", inline=True)
        embed.add_field(name="최대 보유 종목", value=f"{max_coins}개", inline=True)
        embed.add_field(
            name="💰 스윙 설정 (가상)",
            value=(
                f"가상 예산: **{budget:,} KRW**  |  1회 진입 비중: **{weight}%**\n"
                f"→ 1회 매수 기준금액: **{trade_amount:,} KRW**\n"
                f"현재 가상 잔고: **{final_virtual_krw:,.0f} KRW**"
            ),
            inline=False,
        )
        embed.add_field(
            name="📋 전략 (듀얼 엔진 공통)",
            value=(
                "**전략A** 추세 돌파 (MA50 상승 + RSI 55~70) — 익절 **6.0%** / 손절 **4.0%** (R:R 1.5:1)\n"
                "**전략B** 낙폭 반등 (MA50 하락 + RSI < 25) — 익절 **3.0%** / 손절 **2.5%** (R:R 1.2:1)\n"
                "BTC 국면에 따라 전략A/B 자동 전환 | 메이저 코인 거래 차단"
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
                value=f"가상 잔고가 설정 예산보다 부족하여 **{budget:,} KRW**로 자동 충전되었습니다.",
                inline=False,
            )
        embed.set_footer(text=f"⏳ 다음 AI 분석: {next_time} | 01·05·09·13·17·21시 실행 (4h 봉 기준)")
        await interaction.followup.send(embed=embed, ephemeral=True)


# ------------------------------------------------------------------
# Step 2: SCALPING 전용 Modal (모의투자)
# ------------------------------------------------------------------


class PaperScalpSettingsModal(discord.ui.Modal, title="🎮 [모의투자] 1h 스캘핑 설정"):
    """스캘핑 엔진 가상 예산·비중·최대종목을 입력받아 DB에 저장하는 Modal.

    Args:
        user_id:             Discord 사용자 ID.
        current_budget:      현재 ai_scalp_budget_krw DB 값 (pre-fill 용).
        current_weight:      현재 ai_scalp_weight_pct DB 값 (pre-fill 용).
        current_max_coins:   현재 ai_max_coins DB 값 (pre-fill 용).
        current_virtual_krw: 현재 가상 잔고 (완료 Embed 표시용).
    """

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
            label="스캘핑 가상 예산 (KRW)",
            placeholder="예: 2000000  |  [모의투자] 가상 잔고 기준, 최소 1,000,000 원",
            min_length=7,
            max_length=12,
            default=str(current_budget),
        )
        self.weight = discord.ui.TextInput(
            label="스캘핑 1회 진입 비중 (%)",
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

        trade_amount = max(5_000, int(budget * weight / 100))

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == self._user_id))
            user = result.scalar_one_or_none()
            if user is None:
                await interaction.followup.send("❌ 유저 정보를 찾을 수 없습니다.", ephemeral=True)
                return

            # ── 가상 잔고 오토리필 (예산 > 잔고 시 자동 충전) ──────────
            auto_refilled = False
            if float(user.virtual_krw) < budget:
                user.virtual_krw = float(budget)
                auto_refilled = True

            user.ai_paper_mode_enabled = True
            user.ai_engine_mode = "SCALPING"
            user.ai_scalp_budget_krw = budget
            user.ai_scalp_weight_pct = weight
            user.ai_max_coins = max_coins
            user.ai_trade_style = "SCALPING"      # 하위 호환
            user.ai_trade_amount = trade_amount   # 하위 호환
            await db.commit()
            final_virtual_krw = float(user.virtual_krw)

        logger.info(
            "AI 모의투자 스캘핑 설정 업데이트: user_id=%s enabled=True budget=%d weight=%d%% max_coins=%d auto_refilled=%s",
            self._user_id, budget, weight, max_coins, auto_refilled,
        )

        next_time = get_next_run_time_for_style("SCALPING")
        embed = discord.Embed(
            title="⚡ [모의투자] 1h 스캘핑 엔진 가동",
            description=(
                "1시간 봉 기반 **단기 모멘텀 포착** 엔진이 모의투자로 활성화되었습니다.\n"
                "Close > MA20 + RSI 60~75 진입 조건, 매시 정각 실행합니다.\n"
                "**API 키 없이 가상 잔고**로 스캘핑 전략을 검증할 수 있습니다."
            ),
            color=discord.Color.orange(),
        )
        embed.add_field(name="AI 모의투자", value="✅ 활성화", inline=True)
        embed.add_field(name="가동 엔진", value="⚡ 1h 스캘핑", inline=True)
        embed.add_field(name="최대 보유 종목", value=f"{max_coins}개", inline=True)
        embed.add_field(
            name="💰 스캘핑 설정 (가상)",
            value=(
                f"가상 예산: **{budget:,} KRW**  |  1회 진입 비중: **{weight}%**\n"
                f"→ 1회 매수 기준금액: **{trade_amount:,} KRW**\n"
                f"현재 가상 잔고: **{final_virtual_krw:,.0f} KRW**"
            ),
            inline=False,
        )
        embed.add_field(
            name="📋 스캘핑 전략",
            value=(
                "진입: Close > MA20 AND RSI 60~75\n"
                "익절: **+2.0%** / 손절: **-1.5%** (R:R 1.33:1)\n"
                "메이저 코인 거래 차단 | 매시 정각 실행"
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
                value=f"가상 잔고가 설정 예산보다 부족하여 **{budget:,} KRW**로 자동 충전되었습니다.",
                inline=False,
            )
        embed.set_footer(text=f"⏳ 다음 AI 분석: {next_time} | 매시 정각 실행 (1h 봉 기준)")
        await interaction.followup.send(embed=embed, ephemeral=True)


# ------------------------------------------------------------------
# Step 2: BOTH(동시 가동) Modal (모의투자)
# ------------------------------------------------------------------


class PaperBothSettingsModal(discord.ui.Modal, title="🎮 [모의투자] 동시 가동 (스윙+스캘핑) 설정"):
    """두 엔진의 가상 예산·비중·최대종목을 입력받아 DB에 저장하는 Modal.

    Discord Modal 최대 5개 TextInput 제약에 맞게 구성.

    Args:
        user_id:               Discord 사용자 ID.
        current_swing_budget:  현재 ai_swing_budget_krw DB 값.
        current_swing_weight:  현재 ai_swing_weight_pct DB 값.
        current_scalp_budget:  현재 ai_scalp_budget_krw DB 값.
        current_scalp_weight:  현재 ai_scalp_weight_pct DB 값.
        current_max_coins:     현재 ai_max_coins DB 값.
        current_virtual_krw:   현재 가상 잔고 (완료 Embed 표시용).
    """

    def __init__(
        self,
        user_id: str,
        current_swing_budget: int,
        current_swing_weight: int,
        current_scalp_budget: int,
        current_scalp_weight: int,
        current_max_coins: int,
        current_virtual_krw: float,
    ) -> None:
        super().__init__()
        self._user_id = user_id
        self._virtual_krw = current_virtual_krw

        self.swing_budget = discord.ui.TextInput(
            label="📊 스윙 가상 예산 (KRW)",
            placeholder="예: 3000000  |  [모의투자] 최소 1,000,000 원",
            min_length=7,
            max_length=12,
            default=str(current_swing_budget),
        )
        self.swing_weight = discord.ui.TextInput(
            label="📊 스윙 1회 진입 비중 (%)",
            placeholder="예: 20  |  20%는 안전 지향, 70% 이상은 공격적 성향입니다.",
            min_length=2,
            max_length=3,
            default=str(current_swing_weight),
        )
        self.scalp_budget = discord.ui.TextInput(
            label="⚡ 스캘핑 가상 예산 (KRW)",
            placeholder="예: 2000000  |  [모의투자] 최소 1,000,000 원",
            min_length=7,
            max_length=12,
            default=str(current_scalp_budget),
        )
        self.scalp_weight = discord.ui.TextInput(
            label="⚡ 스캘핑 1회 진입 비중 (%)",
            placeholder="예: 30  |  단타 특성상 30~50% 권장 (손절 -1.5% 타이트).",
            min_length=2,
            max_length=3,
            default=str(current_scalp_weight),
        )
        self.max_coins = discord.ui.TextInput(
            label="최대 동시 보유 종목 수 (엔진 합산)",
            placeholder="예: 5  (1 ~ 10) | 두 엔진 합산 보유 종목 수입니다.",
            min_length=1,
            max_length=2,
            default=str(current_max_coins),
        )
        self.add_item(self.swing_budget)
        self.add_item(self.swing_weight)
        self.add_item(self.scalp_budget)
        self.add_item(self.scalp_weight)
        self.add_item(self.max_coins)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """모달 제출 처리: 입력값 검증 → DB 업데이트 → 완료 Embed 반환."""
        await interaction.response.defer(ephemeral=True)

        swing_budget, err = _validate_budget(self.swing_budget.value)
        if err:
            await interaction.followup.send(f"[스윙 예산] {err}", ephemeral=True)
            return

        swing_weight, err = _validate_weight(self.swing_weight.value)
        if err:
            await interaction.followup.send(f"[스윙 비중] {err}", ephemeral=True)
            return

        scalp_budget, err = _validate_budget(self.scalp_budget.value)
        if err:
            await interaction.followup.send(f"[스캘핑 예산] {err}", ephemeral=True)
            return

        scalp_weight, err = _validate_weight(self.scalp_weight.value)
        if err:
            await interaction.followup.send(f"[스캘핑 비중] {err}", ephemeral=True)
            return

        max_coins, err = _validate_max_coins(self.max_coins.value)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        swing_trade_amount = max(5_000, int(swing_budget * swing_weight / 100))
        scalp_trade_amount = max(5_000, int(scalp_budget * scalp_weight / 100))
        total_budget = swing_budget + scalp_budget

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == self._user_id))
            user = result.scalar_one_or_none()
            if user is None:
                await interaction.followup.send("❌ 유저 정보를 찾을 수 없습니다.", ephemeral=True)
                return

            # ── 가상 잔고 오토리필 (두 엔진 예산 합산 기준) ────────────
            auto_refilled = False
            if float(user.virtual_krw) < total_budget:
                user.virtual_krw = float(total_budget)
                auto_refilled = True

            user.ai_paper_mode_enabled = True
            user.ai_engine_mode = "BOTH"
            user.ai_swing_budget_krw = swing_budget
            user.ai_swing_weight_pct = swing_weight
            user.ai_scalp_budget_krw = scalp_budget
            user.ai_scalp_weight_pct = scalp_weight
            user.ai_max_coins = max_coins
            user.ai_trade_style = "SWING"                            # 하위 호환 (스윙 기준)
            user.ai_trade_amount = swing_trade_amount                 # 하위 호환
            await db.commit()
            final_virtual_krw = float(user.virtual_krw)

        logger.info(
            "AI 모의투자 동시 가동 설정 업데이트: user_id=%s enabled=True "
            "swing_budget=%d swing_weight=%d%% scalp_budget=%d scalp_weight=%d%% max_coins=%d auto_refilled=%s",
            self._user_id,
            swing_budget, swing_weight, scalp_budget, scalp_weight, max_coins, auto_refilled,
        )

        embed = discord.Embed(
            title="🔥 [모의투자] 동시 가동 모드 활성화",
            description=(
                "**📊 4h 듀얼 스윙** + **⚡ 1h 스캘핑** 두 엔진이 **독립적으로** 가동됩니다.\n"
                "각 엔진의 가상 예산과 비중이 분리되어 운용됩니다.\n"
                "**API 키 없이 가상 잔고**로 두 전략을 동시에 검증할 수 있습니다."
            ),
            color=discord.Color.red(),
        )
        embed.add_field(name="AI 모의투자", value="✅ 활성화", inline=True)
        embed.add_field(name="가동 엔진", value="🔥 동시 가동", inline=True)
        embed.add_field(name="최대 보유 종목", value=f"{max_coins}개 (합산)", inline=True)
        embed.add_field(
            name="📊 스윙 설정 (가상)",
            value=(
                f"가상 예산: **{swing_budget:,} KRW**  |  진입 비중: **{swing_weight}%**\n"
                f"→ 1회 매수 기준금액: **{swing_trade_amount:,} KRW**"
            ),
            inline=False,
        )
        embed.add_field(
            name="⚡ 스캘핑 설정 (가상)",
            value=(
                f"가상 예산: **{scalp_budget:,} KRW**  |  진입 비중: **{scalp_weight}%**\n"
                f"→ 1회 매수 기준금액: **{scalp_trade_amount:,} KRW**"
            ),
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
                "**스윙** 전략A 추세돌파 + 전략B 낙폭반등 자동전환 | 6회/일\n"
                "**스캘핑** Close>MA20 + RSI 60~75 | TP 2% / SL 1.5% | 24회/일"
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
        embed.set_footer(text="스윙: 01·05·09·13·17·21시 KST | 스캘핑: 매시 정각 실행")
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
        description="API 키 없이 AI가 가상 잔고로 자동 매매하는 모의투자 모드를 설정합니다.",
    )
    async def paper_trading_command(self, interaction: discord.Interaction) -> None:
        """유저 정보를 조회(없으면 자동 생성)한 뒤 드롭다운 선택 View(1단계)를 띄운다.

        [설정 UI] 드롭다운(모드·엔진) → "다음 →" 버튼 → Modal(가상 예산·비중) 2단계 흐름.
        VIP 등급 체크 없음 — 모든 등급 사용 가능.
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

        # 현재 엔진 모드 파악 (하위 호환 포함)
        engine_mode = (getattr(user, "ai_engine_mode", None) or "").upper()
        if engine_mode not in ("SWING", "SCALPING", "BOTH"):
            old_style = (getattr(user, "ai_trade_style", "SWING") or "SWING").upper()
            engine_mode = "SCALPING" if old_style in ("BEAST", "SCALPING") else "SWING"

        _ENGINE_LABELS = {
            "SWING": "📊 4h 듀얼 스윙",
            "SCALPING": "⚡ 1h 스캘핑",
            "BOTH": "🔥 동시 가동 (스윙 + 스캘핑)",
        }
        engine_label = _ENGINE_LABELS.get(engine_mode, engine_mode)

        swing_budget = int(getattr(user, "ai_swing_budget_krw", 1_000_000) or 1_000_000)
        swing_weight = int(getattr(user, "ai_swing_weight_pct", 20) or 20)
        scalp_budget = int(getattr(user, "ai_scalp_budget_krw", 1_000_000) or 1_000_000)
        scalp_weight = int(getattr(user, "ai_scalp_weight_pct", 20) or 20)

        embed = discord.Embed(
            title="🎮 AI 모의투자 설정 (V2)",
            description=(
                "드롭다운에서 **AI 모드**와 **가동 엔진**을 선택한 뒤\n"
                "**⚙️ 다음 →** 버튼을 눌러 가상 예산과 비중을 입력하세요.\n"
                "API 키 없이 **가상 잔고**로 AI 전략을 체험할 수 있습니다."
            ),
            color=discord.Color.purple(),
        )
        current_value_lines = [
            f"AI 모의투자: **{'ON' if user.ai_paper_mode_enabled else 'OFF'}**",
            f"가동 엔진: **{engine_label}**",
        ]
        if engine_mode in ("SWING", "BOTH"):
            current_value_lines.append(
                f"📊 스윙 설정: **{swing_budget:,} KRW** / **{swing_weight}%**"
            )
        if engine_mode in ("SCALPING", "BOTH"):
            current_value_lines.append(
                f"⚡ 스캘핑 설정: **{scalp_budget:,} KRW** / **{scalp_weight}%**"
            )
        current_value_lines.append(f"최대 종목: **{user.ai_max_coins}개**")
        current_value_lines.append(f"💰 가상 잔고: **{float(user.virtual_krw):,.0f} KRW**")

        embed.add_field(
            name="현재 설정",
            value="\n".join(current_value_lines),
            inline=False,
        )
        embed.set_footer(text="⏱️ 이 메시지는 3분 후 만료됩니다.")

        view = PaperSettingView(user=user)
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
        paper_balance_change = paper_total_asset - _INITIAL_VIRTUAL_KRW
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
                    f"  {balance_icon} 초기 대비: **{paper_balance_change:+,.0f} KRW**\n"
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
