"""
FastAPI route handlers.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl.utils.exceptions import InvalidFileException
from pydantic import BaseModel
from sqlalchemy import select, desc

from spir_dynamic.app.auth import get_current_user, TokenData
from spir_dynamic.app.pipeline import run_pipeline, retrieve_result
from spir_dynamic.app.config import get_settings
from spir_dynamic.db.database import get_db, is_db_enabled
from spir_dynamic.db.models import ExtractionHistory
from spir_dynamic.extraction.file_validator import ValidationError
from spir_dynamic.services.currency_service import conversion_summary

log = logging.getLogger(__name__)

router = APIRouter()

# ── Concurrency control ────────────────────────────────────────────────────────
# Lazily created after the asyncio event loop is running (first request).
_extraction_semaphore: asyncio.Semaphore | None = None
_active_extractions: int = 0
_active_lock = asyncio.Lock() if False else None  # populated on first use

# Thread pool sized to max_concurrent_extractions so OS scheduler
# sees at most N extraction threads, not the default unlimited pool.
_extraction_executor: ThreadPoolExecutor | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _extraction_semaphore
    if _extraction_semaphore is None:
        cfg = get_settings()
        _extraction_semaphore = asyncio.Semaphore(cfg.max_concurrent_extractions)
        log.info(
            "Extraction semaphore initialised | max_concurrent=%d",
            cfg.max_concurrent_extractions,
        )
    return _extraction_semaphore


def _get_executor() -> ThreadPoolExecutor:
    global _extraction_executor
    if _extraction_executor is None:
        cfg = get_settings()
        _extraction_executor = ThreadPoolExecutor(
            max_workers=cfg.max_concurrent_extractions,
            thread_name_prefix="spir-extract",
        )
        log.info(
            "Extraction thread pool initialised | max_workers=%d",
            cfg.max_concurrent_extractions,
        )
    return _extraction_executor


# ── Models ─────────────────────────────────────────────────────────────────────

class HistoryItem(BaseModel):
    id: str
    filename: str
    spir_no: Optional[str]
    tag_count: int
    spare_count: int
    created_at: datetime
    file_id: Optional[str] = None
    total_rows: Optional[int] = None
    model_config = {"from_attributes": True}


class CombineRequest(BaseModel):
    history_ids: list[str]


class DeleteRequest(BaseModel):
    history_ids: list[str]


# ── Upload helper ───────────────────────────────────────────────────────────────

async def _stream_to_temp(
    upload: UploadFile,
    max_mb: int,
    chunk_size: int,
) -> tuple[Path, int]:
    """
    Stream UploadFile to a named temp file in chunks.

    Returns (path, total_bytes). Rejects mid-stream with HTTP 413 if the file
    exceeds max_mb. Cleans up the temp file on any error.
    """
    total = 0
    max_bytes = max_mb * 1024 * 1024
    suffix = Path(upload.filename or "upload").suffix.lower() or ".xlsx"
    tmp = tempfile.NamedTemporaryFile(
        delete=False,
        suffix=suffix,
        prefix="spir_upload_",
    )
    tmp_path = Path(tmp.name)
    try:
        with tmp:
            while chunk := await upload.read(chunk_size):
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds {max_mb} MB limit",
                    )
                tmp.write(chunk)
        log.debug("Upload streamed to temp | path=%s bytes=%d", tmp_path, total)
        return tmp_path, total
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


# ── Extract endpoint ────────────────────────────────────────────────────────────

@router.post("/extract")
async def extract(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    td: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """Upload and extract a SPIR Excel file."""
    cfg = get_settings()
    filename = file.filename or "upload.xlsx"
    tmp_path: Path | None = None
    queue_wait: float = 0.0  # populated after semaphore is acquired
    size_mb: float = 0.0     # populated after upload completes
    extract_dur: float = 0.0 # populated after extraction completes

    try:
        # ── Phase A: Stream upload to disk (no full bytes in RAM) ────────────
        tmp_path, file_size = await _stream_to_temp(
            file, cfg.max_file_size_mb, cfg.upload_chunk_size
        )
        size_mb = file_size / (1024 * 1024)
        log.info("Upload received | file=%s size_mb=%.1f", filename, size_mb)

        # ── Phase B: Wait for a concurrency slot ─────────────────────────────
        sem = _get_semaphore()
        executor = _get_executor()

        log.info(
            "Extraction queued | file=%s size_mb=%.1f waiting_for_slot=True",
            filename, size_mb,
        )
        queue_wait_start = time.perf_counter()

        async with sem:
            queue_wait = time.perf_counter() - queue_wait_start
            if queue_wait > 1.0:
                log.info(
                    "Semaphore acquired | file=%s queue_wait=%.1fs",
                    filename, queue_wait,
                )

            log.info("Extraction start | file=%s size_mb=%.1f", filename, size_mb)
            extract_start = time.perf_counter()

            loop = asyncio.get_event_loop()
            try:
                # Pass the PATH — pipeline opens workbook from disk, zero bytes copy.
                result = await asyncio.wait_for(
                    loop.run_in_executor(executor, run_pipeline, tmp_path, filename),
                    timeout=cfg.extraction_timeout_seconds,
                )
            except asyncio.TimeoutError:
                _elapsed = time.perf_counter() - extract_start
                log.error(
                    "Extraction timeout | file=%s size_mb=%.1f timeout_s=%d",
                    filename, size_mb, cfg.extraction_timeout_seconds,
                )
                log.error(
                    "EXTRACTION_EVENT | file=%s size_mb=%.1f extract_dur=%.1fs"
                    " queue_wait=%.1fs status=timeout",
                    filename, size_mb, _elapsed, queue_wait,
                )
                raise HTTPException(
                    status_code=504,
                    detail=(
                        f"Extraction timed out after {cfg.extraction_timeout_seconds}s. "
                        "The file may contain too many sheets, images, or embedded objects."
                    ),
                )
            except ValidationError as exc:
                log.warning("Validation failed | file=%s reason=%s", filename, exc)
                log.warning(
                    "EXTRACTION_EVENT | file=%s size_mb=%.1f queue_wait=%.1fs"
                    " status=validation_error reason=%s",
                    filename, size_mb, queue_wait, exc,
                )
                raise HTTPException(status_code=422, detail=str(exc))
            except MemoryError:
                _elapsed = time.perf_counter() - extract_start
                log.error(
                    "Out-of-memory during extraction | file=%s size_mb=%.1f",
                    filename, size_mb,
                )
                log.error(
                    "EXTRACTION_EVENT | file=%s size_mb=%.1f extract_dur=%.1fs"
                    " queue_wait=%.1fs status=oom",
                    filename, size_mb, _elapsed, queue_wait,
                )
                raise HTTPException(
                    status_code=507,
                    detail=(
                        "Server ran out of memory processing this file. "
                        "This can happen with files containing many images or embedded objects."
                    ),
                )
            except (zipfile.BadZipFile, InvalidFileException) as exc:
                log.warning("Corrupted/unreadable file | file=%s error=%s", filename, exc)
                log.warning(
                    "EXTRACTION_EVENT | file=%s size_mb=%.1f queue_wait=%.1fs"
                    " status=corrupt_file",
                    filename, size_mb, queue_wait,
                )
                raise HTTPException(
                    status_code=422,
                    detail=f"File appears corrupted or unreadable: {exc}",
                )
            except Exception as exc:
                _elapsed = time.perf_counter() - extract_start
                log.error(
                    "Extraction failed | file=%s size_mb=%.1f error=%s",
                    filename, size_mb, exc,
                    exc_info=True,
                )
                log.error(
                    "EXTRACTION_EVENT | file=%s size_mb=%.1f extract_dur=%.1fs"
                    " queue_wait=%.1fs status=error",
                    filename, size_mb, _elapsed, queue_wait,
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"Extraction failed: {exc}",
                )

            extract_dur = time.perf_counter() - extract_start
            if extract_dur > 120:
                log.warning(
                    "Slow extraction | file=%s size_mb=%.1f duration=%.1fs",
                    filename, size_mb, extract_dur,
                )
            log.info(
                "Extraction done | file=%s rows=%d size_mb=%.1f duration=%.1fs",
                filename, result.get("total_rows", 0), size_mb, extract_dur,
            )

        # ── Phase C: Persist rows + audit log (unchanged) ────────────────────
        json_path = _save_rows_to_disk(result, cfg)

        from spir_dynamic.services.audit_service import log_extraction
        ip = _get_ip(request)
        try:
            await log_extraction(
                td.user_id, td.jti, result, ip,
                original_filename=filename, json_path=json_path,
            )
        except Exception as e:
            log.error("History logging failed (extraction still succeeded): %s", e)

        log.info(
            "EXTRACTION_EVENT | file=%s spir_no=%s rows=%d tags=%d"
            " size_mb=%.1f extract_dur=%.1fs queue_wait=%.1fs status=success",
            filename,
            result.get("spir_no", ""),
            result.get("total_rows", 0),
            result.get("total_tags", 0),
            size_mb,
            extract_dur,
            queue_wait,
        )

        return result

    finally:
        # Temp-file cleanup runs unconditionally — after workbook.close() has
        # already completed inside pipeline.py's finally block.
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
                log.debug("Temp file cleaned | path=%s", tmp_path)
            except Exception as e:
                log.warning("Temp cleanup failed | path=%s error=%s", tmp_path, e)


# ── Download endpoint ───────────────────────────────────────────────────────────

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

    from spir_dynamic.services.audit_service import log_download
    ip = _get_ip(request)
    await log_download(td.user_id, None, file_id, ip)

    if isinstance(data, io.BytesIO):
        data = data.read()

    safe_name = filename.replace('"', "'")

    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


# ── Misc endpoints ──────────────────────────────────────────────────────────────

@router.get("/health")
async def health() -> dict:
    import shutil

    cfg = get_settings()
    out: dict = {
        "status": "healthy",
        "version": cfg.app_version,
        "db_mode": "enabled" if is_db_enabled() else "legacy",
    }

    # Verify the extraction storage directory is writable.
    try:
        rows_dir = Path(cfg.rows_storage_path)
        probe = rows_dir / ".health_probe"
        probe.touch()
        probe.unlink(missing_ok=True)
        out["extraction_dir"] = "ok"
    except Exception as exc:
        out["extraction_dir"] = "error"
        out["extraction_dir_error"] = str(exc)
        out["status"] = "degraded"

    # Report free disk space on the filesystem that holds extracted rows.
    try:
        anchor = Path(cfg.rows_storage_path).anchor or "/"
        usage = shutil.disk_usage(anchor)
        out["disk_free_gb"] = round(usage.free / (1024 ** 3), 1)
        if out["disk_free_gb"] < 2.0:
            out["disk_warning"] = "low"
            out["status"] = "degraded"
    except Exception:
        pass  # non-fatal — don't degrade status on a stat failure

    return out


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
        from sqlalchemy import text
        user = await db.get(User, td.user_id)
        result = await db.execute(
            text("SELECT COUNT(*) FROM extraction_history WHERE user_id = :user_id"),
            {"user_id": td.user_id},
        )
        total_files = int(result.scalar() or 0)
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
        "role": td.role,
        "created_at": None,
        "total_files_extracted": 0,
    }


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _save_rows_to_disk(result: dict, cfg) -> Optional[str]:
    """
    Write extracted preview_rows to a JSON file on disk.

    Returns the file path on success, None on any failure.
    Failure is non-fatal — extraction and download continue regardless.
    The file is needed only for the /combine endpoint.
    """
    try:
        rows_dir = Path(cfg.rows_storage_path)
        rows_dir.mkdir(parents=True, exist_ok=True)
        file_id = result.get("file_id", "")
        if not file_id:
            return None
        payload = json.dumps({
            "file_id": file_id,
            "filename": result.get("filename", ""),
            "spir_no": result.get("spir_no", ""),
            "cols": result.get("preview_cols", []),
            "rows": result.get("preview_rows", []),
        }, ensure_ascii=False)
        path = rows_dir / f"{file_id}.json"
        path.write_text(payload, encoding="utf-8")
        log.debug("Rows persisted to disk: %s (%d rows)", path, len(result.get("preview_rows", [])))
        return str(path)
    except Exception as exc:
        log.warning("Row JSON save failed (non-fatal): %s", exc)
        return None


def _build_combined_excel(rows: list[list]) -> bytes:
    """
    Runs in a thread pool. Deduplicates combined rows and builds one Excel file.
    Reuses the existing deduplication + build_xlsx pipeline unchanged.
    """
    from spir_dynamic.services.duplicate_checker import deduplicate_rows
    from spir_dynamic.services.excel_builder import build_xlsx
    from spir_dynamic.extraction.output_schema import CI

    deduped = deduplicate_rows(rows, CI)
    return build_xlsx(deduped, "COMBINED")


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


# ── History endpoints ────────────────────────────────────────────────────────────

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
                file_id=getattr(r, "file_id", None),
                total_rows=int(getattr(r, "total_rows", 0) or 0),
            )
        )
    return items


# ── Delete history endpoint ──────────────────────────────────────────────────────

@router.delete("/history")
async def delete_history(
    body: DeleteRequest,
    td: TokenData = Depends(get_current_user),
    db=Depends(get_db),
) -> dict[str, int]:
    """Delete history records and their associated JSON row files."""
    if not is_db_enabled() or db is None:
        raise HTTPException(status_code=503, detail="Database required for history deletion")
    if not td.user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not body.history_ids:
        return {"deleted": 0}

    ownership_filter = (
        (ExtractionHistory.user_id == td.user_id)
        if td.role != "admin"
        else True
    )
    q = select(ExtractionHistory).where(
        ExtractionHistory.id.in_(body.history_ids)
    ).where(ownership_filter)
    db_result = await db.execute(q)
    records = db_result.scalars().all()

    cfg = get_settings()
    storage_root = Path(cfg.rows_storage_path).resolve()

    for rec in records:
        if rec.json_path:
            p = Path(rec.json_path).resolve()
            try:
                p.relative_to(storage_root)
            except ValueError:
                log.warning("Refusing to delete file outside storage root: %s", p)
            else:
                try:
                    if p.exists():
                        p.unlink()
                        log.info("Deleted JSON file: %s", p)
                    else:
                        log.warning("JSON file missing during delete: %s", p)
                except Exception as exc:
                    log.warning("Failed to delete JSON file %s: %s", p, exc)

        if rec.file_id:
            try:
                from spir_dynamic.services.redis_store import RedisStorage
                rs = RedisStorage(cfg.redis_url)
                rs.delete(rec.file_id)
            except Exception as exc:
                log.warning("Redis cleanup failed for file_id %s: %s", rec.file_id, exc)

        await db.delete(rec)
        log.info("Deleted history record: %s", rec.id)

    await db.commit()
    return {"deleted": len(records)}


# ── Combine endpoint ─────────────────────────────────────────────────────────────

@router.post("/combine")
async def combine(
    body: CombineRequest,
    td: TokenData = Depends(get_current_user),
    db=Depends(get_db),
) -> StreamingResponse:
    """
    Load already-extracted rows from disk for selected history records,
    merge them, run deduplication, and return ONE combined Excel file.

    Does NOT re-run extraction — only reads persisted row JSON files.
    """
    if not is_db_enabled() or db is None:
        raise HTTPException(status_code=503, detail="Database required for combine")
    if not body.history_ids:
        raise HTTPException(status_code=400, detail="history_ids must not be empty")
    if not td.user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    ownership_filter = (
        (ExtractionHistory.user_id == td.user_id)
        if td.role != "admin"
        else True
    )
    q = (
        select(
            ExtractionHistory.id,
            ExtractionHistory.file_id,
            ExtractionHistory.json_path,
            ExtractionHistory.filename,
        )
        .where(ExtractionHistory.id.in_(body.history_ids))
        .where(ownership_filter)
    )
    db_result = await db.execute(q)
    records = db_result.all()

    if len(records) != len(body.history_ids):
        found_ids = {r.id for r in records}
        missing = [hid for hid in body.history_ids if hid not in found_ids]
        raise HTTPException(
            status_code=404,
            detail=f"History records not found or not accessible: {missing}",
        )

    combined_rows: list[list] = []
    no_json: list[str] = []
    for rec in records:
        if not rec.json_path:
            no_json.append(rec.filename or rec.id)
            continue
        p = Path(rec.json_path)
        if not p.exists():
            no_json.append(rec.filename or rec.id)
            continue
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            combined_rows.extend(payload.get("rows", []))
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to read row data for '{rec.filename}': {exc}",
            )

    if no_json:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Row data not found for {len(no_json)} file(s): {no_json[:3]}"
                + (f" …and {len(no_json) - 3} more" if len(no_json) > 3 else "")
                + ". These files were extracted before persistent row storage was added —"
                " re-extract them to enable combine."
            ),
        )

    if not combined_rows:
        raise HTTPException(status_code=400, detail="No rows found in selected files")

    loop = asyncio.get_event_loop()
    xlsx_bytes = await loop.run_in_executor(None, _build_combined_excel, combined_rows)

    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="COMBINED_Extraction.xlsx"'},
    )
