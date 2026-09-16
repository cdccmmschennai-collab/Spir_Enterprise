"""
Celery task: process one file within a batch job.

Each uploaded file gets its own task so workers process files for
different users (and different files of the same user) without shared state.

State flow:  pending → running → ok | error

The API stores each upload as a *source object* in the BATCH_UPLOADS storage
area before enqueue and passes the object key (see services/source_objects.py).
The worker materialises it as a local file — in place on the filesystem
backend, via a temp download in the scratch dir on MinIO — runs the existing
sanitizer + pipeline on that path, and deletes the source object after
extraction completes (or once a failure is final). The temp download is
removed after every attempt; the source object survives retries. Stale
scratch files left by a crashed worker process are swept when a process starts.
"""
from __future__ import annotations

import time
from pathlib import Path

import structlog
from celery.exceptions import MaxRetriesExceededError, SoftTimeLimitExceeded
from celery.signals import worker_process_init

from spir_dynamic.celery_app import celery_app
from spir_dynamic.tasks.base import BaseTask
from spir_dynamic.monitoring.metrics import (
    SANITIZER_RUNS,
    SANITIZER_SAVINGS_MB,
    SANITIZER_REDUCTION_PCT,
    SANITIZER_DURATION,
)

log = structlog.stdlib.get_logger(__name__)


@worker_process_init.connect
def _sweep_worker_scratch(**_kwargs) -> None:
    """A fresh worker process removes stale scratch files a crashed predecessor left behind."""
    from spir_dynamic.app.config import get_settings
    from spir_dynamic.services.source_objects import scratch_dir, sweep_stale_scratch
    try:
        removed = sweep_stale_scratch(scratch_dir(get_settings().worker_scratch_dir))
        if removed:
            log.info("worker.scratch_sweep", removed=removed)
    except Exception as exc:   # never block worker start-up on housekeeping
        log.warning("worker.scratch_sweep_failed", exc_message=str(exc))


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
    source_key: str,
    filename: str,
    user_id: str = "",
) -> dict:
    """
    Extract a single SPIR file as part of a batch job.

    Args:
        job_id:      Batch job identifier — shared across all files in the batch.
        file_idx:    Position of this file in the batch (0-based).
        source_key:  Key of the source object in the BATCH_UPLOADS storage area
                     (stored there by the API). Never a host filesystem path.
        filename:    Original upload filename (used for pipeline and error reporting).

    Returns:
        Dict with status + result metadata (mirrors run_pipeline output keys).
    """
    from spir_dynamic.app.pipeline import run_pipeline
    from spir_dynamic.app.config import get_settings as _get_settings
    from spir_dynamic.services.cleanup import safe_delete
    from spir_dynamic.services.job_store import FileResult, get_job_store
    from spir_dynamic.services.object_storage import ObjectNotFound
    from spir_dynamic.services.source_objects import (
        discard_source_object,
        scratch_dir,
        staged_source,
    )

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
        source_key=source_key,
    )

    _log_ctx = f"batch job={job_id} idx={file_idx}"
    _t0 = time.perf_counter()
    _cfg = _get_settings()

    store = get_job_store()

    # ── Broker delivery cap — guard against infinite OOM re-delivery loop ────
    # self.retry() increments self.request.retries (capped at max_retries=3).
    # reject_on_worker_lost re-delivers at broker level and does NOT increment
    # self.request.retries, so a worker OOM-killed by openpyxl memory usage
    # can loop forever.  A Redis counter keyed to the source object key caps the
    # total number of times this file is ever attempted across all delivery paths.
    _DELIVERY_CAP = 5  # 3 explicit retries + 2 broker-level OOM re-deliveries
    try:
        import redis as _redis
        _r = _redis.from_url(_cfg.redis_url, socket_timeout=2, socket_connect_timeout=2)
        _dlv_key = f"spir:dlv:{source_key}"
        _dlv_count = int(_r.incr(_dlv_key) or 1)
        _r.expire(_dlv_key, 86400)  # auto-expire after 24h regardless of outcome
    except Exception as _dlv_exc:
        # Redis unavailable — skip the cap rather than failing all extractions.
        log.warning("delivery_cap.redis_unavailable", exc_message=str(_dlv_exc))
        _dlv_count = 1

    if _dlv_count > _DELIVERY_CAP:
        from spir_dynamic.monitoring.metrics import DELIVERY_CAP_HITS
        DELIVERY_CAP_HITS.inc()
        _error_msg = (
            f"File could not be processed after {_dlv_count} attempts "
            f"(delivery cap={_DELIVERY_CAP}). It may be too large for available "
            "server memory even after sanitization. Try splitting the file or "
            "contact your administrator."
        )
        log.error(
            "extraction.delivery_cap",
            source_key=source_key,
            deliveries=_dlv_count,
            cap=_DELIVERY_CAP,
        )
        discard_source_object(source_key, log_context=f"delivery-cap {_log_ctx}")
        store.update_result(
            job_id, file_idx,
            FileResult(filename=filename, status="error", error=_error_msg),
        )
        return {
            "status": "error",
            "job_id": job_id,
            "file_idx": file_idx,
            "error": "delivery_cap_exceeded",
        }

    structlog.contextvars.bind_contextvars(delivery=_dlv_count)

    # Mark this slot as running so the status endpoint shows progress
    store.update_result(job_id, file_idx, FileResult(filename=filename, status="running"))

    def _extract_from_local(_upload_path: Path) -> dict:
        """The pre-3C worker body: sanitize + run_pipeline on a local file."""
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
        # except block preserves the source object for retry attempts.
        try:
            return run_pipeline(_effective_path, filename)
        finally:
            # Always clean up the sanitized copy; original upload is untouched.
            if _san.sanitized_path is not None:
                safe_delete(_san.sanitized_path, log_context=f"san-cleanup {_log_ctx}")

    try:
        # staged_source yields the object's own file on the filesystem backend
        # or a temp download in the scratch dir otherwise; the temp file is
        # removed when the block exits, whatever happens inside it.
        try:
            with staged_source(
                source_key,
                scratch=scratch_dir(_cfg.worker_scratch_dir),
                log_context=_log_ctx,
            ) as _local_path:
                result = _extract_from_local(_local_path)
        except ObjectNotFound:
            # Nothing to retry against: the object was removed by a prior
            # attempt, a cleanup run, or never stored. Fail the slot now.
            _error_msg = (
                f"Source upload not found in storage ({source_key}) — it may have "
                "been removed by a prior attempt, a cleanup run or a system restart"
            )
            log.error("extraction.source_missing", status="error")
            store.update_result(job_id, file_idx, FileResult(
                filename=filename, status="error", error=_error_msg,
            ))
            return {"status": "error", "job_id": job_id, "file_idx": file_idx, "error": _error_msg}

        # Extraction done — the source object is no longer needed.
        discard_source_object(source_key, log_context=_log_ctx)

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
        # Soft time limit fired — not retryable; drop the source object.
        discard_source_object(source_key, log_context=f"timeout {_log_ctx}")
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
            # Source object stays in storage — next retry downloads it again
            # (a StorageUnavailable download failure lands here too).
            raise self.retry(exc=exc, countdown=backoff)
        except MaxRetriesExceededError:
            # Final failure — no more retries; drop the source object.
            discard_source_object(source_key, log_context=f"max-retries {_log_ctx}")
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
