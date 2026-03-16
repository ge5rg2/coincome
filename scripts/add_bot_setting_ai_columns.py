"""
One-time DB migration: bot_settings 테이블에 AI 메타데이터 컬럼 3개 추가.

추가 컬럼:
  - trade_style  VARCHAR(20)  DEFAULT 'SWING'
  - ai_score     INTEGER      DEFAULT 0
  - ai_reason    TEXT         DEFAULT ''

실행 방법:
  python -m scripts.add_bot_setting_ai_columns

멱등 실행 가능: information_schema.columns 로 존재 여부 확인 후 ALTER TABLE 실행.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.database import engine

_COLUMNS: list[tuple[str, str]] = [
    (
        "trade_style",
        "ALTER TABLE bot_settings ADD COLUMN trade_style VARCHAR(20) DEFAULT 'SWING'",
    ),
    (
        "ai_score",
        "ALTER TABLE bot_settings ADD COLUMN ai_score INTEGER DEFAULT 0",
    ),
    (
        "ai_reason",
        "ALTER TABLE bot_settings ADD COLUMN ai_reason TEXT DEFAULT ''",
    ),
]


async def main() -> None:
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'bot_settings'"
            )
        )
        existing: set[str] = {row[0] for row in result}

        for col_name, ddl in _COLUMNS:
            if col_name not in existing:
                await conn.execute(text(ddl))
                print(f"✅ Added column: bot_settings.{col_name}")
            else:
                print(f"⏭️  Already exists (skip): bot_settings.{col_name}")

    print("✅ bot_settings AI 컬럼 마이그레이션 완료")


if __name__ == "__main__":
    asyncio.run(main())
