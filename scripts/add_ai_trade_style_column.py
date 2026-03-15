"""DB 마이그레이션: AI 투자 성향 컬럼 추가.

대상 컬럼:
  users.ai_trade_style  VARCHAR(20) NOT NULL DEFAULT 'SWING'
    - SWING    : 4시간 봉 기반 보수적 스윙 매매 (기존 동작)
    - SCALPING : 1시간 봉 기반 공격적 단타/모멘텀 매매

실행 방법 (로컬):
    python -m scripts.add_ai_trade_style_column

실행 방법 (Docker):
    docker-compose run --rm app python -m scripts.add_ai_trade_style_column
"""
import asyncio
import logging

from sqlalchemy import text

from app.database import AsyncSessionLocal

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

_COLUMNS: list[tuple[str, str]] = [
    (
        "users.ai_trade_style",
        "ALTER TABLE users ADD COLUMN ai_trade_style VARCHAR(20) NOT NULL DEFAULT 'SWING'",
    ),
]


async def run() -> None:
    """마이그레이션 메인 함수: 컬럼이 없을 때만 추가한다."""
    async with AsyncSessionLocal() as db:
        for col_label, ddl in _COLUMNS:
            try:
                await db.execute(text(ddl))
                await db.commit()
                logger.info("컬럼 추가 완료: %s", col_label)
            except Exception as exc:
                await db.rollback()
                err_msg = str(exc).lower()
                if "already exists" in err_msg or "duplicate column" in err_msg:
                    logger.info("컬럼 이미 존재, 스킵: %s", col_label)
                else:
                    logger.error("컬럼 추가 실패: %s — %s", col_label, exc)
                    raise


if __name__ == "__main__":
    asyncio.run(run())
