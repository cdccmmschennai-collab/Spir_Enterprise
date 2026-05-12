"""
In-memory batch job store.

Tracks the state of multi-file batch extraction jobs. Each job holds a list
of FileResult entries — one per uploaded file — updated as files complete.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)


@dataclass
class FileResult:
    filename: str
    status: str = "pending"      # "pending" | "ok" | "error"
    total_rows: int = 0
    total_tags: int = 0
    spir_no: str = ""
    file_id: str = ""            # storage key for individual download
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BatchJob:
    job_id: str
    total: int
    results: list[FileResult]
    created_at: datetime
    expires_at: datetime
    user_id: str = ""

    @property
    def completed(self) -> int:
        return sum(1 for r in self.results if r.status in ("ok", "error"))

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.results if r.status == "ok")

    @property
    def status(self) -> str:
        if self.completed == 0:
            return "processing"
        if self.completed < self.total:
            return "processing"
        if self.succeeded == 0:
            return "failed"
        if self.succeeded < self.total:
            return "partial"
        return "done"

    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) > self.expires_at

    def to_dict(self) -> dict:
        # Annotate each pending file with its queue position within this job
        # (1-based) so the frontend can show "queued — position 3 of 5 waiting".
        results_out = []
        pending_pos = 0
        for r in self.results:
            d = r.to_dict()
            if r.status == "pending":
                pending_pos += 1
                d["queue_position"] = pending_pos
            else:
                d["queue_position"] = None
            results_out.append(d)
        return {
            "job_id": self.job_id,
            "status": self.status,
            "total": self.total,
            "completed": self.completed,
            "succeeded": self.succeeded,
            "pending_count": self.total - self.completed,
            "results": results_out,
        }


class JobStore:
    """Thread-safe in-memory batch job store with TTL expiry."""

    def __init__(self, ttl_seconds: int = 7200):
        self._jobs: dict[str, BatchJob] = {}
        self._ttl = ttl_seconds
        self._lock = threading.Lock()

    def create(self, job_id: str, filenames: list[str], user_id: str = "") -> BatchJob:
        now = datetime.now(timezone.utc)
        job = BatchJob(
            job_id=job_id,
            total=len(filenames),
            results=[FileResult(filename=fn) for fn in filenames],
            created_at=now,
            expires_at=now + timedelta(seconds=self._ttl),
            user_id=user_id,
        )
        with self._lock:
            self._jobs[job_id] = job
            self._purge_expired()
        return job

    def get(self, job_id: str) -> BatchJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.is_expired():
                self._jobs.pop(job_id, None)
                return None
            return job

    def update_result(self, job_id: str, idx: int, result: FileResult) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and 0 <= idx < len(job.results):
                job.results[idx] = result

    def _purge_expired(self) -> None:
        # Must be called while holding self._lock.
        expired = [jid for jid, j in self._jobs.items() if j.is_expired()]
        for jid in expired:
            del self._jobs[jid]


# Singleton — type is JobStore or RedisJobStore depending on config
_job_store = None
_job_store_lock = threading.Lock()


def get_job_store():
    """
    Return the active job store backend.
    Uses Redis when celery_enabled=True so Celery workers (separate processes)
    can write job-result updates that the API polling endpoint can read.
    Falls back to in-memory JobStore when Celery is disabled.
    """
    global _job_store
    if _job_store is None:
        with _job_store_lock:
            if _job_store is None:  # re-check after acquiring lock
                try:
                    from spir_dynamic.app.config import get_settings
                    settings = get_settings()
                    if settings.celery_enabled:
                        from spir_dynamic.services.redis_job_store import RedisJobStore
                        _job_store = RedisJobStore(settings.redis_url, settings.batch_ttl_seconds)
                        log.debug("Job store: Redis (%s)", settings.redis_url)
                    else:
                        _job_store = JobStore(ttl_seconds=settings.batch_ttl_seconds)
                        log.debug("Job store: in-memory")
                except Exception:
                    _job_store = JobStore(ttl_seconds=7200)
    return _job_store
