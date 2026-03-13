"""AES-256 기반 문자열 암복호화 유틸리티 (cryptography.Fernet).

Fernet은 AES-128-CBC + HMAC-SHA256 조합으로 인증된 대칭 암호화를 제공한다.
키는 환경변수 ENCRYPTION_KEY (Base64 URL-safe 32바이트) 로 주입된다.

Examples:
    >>> from app.utils.crypto import encrypt, decrypt
    >>> token = encrypt("my-secret")
    >>> decrypt(token)
    'my-secret'
"""
from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    """Fernet 인스턴스를 싱글턴으로 반환한다.

    Returns:
        초기화된 Fernet 인스턴스.

    Raises:
        RuntimeError: ENCRYPTION_KEY 환경변수가 비어있을 때.
    """
    global _fernet
    if _fernet is None:
        from app.config import settings  # 순환 임포트 방지를 위해 지연 임포트

        key = settings.encryption_key
        if not key:
            raise RuntimeError(
                "ENCRYPTION_KEY 환경변수가 설정되지 않았습니다. "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\" "
                "로 키를 생성한 뒤 .env 에 등록하세요."
            )
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(plaintext: str) -> str:
    """평문 문자열을 Fernet 토큰(암호문)으로 변환한다.

    Args:
        plaintext: 암호화할 평문 문자열.

    Returns:
        URL-safe Base64 인코딩된 Fernet 암호문 문자열.
    """
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Fernet 토큰(암호문)을 평문 문자열로 복원한다.

    Args:
        ciphertext: 복호화할 Fernet 암호문 문자열.

    Returns:
        복호화된 평문 문자열.

    Raises:
        InvalidToken: 토큰이 유효하지 않거나 키가 다를 때.
    """
    return _get_fernet().decrypt(ciphertext.encode()).decode()


__all__ = ["encrypt", "decrypt", "InvalidToken"]
