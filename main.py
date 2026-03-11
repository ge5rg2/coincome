"""
CoinCome 전체 진입점.
Discord 봇과 FastAPI 서버를 단일 이벤트 루프에서 동시에 실행.
"""
from __future__ import annotations

import asyncio
import logging
import os
import certifi
import ssl

# [Mac 환경 SSL 인증서 에러 강제 우회 설정]
os.environ["SSL_CERT_FILE"] = certifi.where()
ssl._create_default_https_context = ssl._create_unverified_context

import uvicorn
from app.api.main import app as fastapi_app
from app.bot.main import bot
from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def run_fastapi() -> None:
    """FastAPI 서버를 비동기 루프 내에서 실행"""
    config = uvicorn.Config(
        app=fastapi_app,
        host=settings.app_host,
        port=settings.app_port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main() -> None:
    # asyncio.gather를 사용해 두 서버를 하나의 루프 안에서 동시에 실행합니다.
    # 이렇게 하면 DB 연결 풀과 웹소켓 이벤트가 충돌하지 않습니다.
    logger.info("FastAPI 및 Discord 봇 동시 시작 준비...")
    
    await asyncio.gather(
        run_fastapi(),
        bot.start(settings.discord_bot_token)
    )

if __name__ == "__main__":
    # Windows/Mac 환경 비동기 루프 에러 방지
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    asyncio.run(main())