"""
One-time DB migration: AI 메타데이터 컬럼 추가.

추가 컬럼:
  bot_settings 테이블:
    - trade_style  VARCHAR(20)  NULL  -- "SWING" | "SCALPING"
    - ai_score     INTEGER      NULL  -- 0–100 매력도 점수
    - ai_reason    TEXT         NULL  -- AI 분석 근거 텍스트

  trade_history 테이블:
    - is_ai_managed  BOOLEAN NOT NULL DEFAULT FALSE
    - trade_style    VARCHAR(20)  NULL
    - ai_score       INTEGER      NULL
    - ai_reason      TEXT         NULL

실행 방법:
  python -m scripts.add_ai_log_columns

멱등 실행 가능: information_schema.columns 로 존재 여부 확인 후 ALTER TABLE 실행.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.database import engine

# (table_name, column_name, DDL)
_COLUMNS: list[tuple[str, str, str]] = [
    (
        "bot_settings",
        "trade_style",
        "ALTER TABLE bot_settings ADD COLUMN trade_style VARCHAR(20) NULL",
    ),
    (
        "bot_settings",
        "ai_score",
        "ALTER TABLE bot_settings ADD COLUMN ai_score INTEGER NULL",
    ),
    (
        "bot_settings",
        "ai_reason",
        "ALTER TABLE bot_settings ADD COLUMN ai_reason TEXT NULL",
    ),
    (
        "trade_history",
        "is_ai_managed",
        "ALTER TABLE trade_history ADD COLUMN is_ai_managed BOOLEAN NOT NULL DEFAULT FALSE",
    ),
    (
        "trade_history",
        "trade_style",
        "ALTER TABLE trade_history ADD COLUMN trade_style VARCHAR(20) NULL",
    ),
    (
        "trade_history",
        "ai_score",
        "ALTER TABLE trade_history ADD COLUMN ai_score INTEGER NULL",
    ),
    (
        "trade_history",
        "ai_reason",
        "ALTER TABLE trade_history ADD COLUMN ai_reason TEXT NULL",
    ),
]


async def main() -> None:
    async with engine.begin() as conn:
        for table_name, col_name, ddl in _COLUMNS:
            result = await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :tbl AND column_name = :col"
                ),
                {"tbl": table_name, "col": col_name},
            )
            exists = result.fetchone() is not None

            if not exists:
                await conn.execute(text(ddl))
                print(f"✅ Added: {table_name}.{col_name}")
            else:
                print(f"⏭️  Already exists (skip): {table_name}.{col_name}")

    print("✅ AI 메타데이터 컬럼 마이그레이션 완료")


if __name__ == "__main__":
    asyncio.run(main())
