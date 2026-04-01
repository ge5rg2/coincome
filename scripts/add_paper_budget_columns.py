"""모의투자 전용 예산 컬럼 추가 마이그레이션 스크립트.

실행 방법:
    python -m scripts.add_paper_budget_columns

수행하는 작업:
  [ADD] users 테이블
    - ai_paper_engine_mode      VARCHAR(10)  DEFAULT 'SWING'   — 모의투자 전용 엔진 모드
    - ai_paper_swing_budget_krw INTEGER      DEFAULT 1000000   — 모의 스윙 예산 한도
    - ai_paper_scalp_budget_krw INTEGER      DEFAULT 1000000   — 모의 스캘핑 예산 한도
    - ai_paper_major_budget     INTEGER      DEFAULT 0         — 모의 MAJOR 예산 한도

  [UPDATE] 기존 데이터 마이그레이션:
    - ai_paper_mode_enabled=TRUE 인 유저의 현재 실전 예산값을 모의 컬럼 초기값으로 복사

컬럼 존재 여부를 사전 확인하여 이미 적용된 환경에서 안전하게 재실행 가능 (idempotent).
"""
import asyncio
import logging
import sys

from sqlalchemy import text

from app.database import AsyncSessionLocal

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── users 테이블에 추가할 컬럼 정의 ─────────────────────────────────
_USERS_COLUMNS: list[dict] = [
    {
        "column": "ai_paper_engine_mode",
        "ddl": "ALTER TABLE users ADD COLUMN ai_paper_engine_mode VARCHAR(10) DEFAULT 'SWING'",
    },
    {
        "column": "ai_paper_swing_budget_krw",
        "ddl": "ALTER TABLE users ADD COLUMN ai_paper_swing_budget_krw INTEGER DEFAULT 1000000",
    },
    {
        "column": "ai_paper_scalp_budget_krw",
        "ddl": "ALTER TABLE users ADD COLUMN ai_paper_scalp_budget_krw INTEGER DEFAULT 1000000",
    },
    {
        "column": "ai_paper_major_budget",
        "ddl": "ALTER TABLE users ADD COLUMN ai_paper_major_budget INTEGER DEFAULT 0",
    },
]


async def _column_exists(session, table: str, column: str) -> bool:
    """PostgreSQL information_schema로 컬럼 존재 여부를 확인한다."""
    result = await session.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name = :tbl AND column_name = :col"
        ),
        {"tbl": table, "col": column},
    )
    return result.scalar() > 0


async def _add_columns(
    db,
    table: str,
    columns: list[dict],
    errors: list[str],
) -> None:
    """지정 테이블에 컬럼 목록을 순차적으로 추가한다."""
    for item in columns:
        col = item["column"]
        if await _column_exists(db, table, col):
            logger.info("  SKIP  %s.%s — 이미 존재합니다.", table, col)
            continue
        try:
            await db.execute(text(item["ddl"]))
            await db.commit()
            logger.info("  OK    %s.%s 추가 완료", table, col)
        except Exception as exc:
            await db.rollback()
            msg = "%s.%s 추가 실패: %s" % (table, col, exc)
            logger.error("  FAIL  %s", msg)
            errors.append(msg)


async def _migrate_existing_paper_users(db, errors: list[str]) -> None:
    """이미 모의투자 활성인 유저의 실전 예산값을 모의 전용 컬럼으로 복사한다.

    ai_paper_mode_enabled=TRUE 인 유저에 한해 현재 실전 예산값을 초기값으로 복사하여
    기존 사용자가 모의투자 설정을 잃지 않도록 보호한다.
    """
    try:
        await db.execute(
            text(
                "UPDATE users "
                "SET ai_paper_engine_mode = ai_engine_mode, "
                "    ai_paper_swing_budget_krw = ai_swing_budget_krw, "
                "    ai_paper_scalp_budget_krw = ai_scalp_budget_krw, "
                "    ai_paper_major_budget = major_budget "
                "WHERE ai_paper_mode_enabled = TRUE"
            )
        )
        await db.commit()
        logger.info(
            "  UPDATE  ai_paper_mode_enabled=TRUE 유저 기존 예산값 복사 완료"
        )
    except Exception as exc:
        await db.rollback()
        msg = "기존 모의 유저 데이터 마이그레이션 실패: %s" % exc
        logger.error("  FAIL  %s", msg)
        errors.append(msg)


async def main() -> None:
    """마이그레이션 메인 로직."""
    logger.info("=" * 60)
    logger.info("모의투자 전용 예산 컬럼 추가 마이그레이션")
    logger.info("대상: users (ai_paper_* 컬럼 4개 추가 + 기존 모의 유저 데이터 복사)")
    logger.info("=" * 60)

    errors: list[str] = []

    async with AsyncSessionLocal() as db:
        # ── 1. users 테이블 컬럼 추가 ────────────────────────────────
        logger.info("[ADD] users 테이블 모의투자 전용 컬럼 추가 시작 ...")
        await _add_columns(db, "users", _USERS_COLUMNS, errors)

        if errors:
            logger.error("컬럼 추가 중 오류 발생 — 데이터 마이그레이션 단계를 건너뜁니다.")
        else:
            # ── 2. 기존 모의투자 활성 유저 데이터 복사 ───────────────
            logger.info("[UPDATE] 기존 모의투자 활성 유저 예산값 복사 시작 ...")
            await _migrate_existing_paper_users(db, errors)

    logger.info("=" * 60)
    if errors:
        logger.error("마이그레이션 중 %d개 오류 발생:", len(errors))
        for e in errors:
            logger.error("  - %s", e)
        sys.exit(1)
    else:
        logger.info("마이그레이션 완료! 모든 작업이 성공적으로 처리되었습니다.")


if __name__ == "__main__":
    asyncio.run(main())
