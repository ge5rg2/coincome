"""
ManualSellView: AI 종합 리포트 하단에 첨부되는 수동 청산(Manual Override) UI.

사용자가 DM으로 받은 AI 리포트에서 Select Menu로 포지션을 선택하고
[즉시 청산] 버튼을 눌러 수동으로 포지션을 청산할 수 있다.

설계 원칙:
  - timeout=300초 (5분): 봇 재시작 시 만료 처리
  - Race Condition 방지: 버튼 콜백에서 DB 재조회 후 is_running + buy_price 재검증
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
    """AI 리포트에 부착되는 수동 청산 UI.

    Attributes:
        bot:       Discord 봇 인스턴스 (user.send DM 발송 등에 사용).
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
        self._selected_id: int | None = None

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
                    label=label[:100],  # Discord 옵션 레이블 최대 100자
                    value=str(pos["setting_id"]),
                    emoji=emoji,
                    description=f"setting_id={pos['setting_id']}",
                )
            )

        select_menu = discord.ui.Select(
            placeholder="청산할 코인을 선택하세요...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="manual_sell_select",
        )
        select_menu.callback = self._on_select
        self.add_item(select_menu)

        # ── 즉시 청산 버튼 ────────────────────────────────────────────
        sell_button = discord.ui.Button(
            label="즉시 청산",
            style=discord.ButtonStyle.danger,
            emoji="🚨",
            custom_id="manual_sell_execute",
        )
        sell_button.callback = self._on_sell_button
        self.add_item(sell_button)

    # ------------------------------------------------------------------
    # Select 콜백 — 청산 대상 선택
    # ------------------------------------------------------------------

    async def _on_select(self, interaction: discord.Interaction) -> None:
        """Select Menu 선택 콜백: 선택된 setting_id를 내부 상태에 저장한다."""
        # 선택한 사용자가 본인인지 확인
        if str(interaction.user.id) != self._user_id:
            await interaction.response.send_message(
                "본인의 리포트에서만 청산할 수 있습니다.",
                ephemeral=True,
            )
            return

        raw_value = interaction.data["values"][0]
        try:
            self._selected_id = int(raw_value)
        except (ValueError, KeyError):
            await interaction.response.send_message(
                "선택값이 올바르지 않습니다. 다시 시도해주세요.",
                ephemeral=True,
            )
            return

        # 선택 확인 응답 (deferred)
        selected_pos = next(
            (p for p in self._positions if p["setting_id"] == self._selected_id),
            None,
        )
        symbol_str = selected_pos["symbol"] if selected_pos else raw_value
        mode_str = "[모의]" if (selected_pos and selected_pos["is_paper"]) else "[실전]"
        await interaction.response.send_message(
            f"{mode_str} **{symbol_str}** 선택됨 — [즉시 청산] 버튼을 눌러 청산을 실행하세요.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # 버튼 콜백 — 수동 청산 실행
    # ------------------------------------------------------------------

    async def _on_sell_button(self, interaction: discord.Interaction) -> None:
        """[즉시 청산] 버튼 콜백: 선택된 포지션을 즉시 청산한다.

        Race Condition 방지를 위해 DB에서 BotSetting을 재조회하여
        is_running=True 및 buy_price IS NOT NULL을 검증한 뒤 청산을 진행한다.
        """
        # 권한 검증: 본인만 사용 가능
        if str(interaction.user.id) != self._user_id:
            await interaction.response.send_message(
                "본인의 리포트에서만 청산할 수 있습니다.",
                ephemeral=True,
            )
            return

        # Select가 선택되지 않은 경우
        if self._selected_id is None:
            await interaction.response.send_message(
                "먼저 청산할 코인을 선택해주세요.",
                ephemeral=True,
            )
            return

        setting_id = self._selected_id

        # ── Ghost Update 방지: DB 재조회 및 상태 재검증 ──────────────
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(BotSetting).where(BotSetting.id == setting_id)
                )
                setting = result.scalar_one_or_none()
        except Exception as exc:
            logger.error(
                "수동 청산 DB 조회 실패: user_id=%s setting_id=%s err=%s",
                self._user_id, setting_id, exc,
            )
            await interaction.response.send_message(
                "DB 조회 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
                ephemeral=True,
            )
            return

        if setting is None:
            await interaction.response.send_message(
                "포지션 정보를 찾을 수 없습니다. 이미 삭제되었을 수 있습니다.",
                ephemeral=True,
            )
            return

        if not setting.is_running:
            await interaction.response.send_message(
                "이미 중지된 포지션입니다.",
                ephemeral=True,
            )
            return

        if setting.buy_price is None:
            await interaction.response.send_message(
                "이미 청산된 포지션입니다. (매수 기록 없음)",
                ephemeral=True,
            )
            return

        # 청산 대상 정보
        symbol = setting.symbol
        is_paper = setting.is_paper_trading

        # ── 즉시 defer: 청산이 오래 걸릴 수 있으므로 먼저 응답 ───────
        await interaction.response.defer(ephemeral=True)

        # ── View 비활성화 (중복 청산 방지) ───────────────────────────
        self.stop()

        # ── 실전 청산: TradingWorker.force_sell() 재사용 ─────────────
        if not is_paper:
            registry = WorkerRegistry.get()
            worker = registry.get_worker(setting_id)

            if worker is None:
                # 실전인데 워커가 메모리에 없음 — 에러 DM 발송 후 안내
                logger.error(
                    "수동 청산 실패: 실전 워커 없음: user_id=%s setting_id=%s symbol=%s",
                    self._user_id, setting_id, symbol,
                )
                try:
                    discord_user = await self._bot.fetch_user(int(self._user_id))
                    await discord_user.send(
                        f"⚠️ **수동 청산 실패** `{symbol}`\n"
                        f"워커가 메모리에 없습니다. 봇이 재시작되었거나 워커가 비정상 종료되었을 수 있습니다.\n"
                        f"`/설정` 커맨드에서 수동으로 중지 후 재시작해 주세요."
                    )
                except Exception as dm_exc:
                    logger.warning("에러 DM 발송 실패: user_id=%s err=%s", self._user_id, dm_exc)
                await interaction.followup.send(
                    f"청산 실패: `{symbol}` 워커가 실행 중이지 않습니다. 봇 재시작이 필요할 수 있습니다.",
                    ephemeral=True,
                )
                return

            try:
                success = await worker.force_sell(reason="🖐️ 수동 청산 (Manual Override)")
            except Exception as exc:
                logger.error(
                    "수동 청산 force_sell 예외: user_id=%s setting_id=%s symbol=%s err=%s",
                    self._user_id, setting_id, symbol, exc,
                )
                await interaction.followup.send(
                    f"청산 중 오류 발생: `{exc}`",
                    ephemeral=True,
                )
                return

            if success:
                await interaction.followup.send(
                    f"청산 요청이 완료되었습니다. `{symbol}` 체결 결과는 별도 DM으로 안내됩니다.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"청산 실패: `{symbol}` — 포지션이 없거나 현재가를 가져올 수 없습니다.",
                    ephemeral=True,
                )
            return

        # ── 모의 청산 fallback: DB 직접 처리 ─────────────────────────
        # 워커가 있으면 force_sell 재사용, 없으면 직접 DB 처리
        registry = WorkerRegistry.get()
        worker = registry.get_worker(setting_id)

        if worker is not None:
            try:
                success = await worker.force_sell(reason="🖐️ 수동 청산 (Manual Override)")
                if success:
                    await interaction.followup.send(
                        f"[모의] `{symbol}` 청산 요청 완료. 체결 결과는 별도 DM으로 안내됩니다.",
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
                    f"청산 중 오류 발생: `{exc}`",
                    ephemeral=True,
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
        """모의투자 포지션을 워커 없이 DB에서 직접 청산한다.

        흐름:
          1. 현재가 조회 (WebSocket 캐시)
          2. 가상 청산 대금 계산
          3. User.virtual_krw += proceeds (DB)
          4. TradeHistory INSERT
          5. BotSetting 포지션 초기화 (buy_price/amount_coin=None, is_running=False)
        """
        symbol = setting.symbol
        buy_price = float(setting.buy_price)
        amount_coin = float(setting.amount_coin) if setting.amount_coin else 0.0

        # 1. 현재가 조회
        current_price = UpbitWebsocketManager.get().get_price(symbol)
        if current_price is None:
            await interaction.followup.send(
                f"[모의] `{symbol}` 현재가를 가져올 수 없습니다. 잠시 후 다시 시도해주세요.",
                ephemeral=True,
            )
            return

        # 2. 청산 대금 계산
        proceeds = current_price * amount_coin * 0.9995
        profit_pct = (current_price - buy_price) / buy_price * 100
        realized_pnl = proceeds - buy_price * amount_coin
        buy_amount_krw = float(setting.buy_amount_krw) if setting.buy_amount_krw else 0.0

        try:
            # 3. 가상 잔고 복원
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

            # 4. 거래 이력 INSERT
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
                )
                db.add(history)
                await db.commit()

            # 5. BotSetting 포지션 초기화
            async with AsyncSessionLocal() as db:
                bs_result = await db.execute(
                    select(BotSetting).where(BotSetting.id == setting.id)
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
                f"[모의] 청산 중 DB 오류 발생: `{exc}`",
                ephemeral=True,
            )
            return

        # WorkerRegistry에서 워커 정리 (혹시 등록되어 있으면)
        registry = WorkerRegistry.get()
        worker = registry.get_worker(setting.id)
        if worker is not None:
            worker.stop()
            registry._workers.pop(setting.id, None)

        # 성공 응답 + DM 알림
        icon = "🟢" if realized_pnl >= 0 else "🔴"
        await interaction.followup.send(
            f"{icon} **[모의 수동 청산]** `{symbol}` 완료\n"
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
        """View 타임아웃 (5분) 시 조용히 처리한다."""
        logger.debug(
            "ManualSellView 타임아웃 (5분 만료): user_id=%s", self._user_id
        )
