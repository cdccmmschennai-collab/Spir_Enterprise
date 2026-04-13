"""
FastAPI route handlers.
"""
from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from spir_dynamic.app.auth import get_current_user, TokenData
from spir_dynamic.app.pipeline import run_pipeline, retrieve_result
from spir_dynamic.app.config import get_settings
from spir_dynamic.extraction.file_validator import ValidationError
from spir_dynamic.services.currency_service import conversion_summary

log = logging.getLogger(__name__)

router = APIRouter()


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

    # Audit log (fire-and-forget — never blocks response)
    if td.user_id:
        from spir_dynamic.services.audit_service import log_extraction
        ip = _get_ip(request)
        background_tasks.add_task(
            _run_async, log_extraction(td.user_id, td.jti, result, ip)
        )

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

    # Audit log
    if td.user_id:
        from spir_dynamic.services.audit_service import log_download
        ip = _get_ip(request)
        background_tasks.add_task(
            _run_async, log_download(td.user_id, td.jti, file_id, ip)
        )

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
