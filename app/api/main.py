"""FastAPI 앱 팩토리"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.routers import payments, web
from app.database import AsyncSessionLocal, Base, engine
from app.models import BotSetting, Payment, User  # noqa: F401 — Alembic 인식용
from app.services.exchange import ExchangeService
from app.services.market_data import MarketDataManager
from app.services.trading_worker import TradingWorker, WorkerRegistry
from app.services.websocket import UpbitWebsocketManager

logger = logging.getLogger(__name__)


async def _recover_workers() -> None:
    """서버 재시작 시 DB에서 is_running=True 인 BotSetting을 조회해 워커를 복구한다.

    buy_price / amount_coin 이 NULL 인 레코드는 아직 매수 전이므로 즉시 매수를,
    값이 존재하는 레코드는 TradingWorker._decide_entry() 에서 포지션을 복원한다.
    Discord 봇이 아직 준비되지 않았을 수 있으므로 알림 콜백은 봇 준비 후 전송한다.
    """
    from app.bot.main import bot  # 순환 임포트 방지를 위해 지연 임포트

    async def _safe_notify(user_id: str, msg: str) -> None:
        """Discord 봇이 준비된 경우에만 DM을 전송한다."""
        if bot.is_ready():
            await bot._send_dm(user_id, msg)
        else:
            logger.warning("봇 미준비 — 알림 스킵: user_id=%s msg=%.40s", user_id, msg)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(BotSetting)
            .where(BotSetting.is_running == True)  # noqa: E712
            .options(selectinload(BotSetting.user))
        )
        settings = result.scalars().all()

    if not settings:
        logger.info("복구할 실행 중 워커 없음.")
        return

    logger.info("워커 복구 시작: %d 개 레코드", len(settings))
    registry = WorkerRegistry.get()

    for s in settings:
        user = s.user
        if user is None:
            logger.warning("User 레코드 없음, 복구 스킵: setting_id=%s", s.id)
            continue

        exchange = ExchangeService(
            access_key=user.upbit_access_key or "",
            secret_key=user.upbit_secret_key or "",
        )
        worker = TradingWorker(
            setting_id=s.id,
            user_id=s.user_id,
            symbol=s.symbol,
            buy_amount_krw=float(s.buy_amount_krw),
            target_profit_pct=float(s.target_profit_pct) if s.target_profit_pct is not None else None,
            stop_loss_pct=float(s.stop_loss_pct) if s.stop_loss_pct is not None else None,
            exchange=exchange,
            notify_callback=_safe_notify,
        )
        await registry.register(worker)
        worker.start()
        logger.info(
            "워커 복구 완료: setting_id=%s user_id=%s symbol=%s",
            s.id, s.user_id, s.symbol,
        )

    logger.info("전체 워커 복구 완료: %d 개", len(settings))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. 테이블 자동 생성 (개발 편의용; 운영에서는 Alembic 사용)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 2. 업비트 WebSocket 매니저 시작
    #    DB에서 is_running=True 심볼을 로드해 즉시 구독을 시작한다.
    ws_manager = UpbitWebsocketManager.get()
    ws_manager.start()

    # 3. DB에서 is_running=True 워커 복구 (포지션 유지 또는 신규 매수)
    await _recover_workers()

    # 4. 시장 데이터 캐싱 매니저 시작 (AI 트레이딩용 Top N 스크리닝 · 지표 계산)
    #    기동 직후 첫 갱신을 비동기 실행 — 완료 전에도 봇은 정상 동작.
    market_data = MarketDataManager.get()
    market_data.start()

    yield

    # 5. 종료 시 WebSocket 태스크 및 시장 데이터 루프 정리
    ws_manager.stop()
    market_data.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="CoinCome API", version="0.1.0", lifespan=lifespan)
    app.include_router(payments.router)
    app.include_router(web.router)
    return app


app = create_app()
