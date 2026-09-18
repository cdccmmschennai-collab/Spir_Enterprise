"""
Direct browser-to-MinIO upload control API — Phase 3D.

Four small, authenticated control requests replace the one big multipart POST
for uploads the API would hand to a Celery worker anyway (heavy / giant
queue, i.e. > large_file_threshold_mb). The workbook bytes themselves go from
the browser straight to MinIO through presigned part URLs and never enter a
FastAPI request body.

    POST   /api/uploads/initiate                  declare (filename, size) [+ batch job/slot]
                                                  -> {"mode": "api"}      keep using /api/extract or
                                                                          /api/batch/{job}/upload
                                                  -> {"mode": "direct", job_id, file_idx, parts:[{n,url}], ...}
    POST   /api/uploads/{job}/{idx}/parts         fresh URLs for parts whose URL expired
    POST   /api/uploads/{job}/{idx}/complete      server verifies the object, then queues the worker
    DELETE /api/uploads/{job}/{idx}               abort: discard parts/object, mark the slot failed

"mode": "api" is the answer whenever the direct path does not apply (file
routes to 'normal', Celery off, MinIO not the source backend, no public
endpoint) — the client then falls back to the existing endpoints unchanged.

Every operation checks the caller owns the job (same rule as the batch API).
The server derives the object key from (job_id, file_idx, registered
filename) exactly as Phase 3C does; the client never names a key. The
"complete" step is the only place a worker task is created, and only after
the object has been assembled and its size verified — see
services/direct_upload.py.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from spir_dynamic.app.auth import TokenData, get_current_user
from spir_dynamic.app.batch_router import (
    _assert_job_access,
    _dispatch_celery,
    _persist_job_to_db,
    max_upload_bytes,
    route_queue,
)
from spir_dynamic.app.config import get_settings
from spir_dynamic.services.direct_upload import (
    STATE_COMPLETING,
    STATE_UPLOADING,
    UploadConflict,
    UploadIncomplete,
    UploadSession,
    UploadVerificationFailed,
    abort_upload,
    direct_upload_storage,
    finalize_upload,
    plan_upload,
    presign_parts,
)
from spir_dynamic.services.job_store import FileResult, get_job_store
from spir_dynamic.services.object_storage import StorageError
from spir_dynamic.services.source_objects import discard_source_object

log = structlog.stdlib.get_logger(__name__)

upload_router = APIRouter()


# ── Request models ────────────────────────────────────────────────────────────

class InitiateRequest(BaseModel):
    filename: str = Field(default="upload.xlsx", max_length=512)
    size: int = Field(gt=0)
    # Present for a batch slot registered via /api/batch/register; absent for
    # the single-file extraction page (a one-file job is created here).
    job_id: str | None = None
    file_idx: int | None = None


class PartsRequest(BaseModel):
    part_numbers: list[int] = Field(min_length=1, max_length=10_000)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mb(size: int) -> float:
    return round(size / (1024 * 1024), 1)


def _load_session(job_id: str, file_idx: int, td: TokenData) -> tuple[Any, UploadSession | None]:
    """The job (404 / 403 checked) and its direct-upload record for the slot, if any."""
    job = get_job_store().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    _assert_job_access(job.user_id, td)
    if file_idx < 0 or file_idx >= job.total:
        raise HTTPException(status_code=400, detail=f"file_idx must be 0–{job.total - 1}")
    raw = get_job_store().get_upload(job_id, file_idx)
    return job, (UploadSession.from_dict(raw) if raw else None)


def _storage_or_503():
    st = direct_upload_storage()
    if st is None:
        raise HTTPException(status_code=503, detail="Direct upload is not available on this server")
    return st


# ── Endpoints ─────────────────────────────────────────────────────────────────

@upload_router.post("/initiate")
async def initiate_upload(
    body: InitiateRequest,
    td: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Decide how this file should be uploaded. Small files (and any deployment
    without direct upload) get {"mode": "api"}; large files get a multipart
    plan with one presigned PUT URL per part.
    """
    cfg = get_settings()
    t0 = time.perf_counter()
    user_id = td.user_id or ""
    store = get_job_store()

    if body.size > max_upload_bytes(cfg):
        raise HTTPException(status_code=413, detail=f"File exceeds {cfg.max_file_size_mb} MB limit")
    if (body.job_id is None) != (body.file_idx is None):
        raise HTTPException(status_code=400, detail="job_id and file_idx must be given together")

    # A batch slot is validated (exists, owned by the caller, still pending)
    # before anything else is decided.
    job = previous = None
    if body.job_id is not None:
        job, previous = _load_session(body.job_id, body.file_idx, td)
        slot = job.results[int(body.file_idx)]
        if slot.status not in ("pending", "uploading"):
            raise HTTPException(status_code=409, detail=f"File slot already {slot.status}")

    queue, _ = route_queue(body.size, cfg)
    storage = direct_upload_storage() if (cfg.direct_upload_configured and cfg.celery_enabled) else None
    if queue == "normal" or storage is None:
        reason = "small_file" if queue == "normal" else "direct_upload_unavailable"
        log.info("upload.mode", mode="api", reason=reason, queue=queue, size_mb=_mb(body.size), user_id=user_id)
        return {"mode": "api", "reason": reason, "queue": queue}

    loop = asyncio.get_event_loop()

    # ── Which slot? ─────────────────────────────────────────────────────────
    if job is not None:
        job_id, file_idx = body.job_id, int(body.file_idx)
        filename = job.results[file_idx].filename    # registered name — never the client's
        if previous is not None:
            if previous.state == STATE_COMPLETING:
                raise HTTPException(status_code=409, detail="Upload is being completed")
            # Re-initiate (e.g. page reloaded): drop the old pieces first.
            await loop.run_in_executor(
                None, lambda: abort_upload(storage, previous, context=f"re-initiate job={job_id} idx={file_idx}"),
            )
            store.clear_upload(job_id, file_idx)
        new_job = False
    else:
        job_id, file_idx = str(uuid.uuid4()), 0
        filename = body.filename or "upload.xlsx"
        new_job = True

    structlog.contextvars.bind_contextvars(job_id=job_id, file_idx=file_idx, filename=filename, user_id=user_id)

    # ── Multipart upload + presigned URLs (blocking S3 calls off the loop) ──
    try:
        session, parts = await loop.run_in_executor(
            None,
            lambda: plan_upload(
                storage, job_id=job_id, file_idx=file_idx, filename=filename, size=body.size,
                user_id=user_id, queue=queue, part_size=cfg.direct_upload_part_size_mb * 1024 * 1024,
                url_ttl=cfg.direct_upload_url_ttl_seconds,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except StorageError as exc:
        log.error("upload.storage_failed", stage="initiate", exc_message=str(exc))
        raise HTTPException(status_code=503, detail=f"Could not start upload: {exc}")

    # ── Record it: job slot -> "uploading" ──────────────────────────────────
    try:
        if new_job:
            store.create(job_id, [filename], user_id=user_id)
        store.set_upload(job_id, file_idx, session.to_dict())
        store.update_result(job_id, file_idx, FileResult(filename=filename, status="uploading"))
    except Exception as exc:
        await loop.run_in_executor(
            None, lambda: abort_upload(storage, session, context=f"record-failed job={job_id}"),
        )
        log.exception("upload.record_failed", exc_type=type(exc).__name__, exc_message=str(exc))
        raise HTTPException(status_code=503, detail=f"Could not record upload: {exc}")
    if new_job:
        asyncio.create_task(_persist_job_to_db(job_id, user_id, [filename], cfg.batch_ttl_seconds))

    log.info(
        "upload.initiated",
        mode="direct", source_key=session.source_key, size_mb=_mb(body.size), queue=queue,
        part_size_mb=cfg.direct_upload_part_size_mb, part_count=session.part_count,
        url_ttl_s=cfg.direct_upload_url_ttl_seconds, duration_s=round(time.perf_counter() - t0, 3),
    )
    return {
        "mode": "direct",
        "job_id": job_id,
        "file_idx": file_idx,
        "filename": filename,
        "size": body.size,
        "part_size": session.part_size,
        "part_count": session.part_count,
        "parts": parts,
        "url_expires_in": cfg.direct_upload_url_ttl_seconds,
        "queue": queue,
    }


@upload_router.post("/{job_id}/{file_idx}/parts")
async def presign_more_parts(
    job_id: str,
    file_idx: int,
    body: PartsRequest,
    td: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """Fresh presigned URLs for the given part numbers (expired URL, long upload)."""
    cfg = get_settings()
    _job, session = _load_session(job_id, file_idx, td)
    if session is None:
        raise HTTPException(status_code=404, detail="No upload in progress for this file")
    if session.state != STATE_UPLOADING:
        raise HTTPException(status_code=409, detail="Upload is being completed")

    numbers = sorted(set(body.part_numbers))
    bad = [n for n in numbers if n < 1 or n > session.part_count]
    if bad:
        raise HTTPException(status_code=400, detail=f"part numbers out of range 1–{session.part_count}: {bad[:5]}")

    storage = _storage_or_503()
    loop = asyncio.get_event_loop()
    try:
        parts = await loop.run_in_executor(
            None, presign_parts, storage, session.source_key, session.upload_id, numbers,
            cfg.direct_upload_url_ttl_seconds,
        )
    except StorageError as exc:
        log.error("upload.storage_failed", stage="presign", job_id=job_id, file_idx=file_idx, exc_message=str(exc))
        raise HTTPException(status_code=503, detail=f"Could not sign upload URLs: {exc}")
    log.info("upload.parts_presigned", job_id=job_id, file_idx=file_idx, user_id=td.user_id or "",
             source_key=session.source_key, count=len(parts))
    return {"parts": parts, "url_expires_in": cfg.direct_upload_url_ttl_seconds}


@upload_router.post("/{job_id}/{file_idx}/complete")
async def complete_upload(
    job_id: str,
    file_idx: int,
    td: TokenData = Depends(get_current_user),
) -> JSONResponse:
    """
    The browser says every part is in. The server checks that against MinIO,
    assembles the object, verifies its size, and only then enqueues the
    Celery task — with the same source key / queue routing as Phase 3C.
    Mirrors the 202 payload of POST /api/extract so the client polls the
    same way.
    """
    cfg = get_settings()
    t0 = time.perf_counter()
    store = get_job_store()
    job, session = _load_session(job_id, file_idx, td)
    slot = job.results[file_idx]
    if session is None:
        # Duplicate completion after success (the slot is queued / running /
        # done), or nothing was ever started for this slot.
        if slot.status in ("pending", "running", "ok"):
            raise HTTPException(status_code=409,
                                detail=f"Upload already completed — file is {slot.status}")
        raise HTTPException(status_code=404, detail="No upload in progress for this file")
    user_id = session.user_id or (td.user_id or "")
    structlog.contextvars.bind_contextvars(job_id=job_id, file_idx=file_idx, filename=session.filename,
                                          user_id=user_id, source_key=session.source_key)

    # One completion at a time per slot (atomic in Redis).
    if not store.transition_upload(job_id, file_idx, STATE_UPLOADING, STATE_COMPLETING):
        raise HTTPException(status_code=409, detail="Upload is already being completed")

    storage = _storage_or_503()
    loop = asyncio.get_event_loop()
    try:
        info = await loop.run_in_executor(None, finalize_upload, storage, session)
    except UploadIncomplete as exc:
        store.transition_upload(job_id, file_idx, STATE_COMPLETING, STATE_UPLOADING)   # client may retry parts
        return JSONResponse(status_code=exc.status, content={"detail": str(exc), **exc.extra})
    except UploadVerificationFailed as exc:
        # Object discarded by finalize_upload; the slot fails — nothing reaches a worker.
        store.clear_upload(job_id, file_idx)
        store.update_result(job_id, file_idx, FileResult(filename=session.filename, status="error",
                                                         error=f"Upload verification failed: {exc}"))
        log.error("upload.verify_failed", **exc.extra)
        raise HTTPException(status_code=exc.status, detail=f"Upload verification failed: {exc}")
    except UploadConflict as exc:
        store.clear_upload(job_id, file_idx)
        store.update_result(job_id, file_idx, FileResult(filename=session.filename, status="error", error=str(exc)))
        log.warning("upload.conflict", exc_message=str(exc))
        raise HTTPException(status_code=exc.status, detail=str(exc))
    except StorageError as exc:
        store.transition_upload(job_id, file_idx, STATE_COMPLETING, STATE_UPLOADING)   # try again later
        log.error("upload.storage_failed", stage="complete", exc_message=str(exc))
        raise HTTPException(status_code=503, detail=f"Could not complete upload: {exc}")

    verify_s = round(time.perf_counter() - t0, 3)
    log.info("upload.verified", size_mb=_mb(info.size), part_count=session.part_count, duration_s=verify_s)

    # ── Hand-off: the slot is a normal queued file from here on ─────────────
    store.clear_upload(job_id, file_idx)
    store.update_result(job_id, file_idx, FileResult(filename=session.filename, status="pending"))
    queue, _ = route_queue(info.size, cfg)
    try:
        queues = _dispatch_celery(job_id, [(session.source_key, session.filename, info.size)], cfg, user_id,
                                  idx_offset=file_idx)
        queue = queues[0]
    except Exception as exc:
        # Verified but never queued: drop the object and fail the slot so the
        # poller never stalls on "pending" and nothing is left behind.
        discard_source_object(session.source_key, log_context=f"enqueue-failed job={job_id} idx={file_idx}")
        store.update_result(job_id, file_idx, FileResult(filename=session.filename, status="error",
                                                         error=f"Could not queue extraction: {exc}"))
        log.exception("upload.enqueue_failed", exc_type=type(exc).__name__, exc_message=str(exc))
        raise HTTPException(status_code=503, detail=f"Could not queue extraction: {exc}")

    log.info("upload.completed", mode="direct", size_mb=_mb(info.size), queue=queue,
             duration_s=round(time.perf_counter() - t0, 3))
    return JSONResponse(status_code=202, content={
        "status": "queued",
        "job_id": job_id,
        "file_idx": file_idx,
        "filename": session.filename,
        "size_mb": _mb(info.size),
        "queue": queue,
    })


@upload_router.delete("/{job_id}/{file_idx}")
async def abort_direct_upload(
    job_id: str,
    file_idx: int,
    td: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """Cancel: discard uploaded parts (and any object), fail the slot."""
    store = get_job_store()
    _job, session = _load_session(job_id, file_idx, td)
    if session is None:
        raise HTTPException(status_code=404, detail="No upload in progress for this file")
    if session.state == STATE_COMPLETING:
        raise HTTPException(status_code=409, detail="Upload is being completed")

    storage = _storage_or_503()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, lambda: abort_upload(storage, session, context=f"client-abort job={job_id} idx={file_idx}"),
    )
    store.clear_upload(job_id, file_idx)
    store.update_result(job_id, file_idx, FileResult(filename=session.filename, status="error",
                                                     error="Upload cancelled"))
    log.info("upload.aborted", job_id=job_id, file_idx=file_idx, user_id=td.user_id or "",
             source_key=session.source_key, size_mb=_mb(session.size))
    return {"status": "aborted", "job_id": job_id, "file_idx": file_idx}
