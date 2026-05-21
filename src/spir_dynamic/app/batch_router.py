"""
Batch extraction API — accept multiple files, process via Celery queue, combine results.

Upload flow (safe for large files):
  POST /api/batch/extract
    ├─ Stream each file to storage/batch_uploads/ (never loads all bytes into RAM)
    ├─ Enqueue one Celery task per file — small files → 'normal' queue,
    │  large files → 'heavy' queue (dedicated worker, higher time limits)
    └─ Return job_id immediately; frontend polls GET /api/batch/{job_id}

Workers pick up tasks from their respective queues, process one file at a time
per worker process, and delete the upload file when done.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Annotated, Any, List

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from spir_dynamic.app.auth import get_current_user, TokenData
from spir_dynamic.app.config import get_settings
from spir_dynamic.app.pipeline import run_pipeline
from spir_dynamic.services.cleanup import safe_delete
from spir_dynamic.services.job_store import FileResult, get_job_store
from spir_dynamic.services.storage import get_storage
from spir_dynamic.services.zip_builder import build_zip

log = structlog.stdlib.get_logger(__name__)

batch_router = APIRouter()

# Celery time limits for the heavy queue (files above large_file_threshold_mb).
# Normal queue uses the defaults set in celery_app.py (300s / 360s).
_HEAVY_SOFT_LIMIT = 900   # 15 minutes
_HEAVY_HARD_LIMIT = 1080  # 18 minutes


@batch_router.post("/extract")
async def batch_extract(
    files: Annotated[List[UploadFile], File(...)],
    td: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Accept 1–N files. Stream each to disk, then enqueue one Celery task per file.
    Returns job_id immediately — poll GET /api/batch/{job_id} for status.

    Files above large_file_threshold_mb are routed to the 'heavy' queue so
    normal jobs are never blocked by a single massive extraction.
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

    # Enrich all subsequent log lines for this request with batch context.
    structlog.contextvars.bind_contextvars(job_id=job_id, user_id=user_id, file_count=len(files))

    # Stream all uploads to disk before enqueuing tasks.
    # This is done sequentially — one file at a time — so RAM usage stays flat
    # regardless of how many files are in the batch.
    upload_dir = Path(cfg.batch_upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    file_data: list[tuple[Path, str, int]] = []  # (disk_path, original_name, size_bytes)
    try:
        for idx, (f, name) in enumerate(zip(files, filenames)):
            disk_path, size_bytes = await _stream_batch_upload(
                f, name, upload_dir, job_id, idx, cfg.max_file_size_mb
            )
            file_data.append((disk_path, name, size_bytes))
            log.info(
                "batch.upload_saved",
                job_id=job_id,
                file_idx=idx,
                filename=name,
                size_mb=round(size_bytes / (1024 * 1024), 1),
                path=str(disk_path),
            )
    except HTTPException:
        # Clean up any files already saved if one upload fails
        for p, _, _ in file_data:
            safe_delete(p, log_context=f"upload-abort job={job_id}")
        raise

    if cfg.celery_enabled:
        _dispatch_celery(job_id, file_data, cfg, user_id)
    else:
        asyncio.create_task(_process_batch_from_disk(job_id, file_data))

    asyncio.create_task(_persist_job_to_db(job_id, user_id, filenames, cfg.batch_ttl_seconds))

    return {"job_id": job_id, "total": len(files), "status": "processing"}


@batch_router.post("/register")
async def batch_register(
    body: RegisterRequest,
    td: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Phase 1 of the sequential upload flow.
    Accepts filenames only (no file data), creates the job, returns job_id.
    File uploads follow via POST /api/batch/{job_id}/upload — one per file.
    """
    cfg = get_settings()
    if not body.filenames:
        raise HTTPException(status_code=400, detail="filenames must not be empty")
    if len(body.filenames) > cfg.batch_max_files:
        raise HTTPException(
            status_code=400,
            detail=f"Max {cfg.batch_max_files} files per batch request",
        )

    job_id = str(uuid.uuid4())
    user_id = td.user_id or ""
    get_job_store().create(job_id, body.filenames, user_id=user_id)

    asyncio.create_task(
        _persist_job_to_db(job_id, user_id, body.filenames, cfg.batch_ttl_seconds)
    )

    return {"job_id": job_id, "total": len(body.filenames), "status": "ready"}


@batch_router.post("/{job_id}/upload")
async def batch_upload_file(
    job_id: str,
    file: UploadFile = File(...),
    file_idx: int = Form(...),
    td: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Phase 2 of the sequential upload flow (one call per file).
    Streams the file to disk and dispatches one Celery task.
    Returns immediately — frontend polls GET /api/batch/{job_id} for status.
    """
    cfg = get_settings()
    job_store = get_job_store()

    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    _assert_job_access(job.user_id, td)

    if file_idx < 0 or file_idx >= job.total:
        raise HTTPException(
            status_code=400,
            detail=f"file_idx must be 0–{job.total - 1}",
        )

    filename = job.results[file_idx].filename  # use registered name — avoids client mismatch
    upload_dir = Path(cfg.batch_upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    user_id = td.user_id or ""

    structlog.contextvars.bind_contextvars(job_id=job_id, file_idx=file_idx, filename=filename, user_id=user_id)

    try:
        disk_path, size_bytes = await _stream_batch_upload(
            file, filename, upload_dir, job_id, file_idx, cfg.max_file_size_mb
        )
    except HTTPException as exc:
        # Mark slot as error so polling never stalls on "pending"
        job_store.update_result(
            job_id, file_idx,
            FileResult(filename=filename, status="error", error=exc.detail),
        )
        raise

    log.info(
        "batch.upload_saved",
        job_id=job_id,
        file_idx=file_idx,
        filename=filename,
        size_mb=round(size_bytes / (1024 * 1024), 1),
    )

    if cfg.celery_enabled:
        _dispatch_celery(job_id, [(disk_path, filename, size_bytes)], cfg, user_id,
                         idx_offset=file_idx)
    else:
        asyncio.create_task(
            _process_batch_from_disk(job_id, [(disk_path, filename, size_bytes)],
                                     idx_offset=file_idx)
        )

    return {"status": "queued", "file_idx": file_idx, "filename": filename}


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


# ── Request models ──────────────────────────────────────────────────────────────

class CombineRequest(BaseModel):
    file_ids: list[str]


class RegisterRequest(BaseModel):
    filenames: list[str]


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
    UI works without modification.
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


# ── Per-file preview ─────────────────────────────────────────────────────────────

@batch_router.get("/{job_id}/preview/{file_idx}")
async def batch_file_preview(
    job_id: str,
    file_idx: int,
    td: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Return extracted row data for ONE file in the batch.

    Row data is stored in Redis by the Celery worker immediately after
    extraction and expires after batch_ttl_seconds.
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
            detail="Row data not found — it may have expired or was not stored",
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


# ── Selective combine ─────────────────────────────────────────────────────────────

@batch_router.post("/{job_id}/combine")
async def batch_combine(
    job_id: str,
    body: CombineRequest,
    td: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Combine extracted row data from selected files into ONE Excel file.
    Uses already-extracted rows from Redis — does NOT re-run extraction.
    """
    job = get_job_store().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    _assert_job_access(job.user_id, td)

    if not body.file_ids:
        raise HTTPException(status_code=400, detail="file_ids must not be empty")

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
        log.exception("batch.combine_failed", job_id=job_id, exc_message=str(exc))
        raise HTTPException(status_code=500, detail=f"Combine failed: {exc}")

    return {
        "file_id": combined_file_id,
        "filename": out_filename,
        "total_rows": total_rows,
        "file_count": len(body.file_ids),
    }


# ── Helpers ──────────────────────────────────────────────────────────────────────

async def _stream_batch_upload(
    upload: UploadFile,
    filename: str,
    upload_dir: Path,
    job_id: str,
    idx: int,
    max_mb: int,
    chunk_size: int = 1_048_576,
) -> tuple[Path, int]:
    """
    Stream one UploadFile to upload_dir in chunks.

    File is never fully in RAM — each 1 MB chunk is written to disk and
    released. Raises HTTP 413 if the file exceeds max_mb mid-stream.
    Returns (disk_path, total_bytes).
    """
    max_bytes = max_mb * 1024 * 1024

    # Build a safe filename: strip path separators and non-printable characters.
    safe_name = re.sub(r'[^\w\-_. ]', '_', Path(filename).name)[:80] or "upload"
    disk_filename = f"{job_id}_{idx:03d}_{safe_name}"
    disk_path = upload_dir / disk_filename

    total = 0
    try:
        with disk_path.open("wb") as fh:
            while chunk := await upload.read(chunk_size):
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File '{filename}' exceeds {max_mb} MB limit",
                    )
                fh.write(chunk)
    except Exception:
        safe_delete(disk_path, log_context=f"upload-error job={job_id} idx={idx}")
        raise

    return disk_path, total


def _dispatch_celery(
    job_id: str,
    file_data: list[tuple[Path, str, int]],
    cfg,
    user_id: str = "",
    idx_offset: int = 0,
) -> None:
    """
    Enqueue one Celery task per file.

    Files above large_file_threshold_mb go to the 'heavy' queue — a dedicated
    worker with higher time limits. Smaller files use the 'normal' queue.

    The API returns immediately after this call. Workers pick up tasks
    independently, in separate processes, paced by their concurrency setting.
    """
    from spir_dynamic.tasks.extraction_tasks import process_file_task

    threshold_bytes = cfg.large_file_threshold_mb * 1024 * 1024

    for idx, (disk_path, filename, size_bytes) in enumerate(file_data, start=idx_offset):
        is_heavy = size_bytes > threshold_bytes
        queue = "heavy" if is_heavy else "normal"
        size_mb = size_bytes / (1024 * 1024)

        kwargs: dict = {}
        if is_heavy:
            # Override time limits for heavy files so they don't get killed at 5 min.
            kwargs["soft_time_limit"] = _HEAVY_SOFT_LIMIT
            kwargs["time_limit"] = _HEAVY_HARD_LIMIT

        process_file_task.apply_async(
            args=[job_id, idx, str(disk_path), filename, user_id],
            queue=queue,
            **kwargs,
        )
        log.info(
            "celery.task_enqueued",
            job_id=job_id,
            file_idx=idx,
            filename=filename,
            size_mb=round(size_mb, 1),
            queue=queue,
        )


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
            f"Row data missing or expired for {len(missing)} file(s): {missing[:3]}"
            + ("..." if len(missing) > 3 else "")
        )

    xlsx_bytes = build_xlsx(all_rows, "COMBINED")
    combined_id = str(uuid.uuid4())
    out_filename = "COMBINED_Extraction.xlsx"
    cfg = get_settings()
    storage.put(combined_id, xlsx_bytes, out_filename, ttl=cfg.batch_ttl_seconds)

    log.info(
        "batch.combine_complete",
        file_count=len(file_ids),
        total_rows=len(all_rows),
        file_id=combined_id,
    )
    return combined_id, out_filename, len(all_rows)


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
        log.warning("batch.db_persist_failed", exc_message=str(exc))


async def _process_batch_from_disk(
    job_id: str,
    file_data: list[tuple[Path, str, int]],
    idx_offset: int = 0,
) -> None:
    """
    Fallback coroutine used when celery_enabled=False (dev/test mode).

    Processes files SEQUENTIALLY — one at a time — from their on-disk paths.
    Each file is cleaned up after extraction regardless of success or failure.
    """
    loop = asyncio.get_event_loop()
    store = get_job_store()

    for idx, (disk_path, filename, _) in enumerate(file_data, start=idx_offset):
        try:
            result = await loop.run_in_executor(None, run_pipeline, disk_path, filename)
            store.update_result(job_id, idx, FileResult(
                filename=filename,
                status="ok",
                total_rows=result.get("total_rows", 0),
                total_tags=result.get("total_tags", 0),
                spir_no=result.get("spir_no", ""),
                file_id=result.get("file_id", ""),
            ))
        except Exception as exc:
            log.exception("batch.extraction_failed", filename=filename, exc_message=str(exc))
            store.update_result(job_id, idx, FileResult(
                filename=filename,
                status="error",
                error=str(exc),
            ))
        finally:
            safe_delete(disk_path, log_context=f"fallback job={job_id} idx={idx}")
