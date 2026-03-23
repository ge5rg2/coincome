"""
/설정 슬래시 커맨드
- 코인 선택 (Select Menu)
- 매수금액 / 익절 % / 손절 % 설정 (Modal)
- DB 저장 후 TradingWorker 시작
"""
from __future__ import annotations

import logging

import discord
import httpx
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.bot_setting import BotSetting
from app.models.user import User
from app.services.exchange import ExchangeService
from app.services.trading_worker import TradingWorker, WorkerRegistry
from app.utils.format import format_krw_price

logger = logging.getLogger(__name__)


def _make_onboarding_embed() -> discord.Embed:
    """API 키 미등록 신규 유저에게 보여줄 보안 가이드 Embed를 반환한다.

    업비트 API 키 생성 시 권한 설정 및 IP 화이트리스트 가이드를 포함한다.
    """
    embed = discord.Embed(
        title="🔑 업비트 API 키 등록이 필요합니다",
        description=(
            "코인 자동 매매를 사용하려면 먼저 `/키등록` 명령어로 업비트 API 키를 등록해야 합니다.\n"
            "키 발급 전 반드시 아래 보안 가이드를 확인하세요."
        ),
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="✅ 허용 권한 (3가지만 체크)",
        value="- 자산조회\n- 주문조회\n- 주문하기",
        inline=True,
    )
    embed.add_field(
        name="🚫 절대 금지",
        value="- ~~출금하기~~\n- ~~입금하기~~",
        inline=True,
    )
    embed.add_field(
        name="🌐 IP 화이트리스트 등록 필수",
        value=f"업비트 API 설정에서 아래 IP **만** 허용하도록 설정하세요.\n```{settings.server_ip}```",
        inline=False,
    )
    embed.set_footer(text="보안 설정 완료 후 /키등록 명령어로 키를 등록해 주세요.")
    return embed


def _make_key_registered_embed() -> discord.Embed:
    """API 키 등록 완료 후 보안 재확인 Embed를 반환한다."""
    embed = discord.Embed(
        title="✅ API 키가 등록되었습니다.",
        color=discord.Color.green(),
    )
    embed.add_field(
        name="🔒 보안 설정 최종 확인",
        value=(
            "등록하신 API 키의 권한 및 IP 설정을 다시 한번 점검해 주세요.\n\n"
            "✅ **허용 권한** — 자산조회 · 주문조회 · 주문하기만 활성화\n"
            "🚫 **출금하기 · 입금하기는 반드시 비활성화**\n"
            f"🌐 **IP 화이트리스트** — `{settings.server_ip}` 만 허용되도록 설정"
        ),
        inline=False,
    )
    return embed


# ────────────────────────────────────────────────────────────────────
# 업비트 KRW 마켓 인메모리 캐시
#
# SettingsCog.cog_load() 에서 1회 채워지며, 이후 coin_autocomplete()
# 콜백이 매 키보드 입력마다 참조한다. REST API 를 입력 이벤트마다 호출하지
# 않으므로 Rate Limit 위험이 없다.
#
# 구조: [{"symbol": "BTC/KRW", "korean_name": "비트코인", "english_name": "Bitcoin"}, ...]
# ────────────────────────────────────────────────────────────────────
_KRW_MARKETS: list[dict] = []


async def _fetch_krw_markets() -> list[dict]:
    """업비트 REST API 에서 KRW 원화 마켓 목록을 가져와 정규화한다.

    Returns:
        symbol(BTC/KRW 형식)·korean_name·english_name 을 담은 dict 리스트.

    Raises:
        httpx.HTTPStatusError: API 응답이 4xx/5xx 일 때.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get("https://api.upbit.com/v1/market/all")
        resp.raise_for_status()

    return [
        {
            # KRW-BTC → BTC/KRW (CCXT 표준 심볼 형식)
            "symbol": f"{m['market'].split('-')[1]}/KRW",
            "korean_name": m["korean_name"],
            "english_name": m["english_name"],
        }
        for m in resp.json()
        if m["market"].startswith("KRW-")
    ]


async def coin_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """유저가 코인 검색창에 타이핑하면 실시간으로 매칭 항목을 반환한다.

    한글명·영문명·심볼(BTC/KRW 형식) 세 필드에서 부분 일치 검색한다.
    디스코드 API 제한에 따라 최대 25개까지만 반환한다.

    Args:
        interaction: 현재 Discord 인터랙션.
        current: 유저가 현재까지 입력한 문자열.

    Returns:
        매칭된 app_commands.Choice 리스트 (최대 25개).
    """
    if not _KRW_MARKETS:
        # 캐시가 아직 비어 있는 경우 (봇 기동 직후 등) — 빈 리스트 반환
        return []

    query = current.strip().lower()
    matches = [
        app_commands.Choice(
            name=f"{m['korean_name']} ({m['symbol']})",
            value=m["symbol"],
        )
        for m in _KRW_MARKETS
        if query in m["korean_name"].lower()
        or query in m["english_name"].lower()
        or query in m["symbol"].lower()
    ]
    return matches[:25]


class TradingSettingModal(discord.ui.Modal, title="매매 설정"):
    buy_amount = discord.ui.TextInput(
        label="매수 금액 (KRW)",
        placeholder="예: 50000",
        min_length=1,
        max_length=15,
    )
    target_profit = discord.ui.TextInput(
        label="익절 목표 (%)",
        placeholder="예: 3.5  (비워두면 미설정)",
        required=False,
        max_length=6,
    )
    stop_loss = discord.ui.TextInput(
        label="손절 지점 (%)",
        placeholder="예: 2.0  (비워두면 미설정)",
        required=False,
        max_length=6,
    )

    def __init__(self, symbol: str, bot: commands.Bot) -> None:
        super().__init__()
        self.symbol = symbol
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)

        try:
            buy_amount_krw = float(self.buy_amount.value.replace(",", ""))
            target_pct = float(self.target_profit.value) if self.target_profit.value.strip() else None
            stop_pct = float(self.stop_loss.value) if self.stop_loss.value.strip() else None
        except ValueError:
            await interaction.followup.send("❌ 숫자 형식이 올바르지 않습니다.", ephemeral=True)
            return

        # ---------------------------------------------------------
        # [방어 로직] 최소 6,000원 이상 입력 강제
        # ---------------------------------------------------------
        if buy_amount_krw < 6000:
            await interaction.followup.send(
                "❌ **매수 금액이 너무 적습니다.**\n"
                "업비트 최소 주문 한도(5,000원) 및 손절 시 가격 하락을 고려하여\n"
                "매수 금액은 **최소 6,000 KRW 이상**으로 설정해 주세요.",
                ephemeral=True
            )
            return
        # ---------------------------------------------------------

        async with AsyncSessionLocal() as db:
            # 사용자 조회 또는 생성
            result = await db.execute(select(User).where(User.user_id == user_id))
            user = result.scalar_one_or_none()
            if user is None:
                user = User(user_id=user_id)
                db.add(user)
                await db.flush()

            # 구독 등급 제한 검사
            if buy_amount_krw > user.max_invest_krw:
                await interaction.followup.send(
                    f"❌ {user.subscription_tier} 등급은 최대 {user.max_invest_krw:,.0f} KRW까지 투자 가능합니다.\n"
                    f"`/구독` 명령어로 등급을 업그레이드 해보세요.",
                    ephemeral=True,
                )
                return

            # 코인 개수 제한 검사
            cnt_result = await db.execute(
                select(BotSetting).where(
                    BotSetting.user_id == user_id,
                    BotSetting.is_running.is_(True),
                )
            )
            running_count = len(cnt_result.scalars().all())
            if running_count >= user.max_coins:
                await interaction.followup.send(
                    f"❌ {user.subscription_tier} 등급은 동시에 {user.max_coins}개까지만 운영 가능합니다.\n"
                    f"`/구독` 명령어로 등급을 업그레이드 해보세요.",
                    ephemeral=True,
                )
                return

            # 업비트 키 확인
            if not user.upbit_access_key or not user.upbit_secret_key:
                await interaction.followup.send(
                    "❌ 업비트 API 키가 등록되어 있지 않습니다.\n`/키등록` 명령어로 먼저 등록해 주세요.",
                    ephemeral=True,
                )
                return

            # BotSetting 저장
            setting = BotSetting(
                user_id=user_id,
                symbol=self.symbol,
                buy_amount_krw=buy_amount_krw,
                target_profit_pct=target_pct,
                stop_loss_pct=stop_pct,
                is_running=True,
            )
            db.add(setting)
            await db.commit()
            await db.refresh(setting)

            # 워커 시작
            exchange = ExchangeService(user.upbit_access_key, user.upbit_secret_key)
            worker = TradingWorker(
                setting_id=setting.id,
                user_id=user_id,
                symbol=self.symbol,
                buy_amount_krw=buy_amount_krw,
                target_profit_pct=target_pct,
                stop_loss_pct=stop_pct,
                exchange=exchange,
                notify_callback=self.bot._send_dm,
            )
            await WorkerRegistry.get().register(worker)
            worker.start()

        summary = (
            f"🚀 **자동 매매 시작!** `{self.symbol}`\n"
            f"매수금액: **{buy_amount_krw:,.0f} KRW**\n"
        )
        if target_pct:
            summary += f"익절: **+{target_pct}%**  "
        if stop_pct:
            summary += f"손절: **-{stop_pct}%**"

        await interaction.followup.send(summary, ephemeral=True)


# ---------------------------------------------------------
# 감시 중인 코인 재선택 시 — 익절/손절만 수정하는 경량 Modal
# ---------------------------------------------------------

class UpdateModal(discord.ui.Modal, title="수익 설정 변경"):
    """이미 감시 중인 코인의 익절·손절 목표만 수정하는 Modal.

    신규 매수를 진행하지 않고 DB의 target_profit_pct / stop_loss_pct 만 UPDATE 한다.
    실행 중인 워커의 인메모리 포지션도 즉시 반영해 다음 폴링 주기부터 새 기준이 적용된다.

    Args:
        setting: 현재 감시 중인 BotSetting 인스턴스 (기존 설정값 pre-fill에 사용).
        bot: Discord 봇 인스턴스 (WorkerRegistry 접근용).
    """

    def __init__(self, setting: BotSetting, bot: commands.Bot) -> None:
        super().__init__()
        self.setting_id = setting.id
        self.symbol = setting.symbol
        self.bot = bot

        # 기존 설정값을 TextInput.default 에 넣어 유저가 바로 확인·수정 가능하게 함
        tp_default = (
            f"{float(setting.target_profit_pct):.1f}"
            if setting.target_profit_pct is not None
            else ""
        )
        sl_default = (
            f"{float(setting.stop_loss_pct):.1f}"
            if setting.stop_loss_pct is not None
            else ""
        )

        self.target_profit = discord.ui.TextInput(
            label="익절 목표 (%)",
            placeholder="예: 3.5  (비워두면 미설정)",
            required=False,
            max_length=6,
            default=tp_default,
        )
        self.stop_loss = discord.ui.TextInput(
            label="손절 지점 (%)",
            placeholder="예: 2.0  (비워두면 미설정)",
            required=False,
            max_length=6,
            default=sl_default,
        )
        self.add_item(self.target_profit)
        self.add_item(self.stop_loss)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        # ── 입력값 파싱 ───────────────────────────────────────────────
        try:
            target_pct = (
                float(self.target_profit.value)
                if self.target_profit.value.strip()
                else None
            )
            stop_pct = (
                float(self.stop_loss.value)
                if self.stop_loss.value.strip()
                else None
            )
        except ValueError:
            await interaction.followup.send(
                "❌ 숫자 형식이 올바르지 않습니다.", ephemeral=True
            )
            return

        # ── DB 업데이트 (신규 매수 없이 목표치만 수정) ───────────────
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(BotSetting).where(BotSetting.id == self.setting_id)
            )
            setting = result.scalar_one_or_none()
            if setting is None:
                await interaction.followup.send(
                    "❌ 설정을 찾을 수 없습니다.", ephemeral=True
                )
                return
            setting.target_profit_pct = target_pct
            setting.stop_loss_pct = stop_pct
            await db.commit()

        # ── 실행 중인 워커 인메모리 상태 즉시 반영 ───────────────────
        # 다음 폴링 주기(_check_exit_conditions)부터 새 기준으로 동작.
        worker = WorkerRegistry.get().get_worker(self.setting_id)
        if worker is not None:
            worker.target_profit_pct = target_pct
            worker.stop_loss_pct = stop_pct
            if worker._position is not None:
                worker._position.target_profit_pct = target_pct
                worker._position.stop_loss_pct = stop_pct
            logger.info(
                "워커 수익 설정 인메모리 반영: setting_id=%s tp=%s sl=%s",
                self.setting_id, target_pct, stop_pct,
            )

        tp_str = f"+{target_pct:.1f}%" if target_pct is not None else "미설정"
        sl_str = f"-{stop_pct:.1f}%" if stop_pct is not None else "미설정"
        await interaction.followup.send(
            f"✅ **`{self.symbol}` 설정이 업데이트되었습니다.**\n"
            f"익절: **{tp_str}**  |  손절: **{sl_str}**\n"
            f"_(다음 폴링 주기부터 새 기준이 적용됩니다)_",
            ephemeral=True,
        )


# ---------------------------------------------------------
# 중지 명령어용 드롭다운 메뉴 (Select) UI
# ---------------------------------------------------------
class StopCoinSelect(discord.ui.Select):
    def __init__(self, active_settings: list[BotSetting]):
        options = [
            discord.SelectOption(
                label="전체 중지",
                value="ALL",
                description="모든 코인의 자동 매매를 즉시 중지합니다.",
                emoji="⏹️"
            )
        ]
        for s in active_settings:
            options.append(
                discord.SelectOption(
                    label=s.symbol,
                    value=str(s.id),
                    description=f"{s.symbol} 감시를 중지합니다.",
                    emoji="🪙"
                )
            )
        super().__init__(
            placeholder="중지할 코인을 선택하세요",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        selected_value = self.values[0]

        # 처리 시간이 걸릴 수 있으므로 defer
        await interaction.response.defer(ephemeral=True)

        if selected_value == "ALL":
            # 전체 중지 (기존과 동일하게 모든 워커 해제)
            await WorkerRegistry.get().stop_all_for_user(user_id)
            await interaction.edit_original_response(content="⏹️ 모든 자동 매매를 중지했습니다.", view=None)
        else:
            # 특정 코인만 중지
            setting_id = int(selected_value)
            symbol_name = next((opt.label for opt in self.options if opt.value == selected_value), "선택한 코인")
            
            # 워커 레지스트리에서 해당 설정만 제거하면 DB 초기화까지 자동으로 처리됨
            await WorkerRegistry.get().unregister(setting_id)
            await interaction.edit_original_response(content=f"⏹️ `{symbol_name}` 자동 매매를 중지했습니다.", view=None)

class StopCoinSelectView(discord.ui.View):
    def __init__(self, active_settings: list[BotSetting]):
        super().__init__(timeout=60)
        self.add_item(StopCoinSelect(active_settings))

class SettingsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        """Cog 로드 시 업비트 KRW 마켓 목록을 인메모리에 캐싱한다.

        setup_hook() 의 await bot.add_cog() 완료 시점에 자동 호출된다.
        실패해도 봇은 정상 기동하며, autocomplete 는 빈 리스트를 반환한다.
        """
        global _KRW_MARKETS
        try:
            _KRW_MARKETS = await _fetch_krw_markets()
            logger.info("업비트 KRW 마켓 캐시 완료: %d 개 코인", len(_KRW_MARKETS))
        except Exception as exc:
            logger.error("업비트 KRW 마켓 캐시 실패 (autocomplete 비활성): %s", exc)

    @app_commands.command(name="수동매매세팅", description="코인별 수동 자동 매매를 설정합니다. (매수금액·익절·손절 설정)")
    @app_commands.describe(coin="매매할 코인을 검색하세요 (한글명·영문명·심볼 모두 지원)")
    @app_commands.autocomplete(coin=coin_autocomplete)
    async def settings_command(
        self, interaction: discord.Interaction, coin: str
    ) -> None:
        user_id = str(interaction.user.id)

        async with AsyncSessionLocal() as db:
            # ── API 키 가드 ───────────────────────────────────────────
            result = await db.execute(select(User).where(User.user_id == user_id))
            user = result.scalar_one_or_none()
            if user is None or not user.upbit_access_key or not user.upbit_secret_key:
                await interaction.response.send_message(
                    embed=_make_onboarding_embed(), ephemeral=True
                )
                return

            # ── 이미 감시 중인 코인인지 확인 ─────────────────────────
            # 동일 심볼이 is_running=True 로 존재하면 신규 매수 대신
            # 익절/손절 목표치만 수정하는 UpdateModal 을 띄운다.
            existing_result = await db.execute(
                select(BotSetting).where(
                    BotSetting.user_id == user_id,
                    BotSetting.symbol == coin,
                    BotSetting.is_running.is_(True),
                )
            )
            existing = existing_result.scalar_one_or_none()

        if existing is not None:
            # 재선택 → 익절/손절만 수정 (기존값 pre-fill)
            modal = UpdateModal(setting=existing, bot=self.bot)
        else:
            # 신규 선택 → 매수금액·익절·손절 전체 설정
            modal = TradingSettingModal(symbol=coin, bot=self.bot)

        await interaction.response.send_modal(modal)
    
    @app_commands.command(name="중지", description="실행 중인 자동 매매를 중지합니다.")
    async def stop_command(self, interaction: discord.Interaction) -> None:
        user_id = str(interaction.user.id)

        async with AsyncSessionLocal() as db:
            # API 키 가드
            user_result = await db.execute(select(User).where(User.user_id == user_id))
            user = user_result.scalar_one_or_none()
            if user is None or not user.upbit_access_key or not user.upbit_secret_key:
                await interaction.response.send_message(embed=_make_onboarding_embed(), ephemeral=True)
                return

            # 현재 실행 중인 설정(코인) 목록 가져오기
            settings_result = await db.execute(
                select(BotSetting).where(
                    BotSetting.user_id == user_id,
                    BotSetting.is_running.is_(True),
                )
            )
            settings_list = settings_result.scalars().all()

        if not settings_list:
            await interaction.response.send_message("👀 현재 실행 중인 자동 매매가 없습니다.", ephemeral=True)
            return

        # 드롭다운 메뉴 띄우기
        view = StopCoinSelectView(settings_list)
        await interaction.response.send_message("🛑 중지할 코인을 선택해주세요:", view=view, ephemeral=True)

    @app_commands.command(name="키등록", description="업비트 API 키를 등록합니다.")
    @app_commands.describe(access_key="업비트 Access Key", secret_key="업비트 Secret Key")
    async def register_keys(
        self,
        interaction: discord.Interaction,
        access_key: str,
        secret_key: str,
    ) -> None:
        user_id = str(interaction.user.id)
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.user_id == user_id))
            user = result.scalar_one_or_none()
            if user is None:
                user = User(user_id=user_id)
                db.add(user)
            user.upbit_access_key = access_key
            user.upbit_secret_key = secret_key
            await db.commit()
        await interaction.response.send_message(embed=_make_key_registered_embed(), ephemeral=True)

    @app_commands.command(name="잔고", description="현재 감시 중인 코인의 수익률과 상태를 확인합니다.")
    async def status_command(self, interaction: discord.Interaction) -> None:
        # 연산이 조금 걸릴 수 있으므로 defer 처리 (사용자에게는 '봇이 생각하는 중...'으로 보임)
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)

        async with AsyncSessionLocal() as db:
            # API 키 가드 (defer 이후이므로 followup.send 사용)
            user_result = await db.execute(select(User).where(User.user_id == user_id))
            user = user_result.scalar_one_or_none()
            if user is None or not user.upbit_access_key or not user.upbit_secret_key:
                await interaction.followup.send(embed=_make_onboarding_embed(), ephemeral=True)
                return

            # 현재 실행 중인 봇 설정 조회
            settings_result = await db.execute(
                select(BotSetting).where(
                    BotSetting.user_id == user_id,
                    BotSetting.is_running.is_(True),
                )
            )
            settings_list = settings_result.scalars().all()

        if not settings_list:
            await interaction.followup.send("👀 현재 감시 중이거나 보유 중인 코인이 없습니다.", ephemeral=True)
            return

        # 2. Embed 카드 생성
        embed = discord.Embed(title="📊 현재 포트폴리오 상태", color=discord.Color.blurple())
        from app.services.websocket import UpbitWebsocketManager
        ws_manager = UpbitWebsocketManager.get()

        for s in settings_list:
            current_price = ws_manager.get_price(s.symbol)

            # 익절 / 손절 설정 표시 (미설정이면 "미설정" 표기)
            tp_str = f"+{s.target_profit_pct:.1f}%" if s.target_profit_pct is not None else "미설정"
            sl_str = f"-{s.stop_loss_pct:.1f}%" if s.stop_loss_pct is not None else "미설정"

            # 매매 모드 태그 (trade_style → 브랜드 이름으로 표시)
            _MODE_DISPLAY: dict[str, str] = {
                "SNIPER": "🛡️ 인텔리전트 스나이퍼",
                "BEAST":  "🔥 야수의 심장",
            }
            mode_tag  = _MODE_DISPLAY.get(s.trade_style or "", "")
            mode_line = f"**모드:** {mode_tag}\n" if mode_tag else ""

            config_line = (
                f"{mode_line}"
                f"**익절:** {tp_str}  |  **손절:** {sl_str}  |  **매수금액:** {float(s.buy_amount_krw):,.0f} KRW"
            )

            # 매수 체결이 완료되어 DB에 단가와 수량이 있는 경우
            if s.buy_price and s.amount_coin and current_price:
                profit_pct = (current_price - s.buy_price) / s.buy_price * 100
                pnl = (current_price - s.buy_price) * s.amount_coin
                status_icon = "🟢" if profit_pct >= 0 else "🔴"

                value = (
                    f"**매수가:** {format_krw_price(s.buy_price)} KRW\n"
                    f"**현재가:** {format_krw_price(current_price)} KRW\n"
                    f"**수익률:** {status_icon} **{profit_pct:+.2f}%** ({pnl:+,.0f} KRW)\n"
                    f"**수량:** {s.amount_coin:.6f}\n"
                    f"{config_line}"
                )
            else:
                # 설정은 켰으나 아직 시세 수신 전이거나 체결 대기 중인 경우
                value = f"⏳ 매수 대기 중 또는 시세 로딩 중...\n{config_line}"

            embed.add_field(name=f"🪙 {s.symbol}", value=value, inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="도움말", description="AI 트레이딩 봇의 엔진별 전략과 사용 방법을 안내합니다.")
    async def help_command(self, interaction: discord.Interaction) -> None:
        """3개 AI 엔진(알트 스윙 / 알트 스캘핑 / 메이저 트렌드)의 전략 설명 Embed를 반환한다."""
        embed = discord.Embed(
            title="📖 AI 트레이딩 봇 도움말",
            description=(
                "CoinCome AI는 **3가지 독립 엔진**으로 구성됩니다.\n"
                "각 엔진은 예산·비중이 분리되어 동시 가동이 가능합니다."
            ),
            color=discord.Color.blue(),
        )

        embed.add_field(
            name="📊 알트 스윙 (4h 봉) — `/ai실전` or `/ai모의`",
            value=(
                "낙폭 과대 및 추세 돌파를 노리는 양방향 스윙. **(알트코인 전용)**\n"
                "**대상**: 업비트 KRW 상위 알트코인 (메이저 제외)\n"
                "**실행**: 01·05·09·13·17·21시 KST (6회/일, 4h 봉 기준)\n"
                "**전략 A** 추세 돌파 — MA50 상승 & RSI 55~70 → 익절 **6%** / 손절 **4%**\n"
                "**전략 B** 낙폭 반등 — MA50 하락 & RSI < 25 → 익절 **3%** / 손절 **2.5%**\n"
                "→ BTC 국면에 따라 A/B 자동 전환 | AI Score 90점 이상만 진입"
            ),
            inline=False,
        )

        embed.add_field(
            name="⚡ 알트 스캘핑 (1h 봉) — `/ai실전` or `/ai모의`",
            value=(
                "상승 모멘텀에 올라타는 짧은 단타. **(알트코인 전용)**\n"
                "**대상**: 업비트 KRW 상위 알트코인 (메이저 제외)\n"
                "**실행**: 매시 정각 (24회/일, 1h 봉 기준)\n"
                "**진입 조건**: Close > MA20 AND RSI 60~75\n"
                "**익절**: +2.0% / **손절**: -1.5% (R:R 1.33:1)\n"
                "→ 단타 특성상 손절 타이트 | AI Score 90점 이상만 진입"
            ),
            inline=False,
        )

        embed.add_field(
            name="🏦 메이저 트렌드 (4h 봉) — `/ai실전` or `/ai모의`",
            value=(
                "비트코인 등 무거운 코인의 강한 돌파 추세를 끝까지 발라먹는 전략. **(메이저 8종 전용)**\n"
                "**대상**: BTC·ETH·XRP·SOL·DOGE·ADA·SUI·PEPE\n"
                "**실행**: 스윙 시간대와 동일 (01·05·09·13·17·21시 KST)\n"
                "**3중 필터**: Close > EMA200 AND EMA20 > EMA50 AND Close > BB Upper(2σ)\n"
                "**AI 판별**: 진짜 돌파 vs Fakeout (거래량·RSI·도지 여부)\n"
                "**익절**: +4.0% / **손절**: -2.0% (R:R 2:1 하드 고정)\n"
                "→ 알트 엔진과 독립 예산 | 블랙리스트 없음"
            ),
            inline=False,
        )

        embed.add_field(
            name="🔧 주요 명령어",
            value=(
                "`/ai실전` — VIP 전용 실전 AI 설정\n"
                "`/ai모의` — 무료 AI 모의투자 설정 (API 키 불필요)\n"
                "`/ai종료` — 실전 AI 연착륙 또는 즉시 종료\n"
                "`/ai통계` — 모의투자 수익률 통계\n"
                "`/수동매매세팅` — 코인별 수동 자동 매매 설정\n"
                "`/잔고` — 현재 보유 포지션 및 수익률 확인"
            ),
            inline=False,
        )

        embed.set_footer(text="AI 리포트는 매 실행 후 DM으로 자동 발송됩니다.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
