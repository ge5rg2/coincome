"""등급별 엔진 수 제한 컬럼 추가 마이그레이션 스크립트.

실행 방법:
    python -m scripts.add_engine_tier_columns

수행하는 작업:
  [ADD] users 테이블
    - max_active_engines  INTEGER DEFAULT 1  — 등급별 활성 가능 AI 엔진 수

  [UPDATE] 기존 유저 등급별 일괄 설정:
    - FREE → max_active_engines = 0  (AI 트레이딩 불가)
    - PRO  → max_active_engines = 1  (알트 엔진 1개)
    - VIP  → max_active_engines = 3  (전체 엔진 허용)

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
        "column": "max_active_engines",
        "ddl": "ALTER TABLE users ADD COLUMN max_active_engines INTEGER DEFAULT 0",
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


async def _update_tier_defaults(db, errors: list[str]) -> None:
    """기존 유저의 등급에 맞게 max_active_engines 를 일괄 UPDATE한다.

    컬럼이 DEFAULT 1 로 생성되므로 FREE 유저만 0으로 덮어써야 하지만,
    명확성을 위해 3개 등급 모두 명시적으로 UPDATE한다.
    """
    updates = [
        ("FREE", 0),
        ("PRO", 1),
        ("VIP", 3),
    ]
    for tier, count in updates:
        try:
            await db.execute(
                text(
                    "UPDATE users SET max_active_engines = :count "
                    "WHERE subscription_tier = :tier"
                ),
                {"count": count, "tier": tier},
            )
            await db.commit()
            logger.info("  UPDATE  users SET max_active_engines=%d WHERE subscription_tier='%s'", count, tier)
        except Exception as exc:
            await db.rollback()
            msg = "UPDATE users (tier=%s) 실패: %s" % (tier, exc)
            logger.error("  FAIL  %s", msg)
            errors.append(msg)


async def main() -> None:
    """마이그레이션 메인 로직."""
    logger.info("=" * 60)
    logger.info("등급별 엔진 수 제한 컬럼 추가 마이그레이션")
    logger.info("대상: users (max_active_engines 컬럼 추가 + 등급별 UPDATE)")
    logger.info("=" * 60)

    errors: list[str] = []

    async with AsyncSessionLocal() as db:
        # ── 1. users 테이블 컬럼 추가 ────────────────────────────────
        logger.info("[ADD] users.max_active_engines 컬럼 추가 시작 ...")
        await _add_columns(db, "users", _USERS_COLUMNS, errors)

        if errors:
            logger.error("컬럼 추가 중 오류 발생 — UPDATE 단계를 건너뜁니다.")
        else:
            # ── 2. 등급별 일괄 UPDATE ─────────────────────────────────
            logger.info("[UPDATE] 등급별 max_active_engines 일괄 설정 시작 ...")
            await _update_tier_defaults(db, errors)

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
