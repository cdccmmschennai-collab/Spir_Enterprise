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
    """Dependency: raises 403 if caller is not an admin. Role is read from JWT — no DB round-trip."""
    if td.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    if not is_db_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin endpoints require DATABASE_URL to be configured",
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


class ResetRequestOut(BaseModel):
    id: str
    username: str
    email: Optional[str]
    reason: Optional[str]
    status: str
    created_at: datetime
    resolved_at: Optional[datetime]
    resolved_by: Optional[str]

    model_config = {"from_attributes": True}


class ResolveResetIn(BaseModel):
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
    format: Optional[str] = None
    total_rows: int = 0
    # Backward-compatible aliases: ORM uses tag_count/spare_count
    total_tags: int = 0
    spare_items: int = 0
    equipment: Optional[str] = None
    manufacturer: Optional[str] = None
    supplier: Optional[str] = None
    dup_count: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def model_validate(cls, obj, *args, **kwargs):  # type: ignore[override]
        """
        Accepts ORM rows with the smaller ExtractionHistory model and derives
        admin-friendly fields (total_tags/spare_items) from tag_count/spare_count.
        """
        data = {
            "id": getattr(obj, "id"),
            "user_id": getattr(obj, "user_id"),
            "original_filename": getattr(obj, "original_filename", "") or "",
            "spir_no": getattr(obj, "spir_no", None),
            "format": getattr(obj, "format", None),
            "total_rows": int(getattr(obj, "total_rows", 0) or 0),
            "total_tags": int(getattr(obj, "total_tags", None) or getattr(obj, "tag_count", 0) or 0),
            "spare_items": int(getattr(obj, "spare_items", None) or getattr(obj, "spare_count", 0) or 0),
            "equipment": getattr(obj, "equipment", None),
            "manufacturer": getattr(obj, "manufacturer", None),
            "supplier": getattr(obj, "supplier", None),
            "dup_count": int(getattr(obj, "dup_count", 0) or 0),
            "created_at": getattr(obj, "created_at"),
        }
        return super().model_validate(data, *args, **kwargs)


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
    from spir_dynamic.app.auth import _hash_password

    if len(body.password.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="Password too long (max 72 characters)")

    # Check username uniqueness
    existing = await db.scalar(select(User).where(User.username == body.username))
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")

    try:
        safe_password = body.password[:72]
        user = User(
            username=body.username,
            email=body.email,
            password_hash=_hash_password(safe_password),
            role=body.role,
            is_active=True,
        )
        db.add(user)
        await db.flush()
        return UserOut.model_validate(user)
    except HTTPException:
        raise
    except Exception as e:
        log.error("User creation failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="User creation failed")


@admin_router.put("/users/{user_id}/password", status_code=204)
async def reset_password(
    user_id: str,
    body: ResetPasswordIn,
    _: TokenData = Depends(require_admin),
    db=Depends(get_db),
) -> None:
    """Reset a user's password. Admin only. New password stored as bcrypt hash."""
    from spir_dynamic.app.auth import _hash_password

    if len(body.new_password.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="Password too long (max 72 characters)")

    user: User | None = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        safe_password = body.new_password[:72]
        user.password_hash = _hash_password(safe_password)
        log.info("Password reset for user '%s' by admin", user.username)
    except HTTPException:
        raise
    except Exception as e:
        log.error("Password reset failed for user_id=%s: %s", user_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Password reset failed")


@admin_router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    td: TokenData = Depends(require_admin),
    db=Depends(get_db),
) -> None:
    """Permanently delete a user and all their data (cascades). Admin only."""
    if td.user_id and td.user_id == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    user: User | None = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    await db.delete(user)
    log.info("User '%s' permanently deleted by admin", user.username)


@admin_router.put("/users/{user_id}/status", status_code=204)
async def set_user_status(
    user_id: str,
    is_active: bool,
    td: TokenData = Depends(require_admin),
    db=Depends(get_db),
) -> None:
    """Activate or deactivate a user. Admin only."""
    if td.user_id and td.user_id == user_id:
        raise HTTPException(status_code=400, detail="Cannot disable your own account")
    user: User | None = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == "admin":
        raise HTTPException(status_code=400, detail="Admin accounts cannot be disabled")
    user.is_active = is_active
    log.info("User '%s' is_active set to %s", user.username, is_active)


# ── Password reset request endpoints ──────────────────────────────────────────

@admin_router.get("/reset-requests", response_model=list[ResetRequestOut])
async def list_reset_requests(
    status: Optional[str] = None,
    td: TokenData = Depends(require_admin),
    db=Depends(get_db),
) -> list[ResetRequestOut]:
    """List password reset requests. Filter by status=pending|resolved. Admin only."""
    from spir_dynamic.db.models import PasswordResetRequest
    q = select(PasswordResetRequest).order_by(desc(PasswordResetRequest.created_at))
    if status:
        q = q.where(PasswordResetRequest.status == status)
    result = await db.execute(q)
    rows = result.scalars().all()
    return [ResetRequestOut.model_validate(r) for r in rows]


@admin_router.put("/reset-requests/{request_id}/resolve", status_code=204)
async def resolve_reset_request(
    request_id: str,
    body: ResolveResetIn,
    td: TokenData = Depends(require_admin),
    db=Depends(get_db),
) -> None:
    """
    Resolve a password reset request: reset the user's password and mark the request resolved.
    Password is stored as bcrypt hash only — admin never sees the previous password.
    """
    from spir_dynamic.app.auth import _hash_password
    from spir_dynamic.db.models import PasswordResetRequest

    req: PasswordResetRequest | None = await db.get(PasswordResetRequest, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.status == "resolved":
        raise HTTPException(status_code=400, detail="Request already resolved")

    user: User | None = await db.scalar(select(User).where(User.username == req.username))
    if user is None:
        raise HTTPException(status_code=404, detail=f"User '{req.username}' not found")

    if len(body.new_password.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="Password too long (max 72 characters)")

    user.password_hash = _hash_password(body.new_password[:72])
    req.status = "resolved"
    req.resolved_at = datetime.now(timezone.utc)
    req.resolved_by = td.username

    log.info(
        "Password reset for user '%s' via reset request '%s', resolved by admin '%s'",
        user.username, request_id, td.username,
    )


# ── Stats endpoint ────────────────────────────────────────────────────────────

@admin_router.get("/stats")
async def get_stats(
    _: TokenData = Depends(require_admin),
    db=Depends(get_db),
) -> dict:
    """System-wide extraction and user statistics. Admin only."""
    from sqlalchemy import func, text
    from datetime import timezone

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    total_users = await db.scalar(select(func.count()).select_from(User))
    active_users = await db.scalar(
        select(func.count()).select_from(User).where(User.is_active == True)
    )
    total_extractions = await db.scalar(
        select(func.count()).select_from(ExtractionHistory)
    )
    today_extractions = await db.scalar(
        select(func.count())
        .select_from(ExtractionHistory)
        .where(ExtractionHistory.created_at >= today_start)
    )
    return {
        "total_users": int(total_users or 0),
        "active_users": int(active_users or 0),
        "total_extractions": int(total_extractions or 0),
        "today_extractions": int(today_extractions or 0),
    }


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
