"""Encryption for secrets stored in the database (AI keys, Gmail tokens, SMTP passwords)."""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

_fernet: Fernet | None = None


def init(secret: str) -> None:
    global _fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(("leadgen-enc:" + secret).encode()).digest())
    _fernet = Fernet(key)


def encrypt(value: str) -> str:
    if not value:
        return ""
    assert _fernet is not None, "crypto not initialised"
    return _fernet.encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    if not token:
        return ""
    assert _fernet is not None, "crypto not initialised"
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


def mask(value: str) -> str:
    """Show only the last 4 characters of a secret."""
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return "•" * 8 + value[-4:]
