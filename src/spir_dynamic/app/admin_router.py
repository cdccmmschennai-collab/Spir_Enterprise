"""
Admin-only endpoints for user management, branch management, and audit log access.

Role hierarchy:
  super_admin  — full platform access, can create branch_admins and super_admins
  branch_admin — scoped to their own branch; can only create regular users
  user         — no access to any admin endpoint
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc, text

from spir_dynamic.app.auth import (
    get_current_user,
    TokenData,
    SUPER_ADMIN,
    BRANCH_ADMIN,
    USER,
    require_super_admin,
    require_branch_admin_or_above,
    validate_password_policy,
)
from spir_dynamic.db.database import get_db, is_db_enabled
from spir_dynamic.db.models import Branch, User, Session, UserActivityLog, ExtractionHistory

log = structlog.stdlib.get_logger(__name__)

admin_router = APIRouter()


# ── Legacy alias — existing code that imports require_admin keeps working ─────

async def require_admin(td: TokenData = Depends(get_current_user)) -> TokenData:
    return await require_branch_admin_or_above(td)


# ── Helper — assert caller can manage target user ─────────────────────────────

def _assert_branch_access(td: TokenData, target_branch_id: Optional[str]) -> None:
    """Raise 403 if a branch_admin tries to act on a user outside their branch."""
    if td.role == BRANCH_ADMIN:
        if target_branch_id != td.branch_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only manage users within your own branch",
            )


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class BranchOut(BaseModel):
    id: str
    name: str
    country: Optional[str]
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class CreateBranchIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    country: Optional[str] = Field(None, max_length=100)


class UserOut(BaseModel):
    id: str
    username: str
    email: Optional[str]
    role: str
    branch_id: Optional[str]
    is_active: bool
    created_at: datetime
    last_login_at: Optional[datetime]
    created_by: Optional[str]

    model_config = {"from_attributes": True}


class CreateUserIn(BaseModel):
    username: str = Field(..., min_length=2, max_length=100)
    password: str = Field(..., min_length=8)
    email: Optional[str] = None
    role: str = Field("user", pattern=r"^(super_admin|branch_admin|user)$")
    branch_id: Optional[str] = None


class ResetPasswordIn(BaseModel):
    new_password: str = Field(..., min_length=8)


class ResetRequestOut(BaseModel):
    id: str
    username: str
    user_id: Optional[str]
    branch_id: Optional[str]
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


# ── Branch management (super_admin only for write, branch_admin_or_above for read) ──

@admin_router.get("/branches", response_model=list[BranchOut])
async def list_branches(
    _: TokenData = Depends(require_branch_admin_or_above),
    db=Depends(get_db),
) -> list[BranchOut]:
    """List all branches. Available to branch_admin and super_admin."""
    result = await db.execute(select(Branch).order_by(Branch.name))
    return [BranchOut.model_validate(b) for b in result.scalars().all()]


@admin_router.post("/branches", response_model=BranchOut, status_code=201)
async def create_branch(
    body: CreateBranchIn,
    td: TokenData = Depends(require_super_admin),
    db=Depends(get_db),
) -> BranchOut:
    """Create a new branch. Super-admin only."""
    existing = await db.scalar(select(Branch).where(Branch.name == body.name))
    if existing:
        raise HTTPException(status_code=409, detail="Branch name already exists")
    branch = Branch(name=body.name, country=body.country, is_active=True)
    db.add(branch)
    await db.flush()
    log.info("branch.created", name=body.name, by=td.username)
    return BranchOut.model_validate(branch)


@admin_router.put("/branches/{branch_id}/status", status_code=204)
async def set_branch_status(
    branch_id: str,
    is_active: bool,
    td: TokenData = Depends(require_super_admin),
    db=Depends(get_db),
) -> None:
    """Activate or deactivate a branch. Super-admin only."""
    branch: Branch | None = await db.get(Branch, branch_id)
    if branch is None:
        raise HTTPException(status_code=404, detail="Branch not found")
    branch.is_active = is_active
    log.info("branch.status_changed", name=branch.name, is_active=is_active, by=td.username)


# ── User management endpoints ──────────────────────────────────────────────────

@admin_router.get("/users", response_model=list[UserOut])
async def list_users(
    td: TokenData = Depends(require_branch_admin_or_above),
    db=Depends(get_db),
) -> list[UserOut]:
    """
    List users. Super-admin sees all; branch_admin sees only their branch.
    """
    q = select(User).order_by(User.created_at)
    if td.role == BRANCH_ADMIN:
        q = q.where(User.branch_id == td.branch_id)
    result = await db.execute(q)
    return [UserOut.model_validate(u) for u in result.scalars().all()]


@admin_router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    body: CreateUserIn,
    td: TokenData = Depends(require_branch_admin_or_above),
    db=Depends(get_db),
) -> UserOut:
    """
    Create a new user.
    - super_admin: can create any role in any branch
    - branch_admin: can only create 'user' role in their own branch
    """
    from spir_dynamic.app.auth import _hash_password
    from spir_dynamic.services.audit_service import log_activity

    # Branch admin restrictions
    if td.role == BRANCH_ADMIN:
        if body.role != USER:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Branch admins can only create regular users",
            )
        body.branch_id = td.branch_id  # force to caller's branch

    # Only super_admin can create branch_admin or super_admin
    if body.role in (BRANCH_ADMIN, SUPER_ADMIN) and td.role != SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only super-admin can create admin accounts",
        )

    if len(body.password.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="Password too long (max 72 characters)")
    validate_password_policy(body.password)

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
            branch_id=body.branch_id,
            is_active=True,
            created_by=td.username,
        )
        db.add(user)
        await db.flush()
        log.info("user.created", username=body.username, role=body.role, branch_id=body.branch_id, by=td.username)

        # Audit log
        if td.user_id:
            import asyncio
            asyncio.ensure_future(log_activity(
                user_id=td.user_id,
                action="user_created",
                details={"new_username": body.username, "role": body.role, "branch_id": body.branch_id},
            ))

        return UserOut.model_validate(user)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("user.create_failed", exc_message=str(e))
        raise HTTPException(status_code=500, detail="User creation failed")


@admin_router.put("/users/{user_id}/password", status_code=204)
async def reset_password(
    user_id: str,
    body: ResetPasswordIn,
    td: TokenData = Depends(require_branch_admin_or_above),
    db=Depends(get_db),
) -> None:
    """Reset a user's password. Branch_admin can only reset passwords within their branch."""
    from spir_dynamic.app.auth import _hash_password

    if len(body.new_password.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="Password too long (max 72 characters)")
    validate_password_policy(body.new_password)

    user: User | None = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    _assert_branch_access(td, user.branch_id)

    try:
        safe_password = body.new_password[:72]
        user.password_hash = _hash_password(safe_password)
        log.info("user.password_reset", username=user.username, by=td.username)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("user.password_reset_failed", user_id=user_id, exc_message=str(e))
        raise HTTPException(status_code=500, detail="Password reset failed")


@admin_router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    td: TokenData = Depends(require_branch_admin_or_above),
    db=Depends(get_db),
) -> None:
    """
    Soft-delete (deactivate) a user. Hard-delete is not permitted.
    - super_admin users can NEVER be deleted.
    - branch_admin can only delete users within their own branch.
    """
    from spir_dynamic.services.audit_service import log_activity

    if td.user_id and td.user_id == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    user: User | None = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Super-admins cannot be deleted by anyone
    if user.role == SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super-admin accounts cannot be deleted",
        )

    _assert_branch_access(td, user.branch_id)

    # Soft delete — preserve audit trail
    user.is_active = False
    log.info("user.soft_deleted", username=user.username, by=td.username)

    if td.user_id:
        import asyncio
        asyncio.ensure_future(log_activity(
            user_id=td.user_id,
            action="user_deleted",
            details={"deleted_username": user.username},
        ))


@admin_router.put("/users/{user_id}/status", status_code=204)
async def set_user_status(
    user_id: str,
    is_active: bool,
    td: TokenData = Depends(require_branch_admin_or_above),
    db=Depends(get_db),
) -> None:
    """Activate or deactivate a user. Super-admins cannot be deactivated."""
    if td.user_id and td.user_id == user_id:
        raise HTTPException(status_code=400, detail="Cannot disable your own account")

    user: User | None = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    if user.role == SUPER_ADMIN:
        raise HTTPException(status_code=400, detail="Super-admin accounts cannot be disabled")

    _assert_branch_access(td, user.branch_id)

    user.is_active = is_active
    log.info("user.status_changed", username=user.username, is_active=is_active, by=td.username)


@admin_router.delete("/users/{user_id}/permanent", status_code=204)
async def permanent_delete_user(
    user_id: str,
    td: TokenData = Depends(require_super_admin),
    db=Depends(get_db),
) -> None:
    """
    Permanently and irrecoverably delete a user and ALL their data.
    Super-admin only. Super-admin accounts cannot be permanently deleted.

    Cascade:
    - ExtractionHistory rows (+ json_path files + redis keys)
    - UserActivityLog rows
    - Session rows
    - Job rows
    - The User row itself
    """
    from spir_dynamic.services.audit_service import log_activity
    from spir_dynamic.app.config import get_settings
    from pathlib import Path
    from sqlalchemy import delete as sa_delete

    if td.user_id and td.user_id == user_id:
        raise HTTPException(status_code=400, detail="Cannot permanently delete your own account")

    user: User | None = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    if user.role == SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super-admin accounts cannot be permanently deleted",
        )

    # --- Cleanup disk files and redis keys before deleting DB rows ---
    cfg = get_settings()
    storage_root = Path(cfg.rows_storage_path).resolve()

    history_q = select(ExtractionHistory).where(ExtractionHistory.user_id == user_id)
    history_result = await db.execute(history_q)
    history_rows = history_result.scalars().all()

    for rec in history_rows:
        # Delete JSON row files from disk
        if rec.json_path:
            p = Path(rec.json_path).resolve()
            try:
                p.relative_to(storage_root)
                if p.exists():
                    p.unlink()
                    log.info("permanent_delete.json_removed", path=str(p))
            except ValueError:
                log.warning("permanent_delete.path_rejected", path=str(p))
            except Exception as exc:
                log.warning("permanent_delete.file_cleanup_failed", path=str(p), exc_message=str(exc))

        # Delete from redis if available
        if rec.file_id:
            try:
                from spir_dynamic.services.redis_store import RedisStorage
                rs = RedisStorage(cfg.redis_url)
                rs.delete(rec.file_id)
                rs.delete(f"rows:{rec.file_id}")
            except Exception as exc:
                log.warning("permanent_delete.redis_cleanup_failed", file_id=rec.file_id, exc_message=str(exc))

    # --- Cascade delete DB rows ---
    # SQLAlchemy cascade handles Sessions, UserActivityLog, ExtractionHistory, Jobs
    # via ondelete="CASCADE" FKs. Calling db.delete(user) is sufficient.
    deleted_username = user.username

    # Audit log BEFORE deletion so td.user_id is still valid
    if td.user_id:
        import asyncio
        asyncio.ensure_future(log_activity(
            user_id=td.user_id,
            action="user_permanently_deleted",
            details={
                "deleted_user_id": user_id,
                "deleted_username": deleted_username,
                "files_purged": len(history_rows),
            },
        ))

    await db.delete(user)
    log.warning(
        "user.permanently_deleted",
        deleted_username=deleted_username,
        by=td.username,
        files_purged=len(history_rows),
    )


@admin_router.put("/users/{user_id}/role", status_code=204)
async def change_user_role(
    user_id: str,
    new_role: str,
    td: TokenData = Depends(require_super_admin),
    db=Depends(get_db),
) -> None:
    """
    Change a user's role. Super-admin only.
    Users cannot change their own role.
    Super-admin accounts cannot have their role changed.
    """
    from spir_dynamic.services.audit_service import log_activity

    if new_role not in (SUPER_ADMIN, BRANCH_ADMIN, USER):
        raise HTTPException(status_code=400, detail="Invalid role")

    if td.user_id and td.user_id == user_id:
        raise HTTPException(status_code=400, detail="Cannot change your own role")

    user: User | None = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    if user.role == SUPER_ADMIN and new_role != SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot demote a super-admin account",
        )

    old_role = user.role
    user.role = new_role
    log.info("user.role_changed", username=user.username, old_role=old_role, new_role=new_role, by=td.username)

    if td.user_id:
        import asyncio
        asyncio.ensure_future(log_activity(
            user_id=td.user_id,
            action="role_changed",
            details={"target_username": user.username, "old_role": old_role, "new_role": new_role},
        ))


@admin_router.put("/users/{user_id}/branch", status_code=204)
async def assign_branch(
    user_id: str,
    branch_id: Optional[str],
    td: TokenData = Depends(require_super_admin),
    db=Depends(get_db),
) -> None:
    """Assign or reassign a user to a branch. Super-admin only."""
    from spir_dynamic.services.audit_service import log_activity

    user: User | None = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    if branch_id is not None:
        branch: Branch | None = await db.get(Branch, branch_id)
        if branch is None:
            raise HTTPException(status_code=404, detail="Branch not found")

    old_branch = user.branch_id
    user.branch_id = branch_id
    log.info("user.branch_assigned", username=user.username, old_branch=old_branch, new_branch=branch_id, by=td.username)

    if td.user_id:
        import asyncio
        asyncio.ensure_future(log_activity(
            user_id=td.user_id,
            action="branch_assigned",
            details={"target_username": user.username, "old_branch_id": old_branch, "new_branch_id": branch_id},
        ))


# ── Password reset request endpoints ──────────────────────────────────────────

@admin_router.get("/reset-requests", response_model=list[ResetRequestOut])
async def list_reset_requests(
    status: Optional[str] = None,
    td: TokenData = Depends(require_branch_admin_or_above),
    db=Depends(get_db),
) -> list[ResetRequestOut]:
    """
    List password reset requests. Filter by status=pending|resolved.
    Super-admin sees all requests; branch_admin sees only their branch's requests.
    """
    from spir_dynamic.db.models import PasswordResetRequest
    q = select(PasswordResetRequest).order_by(desc(PasswordResetRequest.created_at))
    if td.role == BRANCH_ADMIN and td.branch_id:
        q = q.where(PasswordResetRequest.branch_id == td.branch_id)
    if status:
        q = q.where(PasswordResetRequest.status == status)
    result = await db.execute(q)
    rows = result.scalars().all()
    return [ResetRequestOut.model_validate(r) for r in rows]


@admin_router.put("/reset-requests/{request_id}/resolve", status_code=204)
async def resolve_reset_request(
    request_id: str,
    body: ResolveResetIn,
    td: TokenData = Depends(require_branch_admin_or_above),
    db=Depends(get_db),
) -> None:
    """Resolve a password reset request. Admin only."""
    from spir_dynamic.app.auth import _hash_password
    from spir_dynamic.db.models import PasswordResetRequest

    req: PasswordResetRequest | None = await db.get(PasswordResetRequest, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.status == "resolved":
        raise HTTPException(status_code=400, detail="Request already resolved")

    # Prefer stored user_id (set at request creation); fall back to username lookup for
    # legacy rows that predate the user_id column.
    if req.user_id:
        user: User | None = await db.get(User, req.user_id)
    else:
        user = await db.scalar(select(User).where(User.username == req.username))

    if user is None:
        raise HTTPException(status_code=404, detail=f"User '{req.username}' not found")

    # Branch access: use stored branch_id from request (stable), fall back to current user branch
    _assert_branch_access(td, req.branch_id if req.branch_id is not None else user.branch_id)

    if len(body.new_password.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="Password too long (max 72 characters)")
    validate_password_policy(body.new_password)

    user.password_hash = _hash_password(body.new_password[:72])
    req.status = "resolved"
    req.resolved_at = datetime.now(timezone.utc)
    req.resolved_by = td.username

    log.info("password_reset_resolved", username=user.username, request_id=request_id, by=td.username)


# ── Stats endpoint ────────────────────────────────────────────────────────────

@admin_router.get("/stats")
async def get_stats(
    td: TokenData = Depends(require_branch_admin_or_above),
    db=Depends(get_db),
) -> dict:
    """
    System statistics.
    Super-admin: global counts.
    Branch-admin: counts scoped to their branch.
    """
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    if td.role == BRANCH_ADMIN and td.branch_id:
        branch_user_ids = select(User.id).where(User.branch_id == td.branch_id)

        total_users = await db.scalar(
            select(func.count()).select_from(User).where(User.branch_id == td.branch_id)
        )
        active_users = await db.scalar(
            select(func.count()).select_from(User).where(
                User.branch_id == td.branch_id,
                User.is_active == True,
            )
        )
        total_extractions = await db.scalar(
            select(func.count()).select_from(ExtractionHistory).where(
                ExtractionHistory.user_id.in_(branch_user_ids)
            )
        )
        today_extractions = await db.scalar(
            select(func.count()).select_from(ExtractionHistory).where(
                ExtractionHistory.user_id.in_(branch_user_ids),
                ExtractionHistory.created_at >= today_start,
            )
        )
    else:
        total_users = await db.scalar(select(func.count()).select_from(User))
        active_users = await db.scalar(
            select(func.count()).select_from(User).where(User.is_active == True)
        )
        total_extractions = await db.scalar(
            select(func.count()).select_from(ExtractionHistory)
        )
        today_extractions = await db.scalar(
            select(func.count()).select_from(ExtractionHistory).where(
                ExtractionHistory.created_at >= today_start
            )
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
    td: TokenData = Depends(require_branch_admin_or_above),
    db=Depends(get_db),
) -> list[ActivityLogOut]:
    """Get user activity logs. Branch-scoped for branch_admin."""
    q = select(UserActivityLog).order_by(desc(UserActivityLog.created_at))

    if td.role == BRANCH_ADMIN and td.branch_id:
        branch_user_ids = select(User.id).where(User.branch_id == td.branch_id)
        q = q.where(UserActivityLog.user_id.in_(branch_user_ids))

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
    td: TokenData = Depends(require_branch_admin_or_above),
    db=Depends(get_db),
) -> list[ExtractionHistoryOut]:
    """Get extraction history. Branch-scoped for branch_admin."""
    q = select(ExtractionHistory).order_by(desc(ExtractionHistory.created_at))

    if td.role == BRANCH_ADMIN and td.branch_id:
        branch_user_ids = select(User.id).where(User.branch_id == td.branch_id)
        q = q.where(ExtractionHistory.user_id.in_(branch_user_ids))

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
