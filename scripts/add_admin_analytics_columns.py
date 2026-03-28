"""Admin 분석용 컬럼 추가 마이그레이션 스크립트.

실행 방법:
    python -m scripts.add_admin_analytics_columns

수행하는 작업:
  [ADD] trade_history 테이블
    - bought_at      TIMESTAMPTZ           — 매수 체결 시각 (BotSetting에서 이관)
    - close_type     VARCHAR(50)           — 청산 유형 (TP_HIT / SL_HIT / AI_FORCE_SELL / MANUAL_OVERRIDE)
    - ai_version     VARCHAR(20) DEFAULT 'v2.0' — AI 전략 버전
    - expected_price DOUBLE PRECISION      — 익절/손절 목표 단가 (슬리피지 추적용)

  [ADD] bot_settings 테이블
    - bought_at  TIMESTAMPTZ           — 매수 체결 시각 (청산 시 TradeHistory로 이관)
    - ai_version VARCHAR(20) DEFAULT 'v2.0' — AI 전략 버전 태그

컬럼 존재 여부를 사전 확인하여 이미 적용된 환경에서 안전하게 재실행 가능 (idempotent).
"""
import asyncio
import logging
import sys

from sqlalchemy import text

from app.database import AsyncSessionLocal

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── trade_history 테이블에 추가할 컬럼 정의 ─────────────────────────
_TRADE_HISTORY_COLUMNS: list[dict] = [
    {
        "column": "bought_at",
        "ddl": "ALTER TABLE trade_history ADD COLUMN bought_at TIMESTAMPTZ",
    },
    {
        "column": "close_type",
        "ddl": "ALTER TABLE trade_history ADD COLUMN close_type VARCHAR(50)",
    },
    {
        "column": "ai_version",
        "ddl": "ALTER TABLE trade_history ADD COLUMN ai_version VARCHAR(20) DEFAULT 'v2.0'",
    },
    {
        "column": "expected_price",
        "ddl": "ALTER TABLE trade_history ADD COLUMN expected_price DOUBLE PRECISION",
    },
]

# ── bot_settings 테이블에 추가할 컬럼 정의 ──────────────────────────
_BOT_SETTINGS_COLUMNS: list[dict] = [
    {
        "column": "bought_at",
        "ddl": "ALTER TABLE bot_settings ADD COLUMN bought_at TIMESTAMPTZ",
    },
    {
        "column": "ai_version",
        "ddl": "ALTER TABLE bot_settings ADD COLUMN ai_version VARCHAR(20) DEFAULT 'v2.0'",
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
            msg = f"{table}.{col} 추가 실패: {exc}"
            logger.error("  FAIL  %s", msg)
            errors.append(msg)


async def main() -> None:
    """마이그레이션 메인 로직."""
    logger.info("=" * 60)
    logger.info("Admin 분석용 컬럼 추가 마이그레이션")
    logger.info("대상: trade_history (4개), bot_settings (2개)")
    logger.info("=" * 60)

    errors: list[str] = []

    async with AsyncSessionLocal() as db:
        # ── 1. trade_history 컬럼 추가 ────────────────────────────────
        logger.info("[ADD] trade_history 컬럼 추가 시작 ...")
        await _add_columns(db, "trade_history", _TRADE_HISTORY_COLUMNS, errors)

        # ── 2. bot_settings 컬럼 추가 ─────────────────────────────────
        logger.info("[ADD] bot_settings 컬럼 추가 시작 ...")
        await _add_columns(db, "bot_settings", _BOT_SETTINGS_COLUMNS, errors)

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
