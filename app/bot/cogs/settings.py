"""
/설정 슬래시 커맨드
- 코인 선택 (Select Menu)
- 매수금액 / 익절 % / 손절 % 설정 (Modal)
- DB 저장 후 TradingWorker 시작
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.bot_setting import BotSetting
from app.models.user import User
from app.services.exchange import ExchangeService
from app.services.trading_worker import TradingWorker, WorkerRegistry

logger = logging.getLogger(__name__)

# 지원 코인 목록
SUPPORTED_COINS = [
    app_commands.Choice(name="비트코인 (BTC/KRW)", value="BTC/KRW"),
    app_commands.Choice(name="이더리움 (ETH/KRW)", value="ETH/KRW"),
    app_commands.Choice(name="리플 (XRP/KRW)", value="XRP/KRW"),
    app_commands.Choice(name="도지코인 (DOGE/KRW)", value="DOGE/KRW"),
    app_commands.Choice(name="솔라나 (SOL/KRW)", value="SOL/KRW"),
    app_commands.Choice(name="인터넷컴퓨터 (ICP/KRW)", value="ICP/KRW"),
    app_commands.Choice(name="테더 (USDT/KRW)", value="USDT/KRW"),
]


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

    @app_commands.command(name="설정", description="코인 자동 매매를 설정합니다.")
    @app_commands.describe(coin="매매할 코인을 선택하세요")
    @app_commands.choices(coin=SUPPORTED_COINS)
    async def settings_command(
        self, interaction: discord.Interaction, coin: app_commands.Choice[str]
    ) -> None:
        modal = TradingSettingModal(symbol=coin.value, bot=self.bot)
        await interaction.response.send_modal(modal)
    
    @app_commands.command(name="중지", description="실행 중인 자동 매매를 중지합니다.")
    async def stop_command(self, interaction: discord.Interaction) -> None:
        user_id = str(interaction.user.id)

        # 현재 실행 중인 설정(코인) 목록 가져오기
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(BotSetting).where(
                    BotSetting.user_id == user_id,
                    BotSetting.is_running.is_(True),
                )
            )
            settings_list = result.scalars().all()

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
        await interaction.response.send_message("✅ API 키가 등록되었습니다.", ephemeral=True)

    @app_commands.command(name="잔고", description="현재 감시 중인 코인의 수익률과 상태를 확인합니다.")
    async def status_command(self, interaction: discord.Interaction) -> None:
        # 연산이 조금 걸릴 수 있으므로 defer 처리 (사용자에게는 '봇이 생각하는 중...'으로 보임)
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)

        # 1. DB에서 현재 실행 중인 내 봇 설정들을 가져옴
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(BotSetting).where(
                    BotSetting.user_id == user_id,
                    BotSetting.is_running.is_(True)
                )
            )
            settings_list = result.scalars().all()

        if not settings_list:
            await interaction.followup.send("👀 현재 감시 중이거나 보유 중인 코인이 없습니다.", ephemeral=True)
            return

        # 2. Embed 카드 생성
        embed = discord.Embed(title="📊 현재 포트폴리오 상태", color=discord.Color.blurple())
        from app.services.websocket import UpbitWebsocketManager
        ws_manager = UpbitWebsocketManager.get()

        for s in settings_list:
            current_price = ws_manager.get_price(s.symbol)
            
            # 매수 체결이 완료되어 DB에 단가와 수량이 있는 경우
            if s.buy_price and s.amount_coin and current_price:
                profit_pct = (current_price - s.buy_price) / s.buy_price * 100
                pnl = (current_price - s.buy_price) * s.amount_coin
                status_icon = "🟢" if profit_pct >= 0 else "🔴"
                
                value = (
                    f"**매수가:** {s.buy_price:,.0f} KRW\n"
                    f"**현재가:** {current_price:,.0f} KRW\n"
                    f"**수익률:** {status_icon} **{profit_pct:+.2f}%** ({pnl:+,.0f} KRW)\n"
                    f"**수량:** {s.amount_coin:.6f}"
                )
            else:
                # 설정은 켰으나 아직 시세 수신 전이거나 체결 대기 중인 경우
                value = "⏳ 매수 대기 중 또는 시세 로딩 중..."

            embed.add_field(name=f"🪙 {s.symbol}", value=value, inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)
