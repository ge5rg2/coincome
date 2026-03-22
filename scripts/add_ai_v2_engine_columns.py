"""
One-time DB migration: users 테이블에 AI 트레이딩 V2 엔진 관련 컬럼 5개 추가.

추가 컬럼:
  - ai_engine_mode        VARCHAR(20)  NULL   -- SWING / SCALPING / BOTH (NULL=미설정)
  - ai_swing_budget_krw   FLOAT        NOT NULL DEFAULT 1000000.0
  - ai_swing_weight_pct   FLOAT        NOT NULL DEFAULT 20.0
  - ai_scalp_budget_krw   FLOAT        NOT NULL DEFAULT 1000000.0
  - ai_scalp_weight_pct   FLOAT        NOT NULL DEFAULT 20.0

실행 방법:
  python -m scripts.add_ai_v2_engine_columns

information_schema.columns 를 먼저 조회해 컬럼 존재 여부를 확인 후
개별 ALTER TABLE 을 실행하므로 멱등 실행이 가능합니다.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.database import engine

# 추가할 컬럼 정의: (column_name, ALTER TABLE DDL)
_COLUMNS: list[tuple[str, str]] = [
    (
        "ai_engine_mode",
        "ALTER TABLE users ADD COLUMN ai_engine_mode VARCHAR(20) NULL",
    ),
    (
        "ai_swing_budget_krw",
        "ALTER TABLE users ADD COLUMN ai_swing_budget_krw FLOAT NOT NULL DEFAULT 1000000.0",
    ),
    (
        "ai_swing_weight_pct",
        "ALTER TABLE users ADD COLUMN ai_swing_weight_pct FLOAT NOT NULL DEFAULT 20.0",
    ),
    (
        "ai_scalp_budget_krw",
        "ALTER TABLE users ADD COLUMN ai_scalp_budget_krw FLOAT NOT NULL DEFAULT 1000000.0",
    ),
    (
        "ai_scalp_weight_pct",
        "ALTER TABLE users ADD COLUMN ai_scalp_weight_pct FLOAT NOT NULL DEFAULT 20.0",
    ),
]


async def main() -> None:
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'users'"
            )
        )
        existing: set[str] = {row[0] for row in result}

        for col_name, ddl in _COLUMNS:
            if col_name not in existing:
                await conn.execute(text(ddl))
                print(f"✅ Added column: {col_name}")
            else:
                print(f"⏭️  Column already exists (skip): {col_name}")

    print("✅ AI V2 엔진 컬럼 마이그레이션 완료")


if __name__ == "__main__":
    asyncio.run(main())
