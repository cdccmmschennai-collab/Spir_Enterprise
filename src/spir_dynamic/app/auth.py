"""
JWT authentication — token creation, verification dependency, and login router.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt

from spir_dynamic.app.config import get_settings

# ---------------------------------------------------------------------------
# OAuth2 scheme — tokenUrl must match the mounted login endpoint URL
# ---------------------------------------------------------------------------

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def create_access_token(data: dict, expires_delta: timedelta) -> str:
    cfg = get_settings()
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + expires_delta
    to_encode["exp"] = expire
    return jwt.encode(to_encode, cfg.secret_key, algorithm="HS256")


def verify_token(token: str = Depends(oauth2_scheme)) -> str:
    """FastAPI dependency — returns the username or raises 401."""
    cfg = get_settings()
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, cfg.secret_key, algorithms=["HS256"])
        username: str | None = payload.get("sub")
        if username is None:
            raise credentials_exc
        return username
    except JWTError:
        raise credentials_exc


# ---------------------------------------------------------------------------
# Auth router
# ---------------------------------------------------------------------------

auth_router = APIRouter()


@auth_router.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends()) -> dict:
    """Authenticate with username + password, return a JWT bearer token."""
    cfg = get_settings()
    valid_user = secrets.compare_digest(form_data.username, cfg.app_user)
    valid_pass = secrets.compare_digest(form_data.password, cfg.app_pass)

    if not (valid_user and valid_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(
        data={"sub": form_data.username},
        expires_delta=timedelta(hours=cfg.token_expire_hours),
    )
    return {"access_token": token, "token_type": "bearer"}
