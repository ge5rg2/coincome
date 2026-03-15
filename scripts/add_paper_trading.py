"""
One-time DB migration: 모의투자(Paper Trading) 기능을 위한 스키마 변경.

변경 내역:
  [users 테이블]
    - virtual_krw  FLOAT NOT NULL DEFAULT 10000000.0  (가상 잔고 1,000만 원)

  [bot_settings 테이블]
    - is_paper_trading  BOOLEAN NOT NULL DEFAULT FALSE  (모의투자 모드 플래그)

  [trade_history 테이블 신규 생성]
    - id               SERIAL PRIMARY KEY
    - user_id          VARCHAR(255) NOT NULL REFERENCES users(user_id)
    - symbol           VARCHAR(20) NOT NULL
    - buy_price        FLOAT NOT NULL
    - sell_price       FLOAT NOT NULL
    - profit_pct       FLOAT NOT NULL
    - profit_krw       FLOAT NOT NULL
    - buy_amount_krw   FLOAT NOT NULL
    - is_paper_trading BOOLEAN NOT NULL DEFAULT TRUE
    - created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()

실행 방법:
  python -m scripts.add_paper_trading

멱등(idempotent) 실행 보장:
  - 기존 컬럼은 information_schema.columns 조회 후 없을 때만 ALTER TABLE.
  - trade_history 테이블은 CREATE TABLE IF NOT EXISTS 사용.
  - 인덱스는 CREATE INDEX IF NOT EXISTS 사용.

주의: PL/pgSQL DO $$ 블록을 사용하지 않는 순수 Python 구현.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.database import engine


# ── 컬럼 추가 정의 (table_name, column_name, ALTER TABLE DDL) ─────────
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    (
        "users",
        "virtual_krw",
        "ALTER TABLE users ADD COLUMN virtual_krw FLOAT NOT NULL DEFAULT 10000000.0",
    ),
    (
        "bot_settings",
        "is_paper_trading",
        "ALTER TABLE bot_settings ADD COLUMN is_paper_trading BOOLEAN NOT NULL DEFAULT FALSE",
    ),
]

# ── trade_history 테이블 생성 DDL ────────────────────────────────────
_CREATE_TRADE_HISTORY = """
CREATE TABLE IF NOT EXISTS trade_history (
    id               SERIAL PRIMARY KEY,
    user_id          VARCHAR(255) NOT NULL REFERENCES users(user_id),
    symbol           VARCHAR(20) NOT NULL,
    buy_price        FLOAT NOT NULL,
    sell_price       FLOAT NOT NULL,
    profit_pct       FLOAT NOT NULL,
    profit_krw       FLOAT NOT NULL,
    buy_amount_krw   FLOAT NOT NULL,
    is_paper_trading BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

_CREATE_TRADE_HISTORY_IDX = """
CREATE INDEX IF NOT EXISTS ix_trade_history_user_id ON trade_history (user_id)
"""


async def main() -> None:
    async with engine.begin() as conn:
        # ── 1. 기존 컬럼 현황 조회 ──────────────────────────────────
        result = await conn.execute(
            text(
                "SELECT table_name, column_name "
                "FROM information_schema.columns "
                "WHERE table_name IN ('users', 'bot_settings')"
            )
        )
        existing: set[tuple[str, str]] = {(row[0], row[1]) for row in result}

        # ── 2. 누락 컬럼 추가 (멱등) ─────────────────────────────────
        for table_name, col_name, ddl in _COLUMN_MIGRATIONS:
            if (table_name, col_name) not in existing:
                await conn.execute(text(ddl))
                print(f"✅ Added column: {table_name}.{col_name}")
            else:
                print(f"⏭️  Column already exists (skip): {table_name}.{col_name}")

        # ── 3. trade_history 테이블 생성 (IF NOT EXISTS — 멱등) ──────
        await conn.execute(text(_CREATE_TRADE_HISTORY))
        print("✅ trade_history 테이블 확인/생성 완료")

        # ── 4. user_id 인덱스 생성 (IF NOT EXISTS — 멱등) ───────────
        await conn.execute(text(_CREATE_TRADE_HISTORY_IDX))
        print("✅ ix_trade_history_user_id 인덱스 확인/생성 완료")

    print("\n✅ 모의투자 DB 마이그레이션 완료")
    print("   - users.virtual_krw")
    print("   - bot_settings.is_paper_trading")
    print("   - trade_history 테이블 + 인덱스")


if __name__ == "__main__":
    asyncio.run(main())
