#!/usr/bin/env python3
"""기존 DB에 평문으로 저장된 업비트 API 키를 Fernet 암호화로 1회 마이그레이션.

SQLAlchemy ORM/TypeDecorator 를 의도적으로 우회하여 raw SQL 로 처리하므로,
EncryptedString TypeDecorator 가 이미 적용된 상태에서도 안전하게 실행된다.

실행 순서:
  1. .env 에 ENCRYPTION_KEY 가 설정되어 있는지 확인한다.
  2. 이 스크립트를 1회 실행하여 평문 키를 암호화한다.
  3. 이후부터는 TypeDecorator 가 자동으로 암복호화를 처리한다.

Usage:
    cd /path/to/coincome
    python scripts/migrate_keys.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# 프로젝트 루트를 경로에 추가 (상대 임포트 가능하도록)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _is_already_encrypted(fernet: Fernet, value: str) -> bool:
    """값이 이미 유효한 Fernet 암호문인지 판별한다.

    Args:
        fernet: 현재 ENCRYPTION_KEY 로 초기화된 Fernet 인스턴스.
        value: 검사할 문자열.

    Returns:
        True 이면 이미 암호화된 값, False 이면 평문.
    """
    try:
        fernet.decrypt(value.encode())
        return True
    except (InvalidToken, Exception):
        return False


async def migrate() -> None:
    """users 테이블의 평문 API 키를 Fernet 암호문으로 일괄 변환한다."""
    if not settings.encryption_key:
        logger.error(
            "ENCRYPTION_KEY 환경변수가 없습니다. .env 파일을 확인하세요.\n"
            "키 생성: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
        return

    fernet = Fernet(settings.encryption_key.encode())
    engine = create_async_engine(settings.database_url, echo=False)

    async with AsyncSession(engine) as session:
        # ORM/TypeDecorator 를 우회하여 현재 raw 값을 가져온다
        result = await session.execute(
            text("SELECT user_id, upbit_access_key, upbit_secret_key FROM users")
        )
        rows = result.fetchall()

    logger.info("조회된 사용자 수: %d 명", len(rows))

    migrated = 0
    skipped = 0

    async with AsyncSession(engine) as session:
        for user_id, access_key, secret_key in rows:
            new_access: str | None = None
            new_secret: str | None = None

            if access_key:
                if _is_already_encrypted(fernet, access_key):
                    logger.info("SKIP (이미 암호화): user_id=%s — access_key", user_id)
                    skipped += 1
                else:
                    new_access = fernet.encrypt(access_key.encode()).decode()
                    logger.info("암호화 완료: user_id=%s — access_key", user_id)

            if secret_key:
                if _is_already_encrypted(fernet, secret_key):
                    logger.info("SKIP (이미 암호화): user_id=%s — secret_key", user_id)
                else:
                    new_secret = fernet.encrypt(secret_key.encode()).decode()
                    logger.info("암호화 완료: user_id=%s — secret_key", user_id)

            if new_access is not None or new_secret is not None:
                # COALESCE 로 변경이 없는 컬럼은 기존 값 유지
                await session.execute(
                    text(
                        "UPDATE users "
                        "SET upbit_access_key = COALESCE(:ak, upbit_access_key), "
                        "    upbit_secret_key  = COALESCE(:sk, upbit_secret_key) "
                        "WHERE user_id = :uid"
                    ),
                    {"ak": new_access, "sk": new_secret, "uid": user_id},
                )
                migrated += 1

        await session.commit()

    await engine.dispose()
    logger.info(
        "마이그레이션 완료 — 처리: %d 명 / 스킵(이미 암호화): %d 명",
        migrated,
        skipped,
    )


if __name__ == "__main__":
    asyncio.run(migrate())
