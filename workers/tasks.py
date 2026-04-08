"""
workers/tasks.py
──────────────────
Celery task definitions.

process_spir_task:
  - Receives file bytes (hex-encoded for JSON serialization)
  - Calls the same _execute() pipeline as the sync path
  - Updates Redis progress at each major step
  - Stores result in configured storage backend (Redis or S3)
"""
from __future__ import annotations
import json
import logging

from workers.celery_app import celery_app
from app.config import get_settings

log = logging.getLogger(__name__)
cfg = get_settings()


def _progress(job_id: str, status: str, pct: int, message: str = "") -> None:
    try:
        import redis as _r
        r = _r.from_url(cfg.redis_url, decode_responses=True,
                        socket_connect_timeout=2, socket_timeout=2)
        r.setex(
            f"spir:progress:{job_id}",
            cfg.result_ttl_seconds,
            json.dumps({"status": status, "progress": pct, "message": message}),
        )
    except Exception:
        pass


@celery_app.task(
    bind=True,
    name="workers.tasks.process_spir_task",
    max_retries=2,
    default_retry_delay=30,
)
def process_spir_task(self, file_hex: str, original_filename: str):
    """
    Full extraction pipeline as a Celery task.

    Args:
        file_hex:           File bytes encoded as hex string (JSON-safe).
        original_filename:  Original uploaded filename (for output naming).

    Returns:
        dict — same shape as run_pipeline(), stored in Celery result backend.
    """
    job_id = self.request.id
    log.info("Task %s started: %s", job_id, original_filename)

    try:
        _progress(job_id, "processing", 5, "Loading workbook…")
        file_bytes = bytes.fromhex(file_hex)

        _progress(job_id, "processing", 15, "Extracting spare parts…")
        from app.pipeline import _execute
        result = _execute(file_bytes, original_filename)

        _progress(job_id, "done", 100, "Complete")
        log.info("Task %s complete: %d rows, %d tags",
                 job_id, result["total_rows"], result["total_tags"])
        return result

    except Exception as exc:
        log.error("Task %s failed: %s", job_id, exc, exc_info=True)
        _progress(job_id, "failed", 0, str(exc))
        raise self.retry(exc=exc) if self.request.retries < self.max_retries else exc
