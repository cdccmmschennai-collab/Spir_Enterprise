"""
Admin-only endpoints for user management, password resets, and audit log access.

All endpoints require an authenticated user with role == 'admin'.
Only admins can create/update users or view passwords (passwords are never
returned in any form — only hashes are stored).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update, desc

from spir_dynamic.app.auth import get_current_user, TokenData
from spir_dynamic.db.database import get_db, is_db_enabled
from spir_dynamic.db.models import User, Session, UserActivityLog, ExtractionHistory

log = logging.getLogger(__name__)

admin_router = APIRouter()


# ── Admin guard ────────────────────────────────────────────────────────────────

async def require_admin(td: TokenData = Depends(get_current_user)) -> TokenData:
    """Dependency: raises 403 if caller is not an admin (requires DB mode)."""
    if not is_db_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin endpoints require DATABASE_URL to be configured",
        )
    from spir_dynamic.db.database import get_session_factory
    factory = get_session_factory()
    async with factory() as db:
        user: User | None = await db.scalar(
            select(User).where(User.id == td.user_id)
        )
    if user is None or user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return td


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class UserOut(BaseModel):
    id: str
    username: str
    email: Optional[str]
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: Optional[datetime]

    model_config = {"from_attributes": True}


class CreateUserIn(BaseModel):
    username: str = Field(..., min_length=2, max_length=100)
    password: str = Field(..., min_length=8)
    email: Optional[str] = None
    role: str = Field("user", pattern="^(admin|user)$")


class ResetPasswordIn(BaseModel):
    new_password: str = Field(..., min_length=8)


class ActivityLogOut(BaseModel):
    id: str
    user_id: str
    session_id: Optional[str]
    action: str
    details: Optional[dict]
    ip_address: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class ExtractionHistoryOut(BaseModel):
    id: str
    user_id: str
    original_filename: str
    spir_no: Optional[str]
    format: Optional[str]
    total_rows: int
    total_tags: int
    spare_items: int
    equipment: Optional[str]
    manufacturer: Optional[str]
    supplier: Optional[str]
    dup_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ── User management endpoints ──────────────────────────────────────────────────

@admin_router.get("/users", response_model=list[UserOut])
async def list_users(
    _: TokenData = Depends(require_admin),
    db=Depends(get_db),
) -> list[UserOut]:
    """List all users. Admin only."""
    result = await db.execute(select(User).order_by(User.created_at))
    users = result.scalars().all()
    return [UserOut.model_validate(u) for u in users]


@admin_router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    body: CreateUserIn,
    _: TokenData = Depends(require_admin),
    db=Depends(get_db),
) -> UserOut:
    """Create a new user. Admin only. Password is stored as bcrypt hash only."""
    from passlib.context import CryptContext
    pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

    # Check username uniqueness
    existing = await db.scalar(select(User).where(User.username == body.username))
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")

    user = User(
        username=body.username,
        email=body.email,
        password_hash=pwd_ctx.hash(body.password),
        role=body.role,
        is_active=True,
    )
    db.add(user)
    await db.flush()  # get the generated id
    return UserOut.model_validate(user)


@admin_router.put("/users/{user_id}/password", status_code=204)
async def reset_password(
    user_id: str,
    body: ResetPasswordIn,
    _: TokenData = Depends(require_admin),
    db=Depends(get_db),
) -> None:
    """Reset a user's password. Admin only. New password stored as bcrypt hash."""
    from passlib.context import CryptContext
    pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

    user: User | None = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    user.password_hash = pwd_ctx.hash(body.new_password)
    log.info("Password reset for user '%s' by admin", user.username)


@admin_router.put("/users/{user_id}/status", status_code=204)
async def set_user_status(
    user_id: str,
    is_active: bool,
    _: TokenData = Depends(require_admin),
    db=Depends(get_db),
) -> None:
    """Activate or deactivate a user. Admin only."""
    user: User | None = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = is_active
    log.info("User '%s' is_active set to %s", user.username, is_active)


# ── Audit log / history endpoints ──────────────────────────────────────────────

@admin_router.get("/logs", response_model=list[ActivityLogOut])
async def get_activity_logs(
    limit: int = 100,
    offset: int = 0,
    user_id: Optional[str] = None,
    action: Optional[str] = None,
    _: TokenData = Depends(require_admin),
    db=Depends(get_db),
) -> list[ActivityLogOut]:
    """Get user activity logs. Admin only."""
    q = select(UserActivityLog).order_by(desc(UserActivityLog.created_at))
    if user_id:
        q = q.where(UserActivityLog.user_id == user_id)
    if action:
        q = q.where(UserActivityLog.action == action)
    q = q.offset(offset).limit(limit)
    result = await db.execute(q)
    rows = result.scalars().all()
    return [ActivityLogOut.model_validate(r) for r in rows]


@admin_router.get("/extraction-history", response_model=list[ExtractionHistoryOut])
async def get_extraction_history(
    limit: int = 100,
    offset: int = 0,
    user_id: Optional[str] = None,
    _: TokenData = Depends(require_admin),
    db=Depends(get_db),
) -> list[ExtractionHistoryOut]:
    """Get extraction history across all users. Admin only."""
    q = select(ExtractionHistory).order_by(desc(ExtractionHistory.created_at))
    if user_id:
        q = q.where(ExtractionHistory.user_id == user_id)
    q = q.offset(offset).limit(limit)
    result = await db.execute(q)
    rows = result.scalars().all()
    return [ExtractionHistoryOut.model_validate(r) for r in rows]


# ── Current user's own history (non-admin) ─────────────────────────────────────

@admin_router.get("/my-history", response_model=list[ExtractionHistoryOut])
async def my_extraction_history(
    limit: int = 50,
    offset: int = 0,
    td: TokenData = Depends(get_current_user),
    db=Depends(get_db),
) -> list[ExtractionHistoryOut]:
    """Get the current user's own extraction history."""
    if not is_db_enabled() or not td.user_id:
        return []
    q = (
        select(ExtractionHistory)
        .where(ExtractionHistory.user_id == td.user_id)
        .order_by(desc(ExtractionHistory.created_at))
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(q)
    rows = result.scalars().all()
    return [ExtractionHistoryOut.model_validate(r) for r in rows]
