"""
FastAPI route handlers.
"""
from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select, desc

from spir_dynamic.app.auth import get_current_user, TokenData
from spir_dynamic.app.pipeline import run_pipeline, retrieve_result
from spir_dynamic.app.config import get_settings
from spir_dynamic.db.database import get_db, is_db_enabled
from spir_dynamic.db.models import ExtractionHistory
from spir_dynamic.extraction.file_validator import ValidationError
from spir_dynamic.services.currency_service import conversion_summary

log = logging.getLogger(__name__)

router = APIRouter()


class HistoryItem(BaseModel):
    id: str
    filename: str
    spir_no: Optional[str]
    tag_count: int
    spare_count: int
    created_at: datetime
    model_config = {"from_attributes": True}


@router.post("/extract")
async def extract(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    td: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """Upload and extract a SPIR Excel file."""
    cfg = get_settings()

    content = await file.read()
    filename = file.filename or "upload.xlsx"

    # Size check
    size_mb = len(content) / (1024 * 1024)
    if size_mb > cfg.max_file_size_mb:
        raise HTTPException(
            status_code=413,
            detail=f"File is {size_mb:.1f} MB — exceeds {cfg.max_file_size_mb} MB limit",
        )

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, run_pipeline, content, filename)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        log.error("Extraction failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Extraction failed: {exc}")

    from spir_dynamic.services.audit_service import log_extraction
    ip = _get_ip(request)
    try:
        await log_extraction(td.user_id, td.jti, result, ip, original_filename=filename)
    except Exception as e:
        log.error("History logging failed (extraction still succeeded): %s", e)

    return result


@router.get("/download/{file_id}")
async def download(
    file_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    td: TokenData = Depends(get_current_user),
):
    """Download the extracted Excel file."""
    result = retrieve_result(file_id)
    if result is None:
        raise HTTPException(status_code=404, detail="File not found or expired")

    data, filename = result

    # Audit log — direct await (re-enable background_tasks after fix)
    from spir_dynamic.services.audit_service import log_download
    ip = _get_ip(request)
    await log_download(td.user_id, None, file_id, ip)

    # Ensure we have bytes
    if isinstance(data, io.BytesIO):
        data = data.read()

    safe_name = filename.replace('"', "'")

    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@router.get("/health")
async def health() -> dict[str, str]:
    cfg = get_settings()
    from spir_dynamic.db.database import is_db_enabled
    return {
        "status": "healthy",
        "version": cfg.app_version,
        "db_mode": "enabled" if is_db_enabled() else "legacy",
    }


@router.get("/currencies")
async def currencies() -> dict:
    return conversion_summary()


@router.get("/me")
async def me(
    td: TokenData = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    """Return the authenticated user's profile."""
    if is_db_enabled() and td.user_id and db is not None:
        from spir_dynamic.db.models import User
        user = await db.get(User, td.user_id)
        count_row = await db.execute(
            select(func.count(ExtractionHistory.id)).where(ExtractionHistory.user_id == td.user_id)
        )
        total_files = count_row.scalar() or 0
        if user:
            return {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "role": user.role,
                "created_at": user.created_at,
                "total_files_extracted": total_files,
            }
    return {
        "id": None,
        "username": td.username,
        "email": None,
        "role": "user",
        "created_at": None,
        "total_files_extracted": 0,
    }


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


async def _run_async(coro) -> None:
    """Wrapper so BackgroundTasks can schedule an async coroutine."""
    try:
        await coro
    except Exception as exc:
        log.debug("Background audit task error: %s", exc)


# ── History endpoints ──────────────────────────────────────────────────────────

@router.get("/history", response_model=list[HistoryItem])
async def list_history(
    limit: int = 50,
    offset: int = 0,
    td: TokenData = Depends(get_current_user),
    db=Depends(get_db),
) -> list[HistoryItem]:
    """Return the current user's extraction history."""
    if not is_db_enabled():
        return []
    if not td.user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    q = (
        select(ExtractionHistory)
        .where(ExtractionHistory.user_id == td.user_id)
        .order_by(desc(ExtractionHistory.created_at))
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(q)
    rows = result.scalars().all()
    # Legacy rows may contain blank/NULL values due to older defaults or schema drift.
    # Normalize the response without changing its shape.
    _ist = ZoneInfo("Asia/Kolkata")
    items: list[HistoryItem] = []
    for r in rows:
        safe_filename = getattr(r, "filename", None) or getattr(r, "original_filename", None) or "—"
        raw_ts = r.created_at
        if raw_ts:
            if raw_ts.tzinfo is None:
                raw_ts = raw_ts.replace(tzinfo=ZoneInfo("UTC"))
            ist_ts = raw_ts.astimezone(_ist)
        else:
            ist_ts = raw_ts
        items.append(
            HistoryItem(
                id=r.id,
                filename=safe_filename,
                spir_no=getattr(r, "spir_no", None),
                tag_count=int(getattr(r, "tag_count", 0) or 0),
                spare_count=int(getattr(r, "spare_count", 0) or 0),
                created_at=ist_ts,
            )
        )
    return items


