"""
app/core/security.py
─────────────────────
Password hashing (bcrypt direct — no passlib) and JWT creation/verification.

Avoids passlib 1.7.4 / bcrypt 4.x incompatibility on Windows.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.config import get_settings

ALGORITHM = "HS256"


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_access_token(subject: str | Any, expires_delta: timedelta | None = None) -> str:
    cfg    = get_settings()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=cfg.access_token_expire_minutes)
    )
    return jwt.encode(
        {"sub": str(subject), "exp": expire, "type": "access",
         "iat": datetime.now(timezone.utc)},
        cfg.secret_key, algorithm=ALGORITHM,
    )


def create_refresh_token(subject: str | Any) -> str:
    cfg    = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(days=cfg.refresh_token_expire_days)
    return jwt.encode(
        {"sub": str(subject), "exp": expire, "type": "refresh",
         "iat": datetime.now(timezone.utc)},
        cfg.secret_key, algorithm=ALGORITHM,
    )


def decode_token(token: str) -> dict:
    cfg = get_settings()
    return jwt.decode(token, cfg.secret_key, algorithms=[ALGORITHM])


def verify_access_token(token: str) -> str:
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise ValueError("Not an access token")
    sub = payload.get("sub")
    if not sub:
        raise JWTError("No subject")
    return sub
