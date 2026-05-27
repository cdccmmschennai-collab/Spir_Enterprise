"""
JWT authentication — token creation, verification dependency, and login/logout routers.

Two modes:
  DB mode (DATABASE_URL set): authenticates against the users table,
    creates session records, logs activity.
  Legacy mode (no DATABASE_URL): falls back to static APP_USER / APP_PASS
    env vars — same behaviour as the original implementation.
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt as _bcrypt

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from pydantic import BaseModel, Field

from spir_dynamic.app.config import get_settings

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login")
_optional_oauth2 = OAuth2PasswordBearer(tokenUrl="/api/login", auto_error=False)

# ---------------------------------------------------------------------------
# Role constants
# ---------------------------------------------------------------------------

SUPER_ADMIN = "super_admin"
BRANCH_ADMIN = "branch_admin"
USER = "user"
# Legacy role kept for backward compatibility during transition
_LEGACY_ADMIN = "admin"


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
    role: str = "user",
    branch_id: Optional[str] = None,
    expires_delta: Optional[timedelta] = None,
) -> str:
    cfg = get_settings()
    if expires_delta is None:
        expires_delta = timedelta(hours=cfg.token_expire_hours)
    expire = datetime.now(timezone.utc) + expires_delta
    payload: dict = {"sub": username, "exp": expire, "role": role}
    if user_id:
        payload["uid"] = user_id
    if jti:
        payload["jti"] = jti
    if branch_id:
        payload["bid"] = branch_id
    return jwt.encode(payload, cfg.secret_key, algorithm="HS256")


class TokenData:
    """Parsed token payload used by route dependencies."""
    __slots__ = ("username", "user_id", "jti", "role", "branch_id")

    def __init__(
        self,
        username: str,
        user_id: Optional[str],
        jti: Optional[str],
        role: str = "user",
        branch_id: Optional[str] = None,
    ):
        self.username = username
        self.user_id = user_id
        self.jti = jti
        self.role = role
        self.branch_id = branch_id


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
        raw_role = payload.get("role", "user")
        # Transparently upgrade legacy "admin" tokens issued before role migration
        if raw_role == _LEGACY_ADMIN:
            raw_role = SUPER_ADMIN
        return TokenData(
            username=username,
            user_id=payload.get("uid"),
            jti=payload.get("jti"),
            role=raw_role,
            branch_id=payload.get("bid"),
        )
    except JWTError:
        raise exc


def verify_token(token: str = Depends(oauth2_scheme)) -> str:
    """
    FastAPI dependency — returns the username or raises 401.
    Kept for backwards compatibility with existing route signatures.
    """
    return _decode_token(token).username


async def get_optional_user(token: Optional[str] = Depends(_optional_oauth2)) -> TokenData:
    """Returns anonymous TokenData if no token — never raises 401."""
    if not token:
        return TokenData(username="anonymous", user_id=None, jti=None, branch_id=None)
    td = _decode_token(token)
    if td.jti:
        from spir_dynamic.services.audit_service import update_session_activity
        import asyncio
        asyncio.ensure_future(update_session_activity(td.jti))
    return td


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
# Login rate limiter
# ---------------------------------------------------------------------------
#
# Security principle: bcrypt is intentionally slow (~100 ms at cost=12), but an
# attacker with many IPs can still queue thousands of guesses per hour. A per-IP
# sliding-window counter in Redis caps the attempt rate without storing passwords
# or blocking legitimate users.
#
# Design decisions:
#   - Window: 60 s (resets rolling — no cliff effect between minutes)
#   - Limit: LOGIN_RATE_LIMIT_PER_MINUTE env var (default 10, 0 = disabled)
#   - Fails open: Redis down → rate limiting skipped, login proceeds normally.
#     A Redis outage must never lock all users out of the application.
#   - The 429 response includes Retry-After so clients/browsers can backoff.
#
# Local dev: set LOGIN_RATE_LIMIT_PER_MINUTE=0 in src/.env to disable entirely.

_rl_client: Any = None  # module-level singleton; initialised on first call


def _get_rl_client(redis_url: str) -> Any:
    """Return (or lazily create) the async Redis client used for rate limiting."""
    global _rl_client
    if _rl_client is None:
        import redis.asyncio as _aioredis  # redis>=4.2 ships this sub-package

        _rl_client = _aioredis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=1,   # fail fast if Redis is unreachable
            socket_timeout=1,
        )
    return _rl_client


async def _enforce_login_rate_limit(ip: Optional[str], cfg) -> None:
    """
    Raise HTTP 429 if the calling IP has exceeded the configured login attempt
    limit within the current 60-second window.

    Silently no-ops when:
      - ip is None (cannot determine caller address)
      - LOGIN_RATE_LIMIT_PER_MINUTE == 0 (explicitly disabled)
      - Redis is unreachable (fails open — preserves availability)
    """
    if not ip or cfg.login_rate_limit_per_minute <= 0:
        return

    try:
        r = _get_rl_client(cfg.redis_url)
        key = f"rl:login:{ip}"

        # Atomic increment.  If this is the first attempt in the window, set a
        # 60-second TTL.  INCR + EXPIRE is not perfectly atomic, but the worst
        # case is a single request slipping through after a TTL races — acceptable.
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, 60)

        if count > cfg.login_rate_limit_per_minute:
            ttl = max(int(await r.ttl(key)), 1)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many login attempts. Please try again later.",
                headers={"Retry-After": str(ttl)},
            )
    except HTTPException:
        raise  # re-raise 429 — do not swallow it
    except Exception as exc:  # noqa: BLE001
        # Redis unavailable, connection refused, timeout, etc.
        # Log at WARNING so ops can detect a misconfigured Redis, but never
        # block the login path over a monitoring/infra dependency.
        _log.warning("rate_limit.redis_unavailable: %s", exc)


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

    # ── Rate limit — checked before any password work ─────────────────────────
    # Enforcing the limit before bcrypt runs means an attacker cannot use the
    # endpoint to saturate CPU with bcrypt hashing even if they bypass Redis.
    await _enforce_login_rate_limit(ip, cfg)

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
    token = create_access_token(username=form_data.username, role="admin")
    return {"access_token": token, "token_type": "bearer"}


@auth_router.get("/me")
async def auth_me(td: TokenData = Depends(get_current_user)) -> dict:
    """Return the authenticated user's profile."""
    from spir_dynamic.db.database import is_db_enabled, get_session_factory
    from spir_dynamic.db.models import User
    from sqlalchemy import select, text

    if is_db_enabled() and td.user_id:
        factory = get_session_factory()
        async with factory() as db:
            user: User | None = await db.get(User, td.user_id)
            result = await db.execute(
                text("SELECT COUNT(*) FROM extraction_history WHERE user_id = :uid"),
                {"uid": td.user_id},
            )
            total_files = int(result.scalar() or 0)
            if user:
                role = user.role
                if role == _LEGACY_ADMIN:
                    role = SUPER_ADMIN
                return {
                    "id": user.id,
                    "username": user.username,
                    "email": user.email,
                    "role": role,
                    "branch_id": user.branch_id,
                    "created_at": user.created_at,
                    "last_login_at": user.last_login_at,
                    "total_files_extracted": total_files,
                }
    return {
        "id": None,
        "username": td.username,
        "email": None,
        "role": td.role,
        "branch_id": td.branch_id,
        "created_at": None,
        "last_login_at": None,
        "total_files_extracted": 0,
    }


@auth_router.post("/refresh")
async def refresh_token(td: TokenData = Depends(get_current_user)) -> dict:
    """Issue a new access token with a fresh expiry using the current valid token."""
    cfg = get_settings()
    new_jti = str(uuid.uuid4())
    token = create_access_token(
        username=td.username,
        user_id=td.user_id,
        jti=new_jti,
        role=td.role,
        expires_delta=timedelta(hours=cfg.token_expire_hours),
    )
    return {"access_token": token, "token_type": "bearer"}


@auth_router.post("/logout")
async def logout(
    request: Request,
    td: TokenData = Depends(get_optional_user),
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


class ChangePasswordIn(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8, max_length=72)


@auth_router.put("/change-password", status_code=204)
async def change_password(
    body: ChangePasswordIn,
    td: TokenData = Depends(get_current_user),
) -> None:
    """
    Change the current user's password.
    Requires the current password to be supplied and correct.
    Invalidates all other active sessions after a successful change.
    """
    from spir_dynamic.db.database import is_db_enabled, get_session_factory
    from spir_dynamic.db.models import User, Session
    from sqlalchemy import select, update
    from spir_dynamic.services.audit_service import log_activity

    if not is_db_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Password change requires database mode",
        )
    if not td.user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    # Validate password policy
    import re as _re
    pw = body.new_password
    if not _re.search(r"[A-Z]", pw):
        raise HTTPException(status_code=400, detail="Password must contain at least one uppercase letter")
    if not _re.search(r"[a-z]", pw):
        raise HTTPException(status_code=400, detail="Password must contain at least one lowercase letter")
    if not _re.search(r"\d", pw):
        raise HTTPException(status_code=400, detail="Password must contain at least one digit")

    factory = get_session_factory()
    async with factory() as db:
        user: User | None = await db.get(User, td.user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        if not _verify_password(body.current_password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Current password is incorrect",
            )

        # Update password
        user.password_hash = _hash_password(body.new_password[:72])

        # Invalidate all sessions except the current one
        revoke_stmt = (
            update(Session)
            .where(Session.user_id == td.user_id)
            .where(Session.jti != td.jti if td.jti else Session.user_id == td.user_id)
            .values(is_active=False)
        )
        await db.execute(revoke_stmt)
        await db.commit()

    import asyncio
    asyncio.ensure_future(log_activity(
        user_id=td.user_id,
        action="password_changed",
        session_id=td.jti,
    ))


class PasswordResetRequestIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    email: Optional[str] = None
    reason: Optional[str] = Field(None, max_length=500)


@auth_router.post("/reset-request", status_code=201)
async def create_reset_request(body: PasswordResetRequestIn) -> dict:
    """
    Submit a password reset request for admin approval. No auth required.
    Accepts username or email as the identifier field.
    Returns 404 if no active account matches, 201 on success.
    No DB row is created for unknown/inactive accounts.
    """
    from spir_dynamic.db.database import is_db_enabled, get_session_factory
    from spir_dynamic.db.models import PasswordResetRequest, User
    from sqlalchemy import select

    if not is_db_enabled():
        return {"message": "Request received. Contact your system administrator directly."}

    identifier = body.username.strip()
    user_found = False
    try:
        factory = get_session_factory()
        async with factory() as db:
            # Support lookup by username OR email so users can use either identifier
            user: User | None = await db.scalar(
                select(User).where(
                    (User.username == identifier) | (User.email == identifier),
                    User.is_active == True,
                )
            )
            if user is not None:
                user_found = True
                req = PasswordResetRequest(
                    username=user.username,
                    user_id=user.id,
                    branch_id=user.branch_id,
                    email=body.email.strip() if body.email else None,
                    reason=body.reason.strip() if body.reason else None,
                    status="pending",
                )
                db.add(req)
                await db.commit()
    except Exception:
        pass  # swallow genuine DB/network errors; the found flag decides the response

    if not user_found:
        raise HTTPException(status_code=404, detail="No matching account found.")

    return {"message": "Password reset request submitted."}


# ---------------------------------------------------------------------------
# Role-based dependency guards
# ---------------------------------------------------------------------------

async def require_super_admin(td: TokenData = Depends(get_current_user)) -> TokenData:
    """Dependency: raises 403 unless caller is SUPER_ADMIN."""
    if td.role != SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super-admin access required",
        )
    from spir_dynamic.db.database import is_db_enabled
    if not is_db_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin endpoints require DATABASE_URL to be configured",
        )
    return td


async def require_branch_admin_or_above(td: TokenData = Depends(get_current_user)) -> TokenData:
    """Dependency: raises 403 unless caller is BRANCH_ADMIN or SUPER_ADMIN."""
    if td.role not in (SUPER_ADMIN, BRANCH_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    from spir_dynamic.db.database import is_db_enabled
    if not is_db_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin endpoints require DATABASE_URL to be configured",
        )
    return td


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
            select(User).where(
                (User.username == username) | (User.email == username),
                User.is_active == True,
            )
        )
        # Capture all needed fields INSIDE the session — never access a detached
        # SQLAlchemy object outside an async session (causes MissingGreenlet).
        user_id: str | None = user.id if user is not None else None
        user_name: str | None = user.username if user is not None else None
        password_hash: str | None = user.password_hash if user is not None else None
        user_role: str = user.role if user is not None else "user"
        user_branch_id: str | None = user.branch_id if user is not None else None
        # Upgrade legacy tokens at login time
        if user_role == _LEGACY_ADMIN:
            user_role = SUPER_ADMIN

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
        role=user_role,
        branch_id=user_branch_id,
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
