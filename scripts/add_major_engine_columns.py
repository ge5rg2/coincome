"""V2 대청소 마이그레이션 스크립트 — MAJOR 엔진 컬럼 추가 + V1 레거시 컬럼 DROP.

실행 방법:
    python -m scripts.add_major_engine_columns

수행하는 작업:
  [ADD] users 테이블
    - is_major_enabled  BOOLEAN   DEFAULT FALSE  (MAJOR Trend Catcher 엔진 ON/OFF)
    - major_budget      INTEGER   DEFAULT 0      (MAJOR 엔진 운용 예산 한도, KRW)
    - major_trade_ratio INTEGER   DEFAULT 10     (MAJOR 1회 진입 비중 %, 10~100)

  [DROP] users 테이블 (V1 레거시 컬럼 물리적 삭제)
    - ai_trade_style  — V2에서 ai_engine_mode 로 대체됨
    - ai_budget_krw   — V2에서 ai_swing/scalp_budget_krw 로 분리됨
    - ai_trade_amount — V2에서 예산 × 비중으로 동적 산출, 별도 저장 불필요

컬럼 존재 여부를 사전 확인하여 이미 적용된 환경에서 안전하게 재실행 가능 (idempotent).
"""
import asyncio
import logging
import sys

from sqlalchemy import text

from app.database import AsyncSessionLocal

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── 추가할 컬럼 정의 ────────────────────────────────────────────────
_ADD_COLUMNS: list[dict] = [
    {
        "column": "is_major_enabled",
        "ddl": "ALTER TABLE users ADD COLUMN is_major_enabled BOOLEAN NOT NULL DEFAULT FALSE",
    },
    {
        "column": "major_budget",
        "ddl": "ALTER TABLE users ADD COLUMN major_budget INTEGER NOT NULL DEFAULT 0",
    },
    {
        "column": "major_trade_ratio",
        "ddl": "ALTER TABLE users ADD COLUMN major_trade_ratio INTEGER NOT NULL DEFAULT 10",
    },
]

# ── 삭제할 V1 레거시 컬럼 ────────────────────────────────────────────
_DROP_COLUMNS: list[str] = [
    "ai_trade_style",
    "ai_budget_krw",
    "ai_trade_amount",
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


async def main() -> None:
    """마이그레이션 메인 로직."""
    logger.info("=" * 60)
    logger.info("V2 대청소 마이그레이션: MAJOR 엔진 컬럼 추가 + V1 레거시 DROP")
    logger.info("=" * 60)

    errors: list[str] = []

    async with AsyncSessionLocal() as db:
        # ── 1. MAJOR 엔진 컬럼 추가 ───────────────────────────────────
        logger.info("[ADD] MAJOR 엔진 컬럼 추가 시작 ...")
        for item in _ADD_COLUMNS:
            col = item["column"]
            if await _column_exists(db, "users", col):
                logger.info("  SKIP  users.%s — 이미 존재합니다.", col)
                continue
            try:
                await db.execute(text(item["ddl"]))
                await db.commit()
                logger.info("  OK    users.%s 추가 완료", col)
            except Exception as exc:
                await db.rollback()
                msg = f"users.{col} 추가 실패: {exc}"
                logger.error("  FAIL  %s", msg)
                errors.append(msg)

        # ── 2. V1 레거시 컬럼 DROP ────────────────────────────────────
        logger.info("[DROP] V1 레거시 컬럼 삭제 시작 ...")
        for col in _DROP_COLUMNS:
            if not await _column_exists(db, "users", col):
                logger.info("  SKIP  users.%s — 이미 존재하지 않습니다.", col)
                continue
            try:
                await db.execute(text(f"ALTER TABLE users DROP COLUMN {col}"))
                await db.commit()
                logger.info("  OK    users.%s 삭제 완료", col)
            except Exception as exc:
                await db.rollback()
                msg = f"users.{col} 삭제 실패: {exc}"
                logger.error("  FAIL  %s", msg)
                errors.append(msg)

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
