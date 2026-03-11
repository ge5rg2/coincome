"""FastAPI 앱 팩토리"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routers import payments, web
from app.database import engine
from app.models import BotSetting, Payment, User  # noqa: F401 — Alembic 인식용
from app.services.websocket import UpbitWebsocketManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. 테이블 자동 생성 (개발 편의용; 운영에서는 Alembic 사용)
    from app.database import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 2. 업비트 WebSocket 매니저 시작
    #    DB에서 is_running=True 심볼을 로드해 즉시 구독을 시작한다.
    ws_manager = UpbitWebsocketManager.get()
    ws_manager.start()

    yield

    # 3. 종료 시 WebSocket 태스크 정리
    ws_manager.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="CoinCome API", version="0.1.0", lifespan=lifespan)
    app.include_router(payments.router)
    app.include_router(web.router)
    return app


app = create_app()
