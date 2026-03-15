"""DB 마이그레이션: AI 리뉴얼 관련 컬럼 추가.

대상 컬럼:
  bot_settings.is_ai_managed  BOOLEAN NOT NULL DEFAULT FALSE
    — AI 펀드 매니저가 자동 생성한 BotSetting과 수동 포지션을 구분하는 플래그.
    — True 인 레코드만 AI 포지션 리뷰·슬롯 카운트 대상으로 삼는다.

실행 방법 (로컬):
    python -m scripts.add_ai_renewal_columns

실행 방법 (Docker):
    docker-compose run --rm app python -m scripts.add_ai_renewal_columns
"""
import asyncio
import logging

from sqlalchemy import text

from app.database import AsyncSessionLocal

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# (테이블명.컬럼명, DDL) 튜플 목록
_COLUMNS: list[tuple[str, str]] = [
    (
        "bot_settings.is_ai_managed",
        "ALTER TABLE bot_settings "
        "ADD COLUMN is_ai_managed BOOLEAN NOT NULL DEFAULT FALSE",
    ),
]


async def run() -> None:
    """마이그레이션 메인 함수: 각 컬럼이 없을 때만 추가한다."""
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
