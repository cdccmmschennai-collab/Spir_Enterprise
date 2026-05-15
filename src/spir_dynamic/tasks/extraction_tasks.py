"""
Celery task: process one file within a batch job.

Each uploaded file gets its own task so workers can process files for
different users (and different files of the same user) in true parallel
without any shared mutable state.

State flow:  pending → running → ok | error
Input bytes are stored in Redis before enqueue (to avoid large messages in
the broker queue) and cleaned up by the task on first attempt.
"""
from __future__ import annotations

import logging

from celery.exceptions import MaxRetriesExceededError

from spir_dynamic.celery_app import celery_app
from spir_dynamic.tasks.base import BaseTask

log = logging.getLogger(__name__)


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
    input_key: str,
    filename: str,
    user_id: str = "",
) -> dict:
    """
    Extract a single SPIR file as part of a batch job.

    Args:
        job_id:    Batch job identifier — shared across all files in the batch.
        file_idx:  Position of this file in the batch (0-based).
        input_key: Redis storage key where the raw input bytes were pre-stored.
        filename:  Original upload filename (used for pipeline and error reporting).

    Returns:
        Dict with status + result metadata (mirrors run_pipeline output keys).
    """
    from spir_dynamic.app.pipeline import run_pipeline
    from spir_dynamic.services.job_store import FileResult, get_job_store
    from spir_dynamic.services.storage import get_storage

    store = get_job_store()
    storage = get_storage()

    # Mark this slot as running so the status endpoint shows progress
    store.update_result(job_id, file_idx, FileResult(filename=filename, status="running"))

    try:
        # Retrieve and immediately delete the input bytes stored by the API process
        entry = storage.get(input_key)
        if entry is None:
            raise RuntimeError(f"Input file not found in storage (key={input_key}); "
                               "it may have expired or been cleaned up on a prior retry")
        file_bytes, _ = entry
        if self.request.retries == 0:
            # Only delete on first attempt — retries re-use the same key
            storage.delete(input_key)

        # run_pipeline is CPU-bound/sync — runs directly in the Celery worker process
        result = run_pipeline(file_bytes, filename)

        store.update_result(job_id, file_idx, FileResult(
            filename=filename,
            status="ok",
            total_rows=result.get("total_rows", 0),
            total_tags=result.get("total_tags", 0),
            spir_no=result.get("spir_no", ""),
            file_id=result.get("file_id", ""),
        ))

        # Store extracted row data + metadata in Redis so combine and async polling
        # can reuse it without re-running the extraction pipeline.
        # Key: rows:{file_id}  — namespaced to avoid collision with xlsx storage.
        try:
            import json as _json
            from spir_dynamic.app.config import get_settings as _gs
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
            storage.put(
                f"rows:{result['file_id']}",
                _row_payload,
                "rows.json",
                ttl=_gs().batch_ttl_seconds,
            )
        except Exception as _row_exc:
            # Non-fatal: row storage failing does not break extraction or download.
            log.warning("Row data storage failed (combine unavailable for this file): %s", _row_exc)

        # ── Persist rows to disk so history-based combine can read them ──────────
        # The sync route writes {rows_storage_path}/{file_id}.json; we do the same
        # here so both code paths produce the same on-disk artefact and the combine
        # endpoint works for async-extracted files.
        json_path: str | None = None
        try:
            import json as _json_disk
            from pathlib import Path as _Path
            from spir_dynamic.app.config import get_settings as _gs_disk
            _cfg = _gs_disk()
            _rows_dir = _Path(_cfg.rows_storage_path)
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
                log.debug("Rows written to disk: %s (%d rows)", _disk_path, result.get("total_rows", 0))
        except Exception as _disk_exc:
            log.warning("Disk row storage failed (non-fatal, combine will be unavailable): %s", _disk_exc)

        # ── Write to extraction_history so frontend history is populated ─────────
        # Uses a dedicated synchronous DB writer (psycopg2 + NullPool) — zero
        # asyncio, zero event loops, safe across all Celery concurrency models.
        # user_id is passed at enqueue time so no store.get() lookup is needed.
        try:
            from spir_dynamic.services.audit_service import log_extraction_worker as _log_worker
            _log_worker(
                user_id=user_id,
                result=result,
                original_filename=filename,
                json_path=json_path,
            )
        except Exception as _hist_exc:
            log.warning("History logging failed in Celery task (non-fatal): %s", _hist_exc)

        log.info("process_file_task done | job=%s idx=%d file=%s rows=%d",
                 job_id, file_idx, filename, result.get("total_rows", 0))
        return {
            "status": "ok",
            "job_id": job_id,
            "file_idx": file_idx,
            "file_id": result.get("file_id", ""),
            "total_rows": result.get("total_rows", 0),
        }

    except Exception as exc:
        # Exponential backoff: 10s → 30s → 90s
        backoff = 10 * (3 ** self.request.retries)
        log.warning(
            "process_file_task failed (attempt %d/%d) | job=%s idx=%d file=%s: %s",
            self.request.retries + 1, self.max_retries + 1,
            job_id, file_idx, filename, exc,
        )
        try:
            raise self.retry(exc=exc, countdown=backoff)
        except MaxRetriesExceededError:
            log.error(
                "process_file_task exhausted retries | job=%s idx=%d file=%s: %s",
                job_id, file_idx, filename, exc, exc_info=True,
            )
            store.update_result(job_id, file_idx, FileResult(
                filename=filename,
                status="error",
                error=str(exc),
            ))
            # Return rather than re-raise so Celery marks as SUCCESS (result stored
            # in job store). The batch status endpoint reads status from the store,
            # not from the Celery task result.
            return {"status": "error", "job_id": job_id, "file_idx": file_idx, "error": str(exc)}
