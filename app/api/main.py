"""FastAPI 앱 팩토리"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routers import payments
from app.database import engine
from app.models import BotSetting, Payment, User  # noqa: F401 — Alembic 인식용


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 앱 시작 시 테이블 자동 생성 (개발 편의용; 운영에서는 Alembic 사용)
    from app.database import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="CoinCome API", version="0.1.0", lifespan=lifespan)
    app.include_router(payments.router)
    return app


app = create_app()
