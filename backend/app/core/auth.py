"""
app/core/auth.py
─────────────────
User model, lazy in-memory store, and FastAPI auth dependencies.
hash_password() is called lazily (not at import time) to avoid startup crash.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError

from app.core.security import verify_password, hash_password, verify_access_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


@dataclass
class User:
    username:        str
    hashed_password: str
    email:           str  = ""
    full_name:       str  = ""
    role:            str  = "engineer"
    disabled:        bool = False


# Lazy-loaded user DB
_USER_DB: dict[str, User] = {}
_USER_DB_LOADED = False


def _ensure_db_loaded() -> None:
    global _USER_DB, _USER_DB_LOADED
    if not _USER_DB_LOADED:
        _USER_DB = {
            "admin": User(
                username="admin",
                hashed_password=hash_password("admin123"),
                email="admin@spir.local",
                full_name="System Administrator",
                role="admin",
            ),
            "engineer": User(
                username="engineer",
                hashed_password=hash_password("engineer123"),
                email="engineer@spir.local",
                full_name="SPIR Engineer",
                role="engineer",
            ),
        }
        _USER_DB_LOADED = True


def get_user(username: str) -> Optional[User]:
    _ensure_db_loaded()
    return _USER_DB.get(username)


def authenticate_user(username: str, password: str) -> Optional[User]:
    _ensure_db_loaded()
    user = get_user(username)
    if not user or not verify_password(password, user.hashed_password):
        return None
    return None if user.disabled else user


def create_user(username: str, plain_password: str,
                email: str = "", full_name: str = "", role: str = "engineer") -> User:
    _ensure_db_loaded()
    if username in _USER_DB:
        raise ValueError(f"Username '{username}' already exists")
    user = User(username=username, hashed_password=hash_password(plain_password),
                email=email, full_name=full_name, role=role)
    _USER_DB[username] = user
    return user


_CREDS_EXC = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    try:
        username = verify_access_token(token)
    except (JWTError, ValueError):
        raise _CREDS_EXC
    user = get_user(username)
    if user is None:
        raise _CREDS_EXC
    if user.disabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account disabled")
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return user
