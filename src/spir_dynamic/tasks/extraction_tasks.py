"""
Celery task: process one file within a batch job.

Each uploaded file gets its own task so workers process files for
different users (and different files of the same user) without shared state.

State flow:  pending → running → ok | error

Input files are saved to disk by the API process (storage/batch_uploads/)
before enqueue. The worker reads directly from disk — no bytes copy in RAM —
and deletes the upload file after extraction completes (or exhausts retries).
Orphaned upload files (from crashed workers) are swept on startup.
"""
from __future__ import annotations

import time
from pathlib import Path

import structlog
from celery.exceptions import MaxRetriesExceededError, SoftTimeLimitExceeded

from spir_dynamic.celery_app import celery_app
from spir_dynamic.tasks.base import BaseTask
from spir_dynamic.monitoring.metrics import (
    SANITIZER_RUNS,
    SANITIZER_SAVINGS_MB,
    SANITIZER_REDUCTION_PCT,
    SANITIZER_DURATION,
)

log = structlog.stdlib.get_logger(__name__)


@celery_app.task(
    base=BaseTask,
    name="spir_dynamic.tasks.process_file",
    bind=True,
    max_retries=3,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_file_task(
    self,
    job_id: str,
    file_idx: int,
    upload_path: str,
    filename: str,
    user_id: str = "",
) -> dict:
    """
    Extract a single SPIR file as part of a batch job.

    Args:
        job_id:      Batch job identifier — shared across all files in the batch.
        file_idx:    Position of this file in the batch (0-based).
        upload_path: Absolute path to the file on disk (streamed there by the API).
        filename:    Original upload filename (used for pipeline and error reporting).

    Returns:
        Dict with status + result metadata (mirrors run_pipeline output keys).
    """
    from spir_dynamic.app.pipeline import run_pipeline
    from spir_dynamic.services.cleanup import safe_delete
    from spir_dynamic.services.job_store import FileResult, get_job_store

    # Bind task context to structlog so every log line in this task carries
    # job_id, file_idx, filename, and task_id without manual repetition.
    # clear_contextvars() prevents context leak from a previously reused worker process.
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        job_id=job_id,
        file_idx=file_idx,
        filename=filename,
        task_id=self.request.id,
        user_id=user_id or "",
    )

    _upload_path = Path(upload_path)
    _log_ctx = f"batch job={job_id} idx={file_idx}"
    _t0 = time.perf_counter()

    store = get_job_store()

    # Mark this slot as running so the status endpoint shows progress
    store.update_result(job_id, file_idx, FileResult(filename=filename, status="running"))

    try:
        if not _upload_path.exists():
            raise RuntimeError(
                f"Upload file not found on disk: {upload_path} — "
                "it may have been removed by a prior attempt or a system restart"
            )

        # ── Sanitize: strip embedded bulk assets before openpyxl opens file ──
        # Runs only for XLSX/XLSM files above SANITIZER_THRESHOLD_MB.
        # On any failure the sanitizer returns a fallback result and extraction
        # continues from the original upload — never raises.
        from spir_dynamic.extraction.sanitizer import sanitize_workbook
        _san = sanitize_workbook(_upload_path, filename)

        # Record sanitizer outcome metrics.
        if _san.skip_reason and not _san.used_fallback:
            SANITIZER_RUNS.labels(outcome="skipped").inc()
        elif _san.used_fallback:
            SANITIZER_RUNS.labels(outcome="fallback").inc()
        else:
            SANITIZER_RUNS.labels(outcome="success").inc()
            savings_mb = _san.original_size_mb - _san.sanitized_size_mb
            if savings_mb > 0:
                SANITIZER_SAVINGS_MB.observe(savings_mb)
            if _san.reduction_pct > 0:
                SANITIZER_REDUCTION_PCT.observe(_san.reduction_pct)
        if _san.duration_s > 0:
            SANITIZER_DURATION.observe(_san.duration_s)

        # Use the sanitized copy when available, otherwise the original.
        # The sanitized copy is on the same filesystem partition as the upload.
        _effective_path = _san.sanitized_path if _san.sanitized_path is not None else _upload_path

        # run_pipeline opens the workbook directly from the Path — no bytes copy.
        # The inner try/finally guarantees the sanitized temp file is deleted
        # after extraction regardless of success or failure, while the outer
        # except block preserves the original upload for retry attempts.
        try:
            result = run_pipeline(_effective_path, filename)
        finally:
            # Always clean up the sanitized copy; original upload is untouched.
            if _san.sanitized_path is not None:
                safe_delete(_san.sanitized_path, log_context=f"san-cleanup {_log_ctx}")

        # Extraction done — the upload file is no longer needed.
        safe_delete(_upload_path, log_context=_log_ctx)
        log.debug("upload.deleted", path=str(_upload_path))

        store.update_result(job_id, file_idx, FileResult(
            filename=filename,
            status="ok",
            total_rows=result.get("total_rows", 0),
            total_tags=result.get("total_tags", 0),
            spir_no=result.get("spir_no", ""),
            file_id=result.get("file_id", ""),
        ))

        # ── Persist row payload to Redis for real-time preview / combine ─────────
        # Key: rows:{file_id}  — namespaced to avoid collision with xlsx storage.
        try:
            import json as _json
            from spir_dynamic.app.config import get_settings as _gs
            from spir_dynamic.services.storage import get_storage as _get_storage
            _row_payload = _json.dumps({
                "cols": result.get("preview_cols", []),
                "rows": result.get("preview_rows", []),
                "spir_no": result.get("spir_no", ""),
                "file_id": result.get("file_id", ""),
                "filename": result.get("filename", ""),
                "format": result.get("format", ""),
                "equipment": result.get("equipment", ""),
                "manufacturer": result.get("manufacturer", ""),
                "supplier": result.get("supplier", ""),
                "spir_type": result.get("spir_type"),
                "eqpt_qty": result.get("eqpt_qty", 0),
                "spare_items": result.get("spare_items", 0),
                "total_tags": result.get("total_tags", 0),
                "annexure_count": result.get("annexure_count", 0),
                "total_rows": result.get("total_rows", 0),
                "dup1_count": result.get("dup1_count", 0),
                "sap_count": result.get("sap_count", 0),
            }).encode("utf-8")
            _storage = _get_storage()
            _storage.put(
                f"rows:{result['file_id']}",
                _row_payload,
                "rows.json",
                ttl=_gs().batch_ttl_seconds,
            )
        except Exception as _row_exc:
            log.warning("redis.row_store_failed", exc_message=str(_row_exc))

        # ── Persist rows to disk for history-based combine ────────────────────────
        json_path: str | None = None
        try:
            import json as _json_disk
            from spir_dynamic.app.config import get_settings as _gs_disk
            _cfg = _gs_disk()
            _rows_dir = Path(_cfg.rows_storage_path)
            _rows_dir.mkdir(parents=True, exist_ok=True)
            _file_id = result.get("file_id", "")
            if _file_id:
                _disk_payload = _json_disk.dumps({
                    "file_id": _file_id,
                    "filename": result.get("filename", ""),
                    "spir_no": result.get("spir_no", ""),
                    "cols": result.get("preview_cols", []),
                    "rows": result.get("preview_rows", []),
                }, ensure_ascii=False)
                _disk_path = _rows_dir / f"{_file_id}.json"
                _disk_path.write_text(_disk_payload, encoding="utf-8")
                json_path = str(_disk_path)
                log.debug("disk.rows_written", path=str(_disk_path), rows=result.get("total_rows", 0))
        except Exception as _disk_exc:
            log.warning("disk.row_store_failed", exc_message=str(_disk_exc))

        # ── Write extraction_history so frontend history is populated ─────────────
        try:
            from spir_dynamic.services.audit_service import log_extraction_worker as _log_worker
            _log_worker(
                user_id=user_id,
                result=result,
                original_filename=filename,
                json_path=json_path,
            )
        except Exception as _hist_exc:
            log.warning("history.log_failed", exc_message=str(_hist_exc))

        log.info(
            "extraction.complete",
            status="ok",
            rows=result.get("total_rows", 0),
            tags=result.get("total_tags", 0),
            spir_no=result.get("spir_no", ""),
            duration_s=round(time.perf_counter() - _t0, 2),
        )
        return {
            "status": "ok",
            "job_id": job_id,
            "file_idx": file_idx,
            "file_id": result.get("file_id", ""),
            "total_rows": result.get("total_rows", 0),
        }

    except SoftTimeLimitExceeded:
        # Soft time limit fired — not retryable; clean up the upload file.
        safe_delete(_upload_path, log_context=f"timeout {_log_ctx}")
        log.error(
            "extraction.timeout",
            status="timeout",
            duration_s=round(time.perf_counter() - _t0, 2),
        )
        store.update_result(job_id, file_idx, FileResult(
            filename=filename,
            status="error",
            error="Extraction timed out",
        ))
        return {
            "status": "error",
            "job_id": job_id,
            "file_idx": file_idx,
            "error": "Extraction timed out",
        }

    except Exception as exc:
        # Exponential backoff: 10s → 30s → 90s
        backoff = 10 * (3 ** self.request.retries)
        log.warning(
            "extraction.attempt_failed",
            attempt=self.request.retries + 1,
            max_attempts=self.max_retries + 1,
            exc_type=type(exc).__name__,
            exc_message=str(exc),
            countdown_s=backoff,
        )
        try:
            # Upload file stays on disk — next retry needs it.
            raise self.retry(exc=exc, countdown=backoff)
        except MaxRetriesExceededError:
            # Final failure — no more retries; clean up the upload file.
            safe_delete(_upload_path, log_context=f"max-retries {_log_ctx}")
            log.exception(
                "extraction.failed",
                status="error",
                exc_type=type(exc).__name__,
                exc_message=str(exc),
                duration_s=round(time.perf_counter() - _t0, 2),
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            store.update_result(job_id, file_idx, FileResult(
                filename=filename,
                status="error",
                error=str(exc),
            ))
            return {"status": "error", "job_id": job_id, "file_idx": file_idx, "error": str(exc)}
