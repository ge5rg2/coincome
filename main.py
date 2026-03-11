"""
CoinCome 전체 진입점.
Discord 봇과 FastAPI 서버를 단일 프로세스에서 동시에 실행.

실행 방법:
    python main.py
"""
from __future__ import annotations

import asyncio
import logging
import threading

import uvicorn

from app.api.main import app as fastapi_app
from app.bot.main import bot
from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def run_fastapi() -> None:
    """FastAPI 서버를 별도 스레드에서 실행"""
    uvicorn.run(
        fastapi_app,
        host=settings.app_host,
        port=settings.app_port,
        log_level="info",
    )


async def main() -> None:
    # FastAPI를 백그라운드 스레드에서 기동
    api_thread = threading.Thread(target=run_fastapi, daemon=True)
    api_thread.start()
    logger.info("FastAPI 서버 시작: http://%s:%s", settings.app_host, settings.app_port)

    # Discord 봇 실행
    await bot.start(settings.discord_bot_token)


if __name__ == "__main__":
    asyncio.run(main())
