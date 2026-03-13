"""
One-time DB migration: users 테이블에 정기 보고 관련 컬럼 3개 추가.

추가 컬럼:
  - report_enabled        BOOLEAN NOT NULL DEFAULT TRUE
  - report_interval_hours INTEGER NOT NULL DEFAULT 1
  - last_report_sent_at   TIMESTAMPTZ (nullable)

실행 방법:
  python -m scripts.add_report_columns

멱등 실행 가능: 이미 컬럼이 존재하면 안전하게 건너뜁니다.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.database import engine

_ADD_COLUMNS_SQL = """
DO $$
BEGIN
    -- report_enabled
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'report_enabled'
    ) THEN
        ALTER TABLE users ADD COLUMN report_enabled BOOLEAN NOT NULL DEFAULT TRUE;
        RAISE NOTICE 'Added column: report_enabled';
    ELSE
        RAISE NOTICE 'Column already exists (skip): report_enabled';
    END IF;

    -- report_interval_hours
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'report_interval_hours'
    ) THEN
        ALTER TABLE users ADD COLUMN report_interval_hours INTEGER NOT NULL DEFAULT 1;
        RAISE NOTICE 'Added column: report_interval_hours';
    ELSE
        RAISE NOTICE 'Column already exists (skip): report_interval_hours';
    END IF;

    -- last_report_sent_at
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'last_report_sent_at'
    ) THEN
        ALTER TABLE users ADD COLUMN last_report_sent_at TIMESTAMPTZ;
        RAISE NOTICE 'Added column: last_report_sent_at';
    ELSE
        RAISE NOTICE 'Column already exists (skip): last_report_sent_at';
    END IF;
END $$;
"""


async def main() -> None:
    async with engine.begin() as conn:
        await conn.execute(text(_ADD_COLUMNS_SQL))
    print("✅ users 테이블 마이그레이션 완료 (report 컬럼 3개)")


if __name__ == "__main__":
    asyncio.run(main())
