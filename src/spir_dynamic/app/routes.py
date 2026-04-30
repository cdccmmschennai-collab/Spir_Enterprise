"""
FastAPI route handlers.
"""
from __future__ import annotations

import base64
import io
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from spir_dynamic.app.auth import get_current_user, TokenData
from spir_dynamic.app.pipeline import retrieve_result
from spir_dynamic.app.config import get_settings
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
    """Upload a SPIR Excel file and enqueue a background extraction job."""
    cfg = get_settings()

    content = await file.read()
    filename = file.filename or "upload.xlsx"

    size_mb = len(content) / (1024 * 1024)
    if size_mb > cfg.max_file_size_mb:
        raise HTTPException(
            status_code=413,
            detail=f"File is {size_mb:.1f} MB — exceeds {cfg.max_file_size_mb} MB limit",
        )

    if not cfg.redis_url:
        raise HTTPException(
            status_code=503,
            detail="Background processing unavailable: REDIS_URL is not configured.",
        )

    from spir_dynamic.tasks import extraction_task
    from spir_dynamic.services.job_tracker import get_job_tracker
    task = extraction_task.delay(base64.b64encode(content).decode(), filename)
    get_job_tracker().set_status(task.id, "pending")
    log.info("Enqueued extraction task %s for %s", task.id, filename)

    return {"job_id": task.id, "status": "queued"}


@router.get("/jobs/{job_id}")
async def job_status(
    job_id: str,
    td: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """Poll the status of a background extraction job."""
    from celery.result import AsyncResult
    from spir_dynamic.celery_worker import celery_app

    ar = AsyncResult(job_id, app=celery_app)
    state = ar.state

    if state == "PENDING":
        return {"status": "queued"}
    if state == "STARTED":
        return {"status": "processing"}
    if state == "SUCCESS":
        # Spread ar.result first, then override status — pipeline.py bakes in
        # "status": "done" which would win if placed after the spread.
        return {**ar.result, "status": "completed"}
    if state == "FAILURE":
        log.error("Task %s failed: %s", job_id, ar.result)
        return {"status": "failed", "error": str(ar.result)}
    return {"status": state.lower()}


@router.get("/status/{job_id}")
async def job_status_tracked(
    job_id: str,
    td: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """Job status via tracker — canonical polling endpoint."""
    from spir_dynamic.services.job_tracker import get_job_tracker
    from celery.result import AsyncResult
    from spir_dynamic.celery_worker import celery_app

    data = get_job_tracker().get_status(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found or expired")

    st = data.get("status", "pending")
    progress = int(data.get("progress", 0))

    if st == "completed":
        ar = AsyncResult(job_id, app=celery_app)
        if ar.state == "SUCCESS":
            return {**ar.result, "status": "completed", "job_id": job_id, "progress": 100}
        return {"job_id": job_id, "status": "completed", "progress": 100}

    if st == "failed":
        return {"job_id": job_id, "status": "failed", "progress": progress,
                "error": data.get("error", "Extraction failed")}

    if st == "cancelled":
        return {"job_id": job_id, "status": "cancelled", "progress": progress}

    return {"job_id": job_id, "status": st, "progress": progress}


@router.post("/cancel/{job_id}")
async def cancel_job(
    job_id: str,
    td: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """Request cooperative cancellation of a queued or processing job."""
    from spir_dynamic.services.job_tracker import get_job_tracker

    tracker = get_job_tracker()
    data = tracker.get_status(job_id)

    if data is None:
        raise HTTPException(status_code=404, detail="Job not found or expired")

    st = data.get("status", "")
    if st in {"completed", "failed"}:
        raise HTTPException(status_code=409, detail=f"Job already {st} — cannot cancel")

    if st == "cancelled":
        return {"job_id": job_id, "status": "cancelled"}  # idempotent

    tracker.mark_cancel(job_id)
    return {"job_id": job_id, "status": "cancelled"}


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
async def health() -> dict[str, Any]:
    cfg = get_settings()
    from spir_dynamic.db.database import is_db_enabled
    return {
        "status": "healthy",
        "version": cfg.app_version,
        "db_mode": "enabled" if is_db_enabled() else "legacy",
        "background_processing": bool(cfg.redis_url)
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
