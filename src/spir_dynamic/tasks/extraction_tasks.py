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

        # Store extracted row data in Redis so the combine endpoint can reuse it
        # without re-running the extraction pipeline.
        # Key: rows:{file_id}  — namespaced to avoid collision with xlsx storage.
        try:
            import json as _json
            from spir_dynamic.app.config import get_settings as _gs
            _row_payload = _json.dumps({
                "cols": result.get("preview_cols", []),
                "rows": result.get("preview_rows", []),
                "spir_no": result.get("spir_no", ""),
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
