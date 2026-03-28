"""
ManualSellView: AI 종합 리포트 하단에 첨부되는 수동 청산(Manual Override) UI.

사용자가 DM으로 받은 AI 리포트에서 Select Menu로 코인을 선택하는 즉시
1-Click으로 포지션이 청산된다 (별도 버튼 없음).

설계 원칙:
  - timeout=300초 (5분): 봇 재시작 시 만료 처리
  - 1-Click: Select 콜백(_on_select) 진입 즉시 defer 후 바로 청산 실행
  - Race Condition 방지: DB 재조회 후 is_running + buy_price 재검증
  - 실전/모의 분기: BotSetting.is_paper_trading 필드 기준
  - 실전 청산: WorkerRegistry.get_worker() → TradingWorker.force_sell() 재사용
  - 모의 청산 fallback: 워커 없으면 DB 직접 처리
  - 모든 응답은 ephemeral=True
  - 청산 후 self.stop() 으로 중복 실행 방지
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.bot_setting import BotSetting
from app.models.trade_history import TradeHistory
from app.models.user import User
from app.services.trading_worker import WorkerRegistry
from app.services.websocket import UpbitWebsocketManager

logger = logging.getLogger(__name__)


class ManualSellView(discord.ui.View):
    """AI 리포트에 부착되는 수동 청산 UI (1-Click).

    Attributes:
        bot:       Discord 봇 인스턴스.
        user_id:   Discord 사용자 ID (문자열).
        positions: 현재 보유 포지션 목록.
                   [{"setting_id": int, "symbol": str, "is_paper": bool, "profit_pct": float}]
    """

    def __init__(
        self,
        bot: commands.Bot,
        user_id: str,
        positions: list[dict],
    ) -> None:
        super().__init__(timeout=300)
        self._bot = bot
        self._user_id = user_id
        self._positions: list[dict] = positions

        if not positions:
            return

        # ── Select Menu 구성 ──────────────────────────────────────────
        options: list[discord.SelectOption] = []
        for pos in positions:
            mode_tag = "[모의]" if pos["is_paper"] else "[실전]"
            profit_str = f"{pos['profit_pct']:+.2f}%"
            label = f"{mode_tag} {pos['symbol']} {profit_str}"
            emoji = "📈" if pos["profit_pct"] >= 0 else "📉"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(pos["setting_id"]),
                    emoji=emoji,
                    description=f"선택 즉시 청산 실행 | setting_id={pos['setting_id']}",
                )
            )

        select_menu = discord.ui.Select(
            placeholder="청산할 코인을 선택하면 즉시 청산됩니다...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="manual_sell_select",
        )
        select_menu.callback = self._on_select
        self.add_item(select_menu)

    # ------------------------------------------------------------------
    # Select 콜백 — 선택 즉시 1-Click 청산
    # ------------------------------------------------------------------

    async def _on_select(self, interaction: discord.Interaction) -> None:
        """Select Menu 선택 콜백: 선택된 코인을 즉시 청산한다."""
        # 3초 타임아웃 방지: 최상단에서 defer
        await interaction.response.defer(ephemeral=True)

        # 권한 검증
        if str(interaction.user.id) != self._user_id:
            await interaction.followup.send(
                "본인의 리포트에서만 청산할 수 있습니다.", ephemeral=True
            )
            return

        # 중복 실행 방지
        if self.is_finished():
            await interaction.followup.send("이미 처리된 요청입니다.", ephemeral=True)
            return

        self.stop()

        raw_value = interaction.data["values"][0]
        try:
            setting_id = int(raw_value)
        except (ValueError, KeyError):
            await interaction.followup.send(
                "선택값이 올바르지 않습니다. 다시 시도해주세요.", ephemeral=True
            )
            return

        # ── Ghost Update 방지: DB 재조회 및 상태 재검증 ──────────────
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(BotSetting).where(
                        BotSetting.id == setting_id,
                        BotSetting.user_id == self._user_id,
                    )
                )
                setting = result.scalar_one_or_none()
        except Exception as exc:
            logger.error(
                "수동 청산 DB 조회 실패: user_id=%s setting_id=%s err=%s",
                self._user_id, setting_id, exc,
            )
            await interaction.followup.send(
                "DB 조회 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.", ephemeral=True
            )
            return

        if setting is None:
            await interaction.followup.send(
                "포지션 정보를 찾을 수 없습니다. 이미 삭제되었을 수 있습니다.", ephemeral=True
            )
            return

        if not setting.is_running:
            await interaction.followup.send("이미 중지된 포지션입니다.", ephemeral=True)
            return

        if setting.buy_price is None:
            await interaction.followup.send(
                "이미 청산된 포지션입니다. (매수 기록 없음)", ephemeral=True
            )
            return

        symbol = setting.symbol
        is_paper = setting.is_paper_trading

        # ── 실전 청산: TradingWorker.force_sell() 재사용 ─────────────
        if not is_paper:
            registry = WorkerRegistry.get()
            worker = registry.get_worker(setting_id)

            if worker is None:
                logger.error(
                    "수동 청산 실패: 실전 워커 없음: user_id=%s setting_id=%s symbol=%s",
                    self._user_id, setting_id, symbol,
                )
                try:
                    discord_user = await self._bot.fetch_user(int(self._user_id))
                    await discord_user.send(
                        f"⚠️ **수동 청산 실패** `{symbol}`\n"
                        "워커가 메모리에 없습니다. 봇이 재시작되었거나 워커가 비정상 종료되었을 수 있습니다.\n"
                        "`/설정` 커맨드에서 수동으로 중지 후 재시작해 주세요."
                    )
                except Exception as dm_exc:
                    logger.warning("에러 DM 발송 실패: user_id=%s err=%s", self._user_id, dm_exc)
                await interaction.followup.send(
                    f"청산 실패: `{symbol}` 워커가 실행 중이지 않습니다. 봇 재시작이 필요할 수 있습니다.",
                    ephemeral=True,
                )
                return

            try:
                success = await worker.force_sell(
                    reason="🖐️ 수동 청산 (Manual Override)",
                    close_type="MANUAL_OVERRIDE",
                )
            except Exception as exc:
                logger.error(
                    "수동 청산 force_sell 예외: user_id=%s setting_id=%s symbol=%s err=%s",
                    self._user_id, setting_id, symbol, exc,
                )
                await interaction.followup.send(
                    f"청산 중 오류 발생: `{exc}`", ephemeral=True
                )
                return

            if success:
                await interaction.followup.send(
                    f"✅ 청산 처리 완료 — `{symbol}` 체결 결과는 별도 DM으로 안내됩니다.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"청산 실패: `{symbol}` — 포지션이 없거나 현재가를 가져올 수 없습니다.",
                    ephemeral=True,
                )
            return

        # ── 모의 청산: 워커 있으면 force_sell, 없으면 DB 직접 처리 ──
        registry = WorkerRegistry.get()
        worker = registry.get_worker(setting_id)

        if worker is not None:
            try:
                success = await worker.force_sell(
                    reason="🖐️ 수동 청산 (Manual Override)",
                    close_type="MANUAL_OVERRIDE",
                )
                if success:
                    await interaction.followup.send(
                        f"✅ 청산 처리 완료 — [모의] `{symbol}` 체결 결과는 별도 DM으로 안내됩니다.",
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        f"[모의] `{symbol}` 청산 실패 — 포지션 없음 또는 현재가 조회 불가.",
                        ephemeral=True,
                    )
            except Exception as exc:
                logger.error(
                    "모의 수동 청산 force_sell 예외: user_id=%s setting_id=%s symbol=%s err=%s",
                    self._user_id, setting_id, symbol, exc,
                )
                await interaction.followup.send(
                    f"청산 중 오류 발생: `{exc}`", ephemeral=True
                )
            return

        # 워커 없음 — DB 직접 가상 청산
        logger.info(
            "모의 수동 청산 (워커 없음, DB 직접 처리): user_id=%s setting_id=%s symbol=%s",
            self._user_id, setting_id, symbol,
        )
        await self._paper_sell_direct(interaction, setting)

    # ------------------------------------------------------------------
    # 모의 직접 청산 (워커 없을 때 fallback)
    # ------------------------------------------------------------------

    async def _paper_sell_direct(
        self,
        interaction: discord.Interaction,
        setting: BotSetting,
    ) -> None:
        """모의투자 포지션을 워커 없이 DB에서 직접 청산한다."""
        symbol = setting.symbol
        buy_price = float(setting.buy_price)
        amount_coin = float(setting.amount_coin) if setting.amount_coin else 0.0

        current_price = UpbitWebsocketManager.get().get_price(symbol)
        if current_price is None:
            await interaction.followup.send(
                f"[모의] `{symbol}` 현재가를 가져올 수 없습니다. 잠시 후 다시 시도해주세요.",
                ephemeral=True,
            )
            return

        proceeds = current_price * amount_coin * 0.9995
        profit_pct = (current_price - buy_price) / buy_price * 100
        realized_pnl = proceeds - buy_price * amount_coin
        buy_amount_krw = float(setting.buy_amount_krw) if setting.buy_amount_krw else 0.0

        try:
            async with AsyncSessionLocal() as db:
                user_result = await db.execute(
                    select(User).where(User.user_id == self._user_id)
                )
                user = user_result.scalar_one_or_none()
                if user is not None:
                    user.virtual_krw = float(user.virtual_krw) + proceeds
                    await db.commit()
                    logger.info(
                        "[모의 수동 청산] 가상 잔고 복원: user=%s proceeds=%.0f balance=%.0f",
                        self._user_id, proceeds, user.virtual_krw,
                    )

            async with AsyncSessionLocal() as db:
                history = TradeHistory(
                    user_id=self._user_id,
                    symbol=symbol,
                    buy_price=buy_price,
                    sell_price=current_price,
                    profit_pct=profit_pct,
                    profit_krw=realized_pnl,
                    buy_amount_krw=buy_amount_krw,
                    is_paper_trading=True,
                    is_ai_managed=setting.is_ai_managed,
                    trade_style=setting.trade_style,
                    ai_score=setting.ai_score,
                    ai_reason=setting.ai_reason,
                    bought_at=setting.bought_at,
                    close_type="MANUAL_OVERRIDE",
                    ai_version=setting.ai_version or "v2.0",
                    expected_price=None,
                )
                db.add(history)
                await db.commit()

            async with AsyncSessionLocal() as db:
                bs_result = await db.execute(
                    select(BotSetting).where(
                        BotSetting.id == setting.id,
                        BotSetting.user_id == self._user_id,
                    )
                )
                bs = bs_result.scalar_one_or_none()
                if bs is not None:
                    bs.buy_price = None
                    bs.amount_coin = None
                    bs.is_running = False
                    await db.commit()

        except Exception as exc:
            logger.error(
                "[모의 수동 청산] DB 처리 실패: user_id=%s setting_id=%s err=%s",
                self._user_id, setting.id, exc,
            )
            await interaction.followup.send(
                f"[모의] 청산 중 DB 오류 발생: `{exc}`", ephemeral=True
            )
            return

        registry = WorkerRegistry.get()
        worker = registry.get_worker(setting.id)
        if worker is not None:
            worker.stop()
            registry._workers.pop(setting.id, None)

        icon = "🟢" if realized_pnl >= 0 else "🔴"
        await interaction.followup.send(
            f"✅ 청산 처리 완료\n"
            f"{icon} **[모의 수동 청산]** `{symbol}`\n"
            f"매수가: {buy_price:,.0f} KRW → 매도가: {current_price:,.0f} KRW\n"
            f"수익률: **{profit_pct:+.2f}%** ({realized_pnl:+,.0f} KRW)",
            ephemeral=True,
        )

        try:
            discord_user = await self._bot.fetch_user(int(self._user_id))
            await discord_user.send(
                f"{icon} **[🎮 모의투자] 수동 매도 체결** `{symbol}` — 🖐️ 수동 청산 (Manual Override)\n"
                f"매수가: {buy_price:,.0f} KRW  →  매도가: {current_price:,.0f} KRW\n"
                f"수익률: **{profit_pct:+.2f}%**  |  손익: **{realized_pnl:+,.0f} KRW**"
            )
        except Exception as dm_exc:
            logger.warning(
                "모의 수동 청산 DM 알림 실패: user_id=%s err=%s", self._user_id, dm_exc
            )

    # ------------------------------------------------------------------
    # View 만료 처리
    # ------------------------------------------------------------------

    async def on_timeout(self) -> None:
        logger.debug("ManualSellView 타임아웃 (5분 만료): user_id=%s", self._user_id)
