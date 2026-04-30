"""
Celery tasks for background SPIR extraction.
"""
from __future__ import annotations

import base64
import logging

from spir_dynamic.celery_worker import celery_app
from spir_dynamic.app.pipeline import run_pipeline

log = logging.getLogger(__name__)


@celery_app.task(bind=True, name="spir.extract", max_retries=0)
def extraction_task(self, file_bytes_b64: str, filename: str) -> dict:
    """
    Decode file bytes and run the full extraction pipeline.
    Checks cancel flag cooperatively — before and after pipeline execution.
    """
    from spir_dynamic.services.job_tracker import get_job_tracker
    job_id = self.request.id
    tracker = get_job_tracker()

    # Pre-check: cancelled before work starts
    if tracker.is_cancelled(job_id):
        tracker.set_status(job_id, "cancelled")
        log.info("Task %s cancelled before start", job_id)
        return {"status": "cancelled", "job_id": job_id}

    tracker.set_status(job_id, "processing")
    log.info("Task %s processing: %s", job_id, filename)

    try:
        file_bytes = base64.b64decode(file_bytes_b64)
        result = run_pipeline(file_bytes, filename)

        # Post-check: cancelled while pipeline ran — discard result
        if tracker.is_cancelled(job_id):
            tracker.set_status(job_id, "cancelled")
            log.info("Task %s cancelled post-pipeline — result discarded", job_id)
            return {"status": "cancelled", "job_id": job_id}

        tracker.set_status(job_id, "completed")
        result["status"] = "completed"
        log.info("Task %s done: %d rows", job_id, result.get("total_rows", 0))
        return result

    except Exception as exc:
        tracker.set_status(job_id, "failed", error=str(exc))
        log.error("Task %s failed: %s", job_id, exc, exc_info=True)
        raise
