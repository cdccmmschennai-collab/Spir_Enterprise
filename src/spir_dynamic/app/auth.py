"""
JWT authentication — token creation, verification dependency, and login/logout routers.

Two modes:
  DB mode (DATABASE_URL set): authenticates against the users table,
    creates session records, logs activity.
  Legacy mode (no DATABASE_URL): falls back to static APP_USER / APP_PASS
    env vars — same behaviour as the original implementation.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt as _bcrypt

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt

from spir_dynamic.app.config import get_settings

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def _hash_password(plain: str) -> str:
    """Hash a plain-text password with bcrypt."""
    return _bcrypt.hashpw(plain.encode("utf-8"), _bcrypt.gensalt(rounds=12)).decode("utf-8")


def _verify_password(plain: str, hashed: str) -> bool:
    """Verify a plain-text password against a bcrypt hash. Never raises."""
    try:
        return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def create_access_token(
    username: str,
    user_id: Optional[str] = None,
    jti: Optional[str] = None,
    expires_delta: Optional[timedelta] = None,
) -> str:
    cfg = get_settings()
    if expires_delta is None:
        expires_delta = timedelta(hours=cfg.token_expire_hours)
    expire = datetime.now(timezone.utc) + expires_delta
    payload: dict = {"sub": username, "exp": expire}
    if user_id:
        payload["uid"] = user_id
    if jti:
        payload["jti"] = jti
    return jwt.encode(payload, cfg.secret_key, algorithm="HS256")


class TokenData:
    """Parsed token payload used by route dependencies."""
    __slots__ = ("username", "user_id", "jti")

    def __init__(self, username: str, user_id: Optional[str], jti: Optional[str]):
        self.username = username
        self.user_id = user_id
        self.jti = jti


def _decode_token(token: str) -> TokenData:
    """Decode JWT and return TokenData. Raises 401 on failure."""
    cfg = get_settings()
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, cfg.secret_key, algorithms=["HS256"])
        username: str | None = payload.get("sub")
        if not username:
            raise exc
        return TokenData(
            username=username,
            user_id=payload.get("uid"),
            jti=payload.get("jti"),
        )
    except JWTError:
        raise exc


def verify_token(token: str = Depends(oauth2_scheme)) -> str:
    """
    FastAPI dependency — returns the username or raises 401.
    Kept for backwards compatibility with existing route signatures.
    """
    return _decode_token(token).username


async def get_current_user(token: str = Depends(oauth2_scheme)) -> TokenData:
    """
    FastAPI dependency — returns a TokenData with username, user_id, jti.
    Used by routes that need to perform audit logging.
    """
    td = _decode_token(token)

    # Optionally bump session activity in DB (fire-and-forget)
    if td.jti:
        from spir_dynamic.services.audit_service import update_session_activity
        import asyncio
        asyncio.ensure_future(update_session_activity(td.jti))

    return td


# ---------------------------------------------------------------------------
# Auth router
# ---------------------------------------------------------------------------

auth_router = APIRouter()


@auth_router.post("/login")
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
) -> dict:
    """Authenticate with username + password, return a JWT bearer token."""
    cfg = get_settings()
    ip = _get_ip(request)
    ua = request.headers.get("user-agent")

    # ── DB mode ──────────────────────────────────────────────────────────────
    from spir_dynamic.db.database import is_db_enabled
    if is_db_enabled():
        token, _ = await _login_db(form_data.username, form_data.password, ip, ua)
        return {"access_token": token, "token_type": "bearer"}

    # ── Legacy mode ───────────────────────────────────────────────────────────
    valid_user = secrets.compare_digest(form_data.username, cfg.app_user)
    valid_pass = secrets.compare_digest(form_data.password, cfg.app_pass)
    if not (valid_user and valid_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token(username=form_data.username)
    return {"access_token": token, "token_type": "bearer"}


@auth_router.post("/logout")
async def logout(
    request: Request,
    td: TokenData = Depends(get_current_user),
) -> dict:
    """Invalidate the current session."""
    if td.jti:
        from spir_dynamic.services.audit_service import end_session, log_logout
        await end_session(td.jti)
        if td.user_id:
            await log_logout(
                user_id=td.user_id,
                session_id=td.jti,
                ip_address=_get_ip(request),
            )
    return {"detail": "Logged out"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _login_db(
    username: str,
    plain_password: str,
    ip_address: Optional[str],
    user_agent: Optional[str],
) -> tuple[str, str]:
    """Authenticate against the users table, create a session, return (token, session_jti)."""
    from sqlalchemy import select, update
    from spir_dynamic.db.database import get_session_factory
    from spir_dynamic.db.models import User, Session
    from spir_dynamic.services.audit_service import log_login

    cfg = get_settings()
    factory = get_session_factory()

    async with factory() as db:
        user: User | None = await db.scalar(
            select(User).where(User.username == username, User.is_active == True)
        )
        # Capture all needed fields INSIDE the session — never access a detached
        # SQLAlchemy object outside an async session (causes MissingGreenlet).
        user_id: str | None = user.id if user is not None else None
        user_name: str | None = user.username if user is not None else None
        password_hash: str | None = user.password_hash if user is not None else None

    if password_hash is None or not _verify_password(plain_password, password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Create session record
    jti = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(hours=cfg.token_expire_hours)

    async with factory() as db:
        session_row = Session(
            user_id=user_id,
            jti=jti,
            ip_address=ip_address,
            user_agent=user_agent,
            expires_at=expires_at,
            is_active=True,
        )
        db.add(session_row)

        # Update last_login_at
        user_upd: User | None = await db.get(User, user_id)
        if user_upd:
            user_upd.last_login_at = datetime.now(timezone.utc)

        await db.commit()

    # Fire-and-forget activity log
    import asyncio
    asyncio.ensure_future(
        log_login(
            user_id=user_id,
            session_id=jti,
            ip_address=ip_address,
            user_agent=user_agent,
        )
    )

    token = create_access_token(
        username=user_name,
        user_id=user_id,
        jti=jti,
        expires_delta=timedelta(hours=cfg.token_expire_hours),
    )
    return token, jti


def _get_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None
