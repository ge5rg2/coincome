"""
One-time DB migration: users 테이블에 AI 예산 및 연착륙 플래그 컬럼 2개 추가.

추가 컬럼:
  - ai_budget_krw       FLOAT    NOT NULL DEFAULT 0.0
  - ai_is_shutting_down BOOLEAN  NOT NULL DEFAULT FALSE

실행 방법:
  python -m scripts.add_ai_budget_shutdown_columns

순수 파이썬 로직으로 구현 (PL/pgSQL DO $$ 블록 미사용).
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
        "ai_budget_krw",
        "ALTER TABLE users ADD COLUMN ai_budget_krw FLOAT NOT NULL DEFAULT 0.0",
    ),
    (
        "ai_is_shutting_down",
        "ALTER TABLE users ADD COLUMN ai_is_shutting_down BOOLEAN NOT NULL DEFAULT FALSE",
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

    print("✅ AI 예산/연착륙 컬럼 마이그레이션 완료")


if __name__ == "__main__":
    asyncio.run(main())
