"""
Batch extraction API — accept multiple files, process via Celery queue, combine results.
"""
from typing import Annotated, List
from fastapi import File, UploadFile

import asyncio
import io
import json
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from spir_dynamic.app.auth import get_current_user, TokenData
from spir_dynamic.app.config import get_settings
from spir_dynamic.app.pipeline import run_pipeline
from spir_dynamic.services.job_store import FileResult, get_job_store
from spir_dynamic.services.storage import get_storage
from spir_dynamic.services.zip_builder import build_zip

log = logging.getLogger(__name__)

batch_router = APIRouter()


@batch_router.post("/extract")
async def batch_extract(
    files: Annotated[List[UploadFile], File(...)],
    td: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Accept 1–N files. Launch extraction for each file concurrently.
    Returns job_id immediately — poll GET /api/batch/{job_id} for status.
    """
    cfg = get_settings()
    if len(files) > cfg.batch_max_files:
        raise HTTPException(
            status_code=400,
            detail=f"Max {cfg.batch_max_files} files per batch request",
        )

    job_id = str(uuid.uuid4())
    filenames = [f.filename or f"file_{i}.xlsx" for i, f in enumerate(files)]
    user_id = td.user_id or ""
    get_job_store().create(job_id, filenames, user_id=user_id)

    # Read all file contents before launching tasks (UploadFile is not thread-safe)
    file_data: list[tuple[bytes, str]] = [
        (await f.read(), name) for f, name in zip(files, filenames)
    ]

    if cfg.celery_enabled:
        _dispatch_celery(job_id, file_data, cfg.batch_ttl_seconds, user_id)
    else:
        asyncio.create_task(_process_batch(job_id, file_data))

    asyncio.create_task(_persist_job_to_db(job_id, user_id, filenames, cfg.batch_ttl_seconds))

    return {"job_id": job_id, "total": len(files), "status": "processing"}


@batch_router.get("/{job_id}")
async def batch_status(
    job_id: str,
    td: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """Poll extraction status for a batch job."""
    job = get_job_store().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    _assert_job_access(job.user_id, td)
    return job.to_dict()


@batch_router.get("/{job_id}/download")
async def batch_download(
    job_id: str,
    td: TokenData = Depends(get_current_user),
) -> StreamingResponse:
    """Download a ZIP archive of all successfully extracted files."""
    job = get_job_store().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    _assert_job_access(job.user_id, td)

    storage = get_storage()
    file_pairs: list[tuple[bytes, str]] = []
    for r in job.results:
        if r.status == "ok" and r.file_id:
            entry = storage.get(r.file_id)
            if entry:
                data, name = entry
                file_pairs.append((data if isinstance(data, bytes) else data.read(), name))

    if not file_pairs:
        raise HTTPException(status_code=404, detail="No completed files available for download")

    zip_bytes = build_zip(file_pairs)
    short_id = job_id[:8]
    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="batch_{short_id}.zip"'},
    )


# ── Request models ─────────────────────────────────────────────────────────────

class CombineRequest(BaseModel):
    file_ids: list[str]


# ── Single-file async result (frontend polling UX) ──────────────────────────────

@batch_router.get("/{job_id}/result")
async def batch_single_result(
    job_id: str,
    td: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Return full extraction result for a single-file async job.

    The frontend polls this after POSTing to /api/batch/extract with one file.
    Returns { status: "processing" } while the worker runs, then a payload that
    mirrors the synchronous /api/extract response so existing preview and download
    UI works without modification. Download uses GET /api/download/{file_id}.
    """
    job = get_job_store().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    _assert_job_access(job.user_id, td)

    if not job.results:
        return {"status": "processing", "completed": 0, "total": job.total}

    result = job.results[0]

    if result.status in ("pending", "running"):
        return {"status": "processing", "completed": job.completed, "total": job.total}

    if result.status == "error":
        return {"status": "error", "error": result.error or "Extraction failed"}

    # status == "ok" — fetch full payload stored by the Celery worker
    storage = get_storage()
    entry = storage.get(f"rows:{result.file_id}")
    if entry is None:
        # Row data expired or not stored (e.g. celery_enabled=False fallback).
        return {
            "status": "done",
            "file_id": result.file_id,
            "filename": result.filename,
            "format": "",
            "spir_no": result.spir_no,
            "equipment": "",
            "manufacturer": "",
            "supplier": "",
            "spir_type": None,
            "eqpt_qty": 0,
            "spare_items": result.total_rows,
            "total_tags": result.total_tags,
            "annexure_count": 0,
            "total_rows": result.total_rows,
            "dup1_count": 0,
            "sap_count": 0,
            "preview_cols": [],
            "preview_rows": [],
        }

    raw_bytes, _ = entry
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Result data corrupted: {exc}")

    return {
        "status": "done",
        "file_id": result.file_id,
        "filename": payload.get("filename", result.filename),
        "format": payload.get("format", ""),
        "spir_no": payload.get("spir_no", result.spir_no),
        "equipment": payload.get("equipment", ""),
        "manufacturer": payload.get("manufacturer", ""),
        "supplier": payload.get("supplier", ""),
        "spir_type": payload.get("spir_type"),
        "eqpt_qty": payload.get("eqpt_qty", 0),
        "spare_items": payload.get("spare_items", 0),
        "total_tags": result.total_tags,
        "annexure_count": payload.get("annexure_count", 0),
        "total_rows": result.total_rows,
        "dup1_count": payload.get("dup1_count", 0),
        "sap_count": payload.get("sap_count", 0),
        "preview_cols": payload.get("cols", []),
        "preview_rows": payload.get("rows", []),
    }


# ── Per-file preview ────────────────────────────────────────────────────────────

@batch_router.get("/{job_id}/preview/{file_idx}")
async def batch_file_preview(
    job_id: str,
    file_idx: int,
    td: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Return extracted row data for ONE file in the batch.

    The frontend calls this to show a preview table for an individual file
    before the user decides which files to combine.

    Row data is stored in Redis by the Celery worker immediately after extraction
    (key: rows:{file_id}) and expires after batch_ttl_seconds.
    """
    job = get_job_store().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    _assert_job_access(job.user_id, td)

    if file_idx < 0 or file_idx >= job.total:
        raise HTTPException(status_code=400, detail=f"file_idx must be 0–{job.total - 1}")

    result = job.results[file_idx]
    if result.status != "ok":
        raise HTTPException(
            status_code=400,
            detail=f"File '{result.filename}' has status '{result.status}' — preview only available for completed files",
        )
    if not result.file_id:
        raise HTTPException(status_code=404, detail="File ID not recorded for this result")

    storage = get_storage()
    entry = storage.get(f"rows:{result.file_id}")
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail="Row data not found — it may have expired or was not stored (check worker version)",
        )

    raw_bytes, _ = entry
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Row data corrupted: {exc}")

    return {
        "filename": result.filename,
        "spir_no": payload.get("spir_no", result.spir_no),
        "total_rows": result.total_rows,
        "total_tags": result.total_tags,
        "cols": payload.get("cols", []),
        "rows": payload.get("rows", []),
    }


# ── Selective combine ────────────────────────────────────────────────────────────

@batch_router.post("/{job_id}/combine")
async def batch_combine(
    job_id: str,
    body: CombineRequest,
    td: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Combine extracted row data from selected files into ONE Excel file.

    Uses already-extracted rows stored in Redis — does NOT re-run extraction.
    The combined file is stored with a new file_id and downloadable via
    GET /api/download/{file_id} (the standard single-file download endpoint).

    Request body: {"file_ids": ["uuid1", "uuid2", ...]}
    """
    job = get_job_store().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    _assert_job_access(job.user_id, td)

    if not body.file_ids:
        raise HTTPException(status_code=400, detail="file_ids must not be empty")

    # Security: only allow file_ids that belong to this job
    valid_file_ids = {r.file_id for r in job.results if r.file_id and r.status == "ok"}
    invalid = [fid for fid in body.file_ids if fid not in valid_file_ids]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"These file_ids do not belong to job {job_id} or are not yet complete: {invalid}",
        )

    loop = asyncio.get_event_loop()
    try:
        combined_file_id, out_filename, total_rows = await loop.run_in_executor(
            None, _do_combine, body.file_ids
        )
    except Exception as exc:
        log.error("Combine failed | job=%s: %s", job_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Combine failed: {exc}")

    return {
        "file_id": combined_file_id,
        "filename": out_filename,
        "total_rows": total_rows,
        "file_count": len(body.file_ids),
    }


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _do_combine(file_ids: list[str]) -> tuple[str, str, int]:
    """
    Synchronous combine worker — runs in thread pool via run_in_executor.

    Reads pre-extracted row data from Redis for each file_id, concatenates
    all rows, builds one styled Excel file, stores it, and returns the new
    file_id, filename, and total row count.
    """
    from spir_dynamic.services.excel_builder import build_xlsx

    storage = get_storage()
    all_rows: list[list] = []
    missing: list[str] = []

    for fid in file_ids:
        entry = storage.get(f"rows:{fid}")
        if entry is None:
            missing.append(fid)
            continue
        raw_bytes, _ = entry
        payload = json.loads(raw_bytes.decode("utf-8"))
        all_rows.extend(payload.get("rows", []))

    if missing:
        raise RuntimeError(
            f"Row data missing or expired for {len(missing)} file(s): {missing[:3]}{'...' if len(missing) > 3 else ''}"
        )

    xlsx_bytes = build_xlsx(all_rows, "COMBINED")
    combined_id = str(uuid.uuid4())
    out_filename = "COMBINED_Extraction.xlsx"
    cfg = get_settings()
    storage.put(combined_id, xlsx_bytes, out_filename, ttl=cfg.batch_ttl_seconds)

    log.info("Combine complete | file_count=%d total_rows=%d file_id=%s",
             len(file_ids), len(all_rows), combined_id)
    return combined_id, out_filename, len(all_rows)


def _dispatch_celery(
    job_id: str,
    file_data: list[tuple[bytes, str]],
    ttl_seconds: int,
    user_id: str = "",
) -> None:
    """
    Store each file's bytes in Redis, then enqueue one Celery task per file.

    The API returns immediately after this call. Workers pick up tasks from the
    Redis queue independently, in separate processes.

    user_id is passed explicitly so the worker can write extraction_history
    without needing to look it up from the job store (which can race or be
    unavailable by the time the task runs).

    Input bytes TTL = ttl_seconds + 600 so the file survives in Redis even if
    all workers are busy and a task is delayed by queue backlog or retries
    (max backoff = 10 * 3^2 = 90s × 3 retries = 270s, well within 600s).
    """
    from spir_dynamic.tasks.extraction_tasks import process_file_task

    storage = get_storage()
    input_ttl = ttl_seconds + 600

    for idx, (file_bytes, filename) in enumerate(file_data):
        input_key = f"input:{job_id}:{idx}"
        storage.put(input_key, file_bytes, filename, ttl=input_ttl)
        process_file_task.delay(job_id, idx, input_key, filename, user_id)
        log.debug("Celery task enqueued | job=%s idx=%d file=%s", job_id, idx, filename)


def _assert_job_access(job_user_id: str, td: TokenData) -> None:
    """Raise 403 if a non-admin caller tries to access another user's job."""
    if td.role == "admin":
        return
    caller_id = td.user_id or ""
    if job_user_id and caller_id and job_user_id != caller_id:
        raise HTTPException(status_code=403, detail="Access denied")


async def _persist_job_to_db(
    job_id: str,
    user_id: str,
    filenames: list[str],
    ttl_seconds: int,
) -> None:
    """Fire-and-forget: write the batch job to PostgreSQL for persistence."""
    from spir_dynamic.db.database import is_db_enabled, get_session_factory
    from spir_dynamic.db.models import Job

    if not is_db_enabled() or not user_id:
        return
    try:
        factory = get_session_factory()
        now = datetime.now(timezone.utc)
        async with factory() as db:
            job = Job(
                id=job_id,
                user_id=user_id,
                status="processing",
                total_files=len(filenames),
                completed_files=0,
                succeeded_files=0,
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            db.add(job)
            await db.commit()
    except Exception as exc:
        log.warning("Batch job DB persist failed (non-fatal): %s", exc)


async def _process_batch(job_id: str, file_data: list[tuple[bytes, str]]) -> None:
    """Background coroutine: run each file through the pipeline in a thread pool."""
    loop = asyncio.get_event_loop()
    store = get_job_store()

    async def _extract_one(idx: int, content: bytes, filename: str) -> None:
        try:
            result = await loop.run_in_executor(None, run_pipeline, content, filename)
            store.update_result(job_id, idx, FileResult(
                filename=filename,
                status="ok",
                total_rows=result.get("total_rows", 0),
                total_tags=result.get("total_tags", 0),
                spir_no=result.get("spir_no", ""),
                file_id=result.get("file_id", ""),
            ))
        except Exception as exc:
            log.error("Batch extraction failed [%s]: %s", filename, exc, exc_info=True)
            store.update_result(job_id, idx, FileResult(
                filename=filename,
                status="error",
                error=str(exc),
            ))

    await asyncio.gather(*[
        _extract_one(i, content, name)
        for i, (content, name) in enumerate(file_data)
    ])
