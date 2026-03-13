"""
One-time DB migration: users 테이블에 AI 최대 보유 종목 수 컬럼 추가.

추가 컬럼:
  - ai_max_coins  INTEGER NOT NULL DEFAULT 3

실행 방법:
  python -m scripts.add_ai_max_coins_column

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
        "ai_max_coins",
        "ALTER TABLE users ADD COLUMN ai_max_coins INTEGER NOT NULL DEFAULT 3",
    ),
]


async def main() -> None:
    async with engine.begin() as conn:
        # 현재 users 테이블에 존재하는 컬럼 목록 조회
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

    print("✅ ai_max_coins 컬럼 마이그레이션 완료")


if __name__ == "__main__":
    asyncio.run(main())
