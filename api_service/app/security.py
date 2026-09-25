from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str, *, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return f"pbkdf2_sha256${salt}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algo, salt, digest = password_hash.split("$", 2)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    actual = hash_password(password, salt=salt)
    return secrets.compare_digest(actual, password_hash)


def generate_password(length: int = 14) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def generate_session_token() -> str:
    return secrets.token_urlsafe(36)


def expires_at() -> datetime:
    return utcnow() + timedelta(hours=settings.SESSION_TTL_HOURS)
