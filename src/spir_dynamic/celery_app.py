"""
Celery application singleton.
Only imports from spir_dynamic.app.config — no routers, no tasks at module level.
"""
from __future__ import annotations

from celery import Celery

from spir_dynamic.app.config import get_settings

_settings = get_settings()

celery_app = Celery(
    "spir_dynamic",
    broker=_settings.redis_url,
    backend=_settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # Reliability: acks_late keeps the broker message until the task completes.
    # reject_on_worker_lost re-queues the task if the worker process is killed
    # mid-extraction (OOM kill, systemd SIGKILL, etc.).
    task_acks_late=True,
    task_reject_on_worker_lost=True,

    # One task at a time per worker process — prevents simultaneous large Excel
    # files from consuming all memory in a single worker.
    worker_prefetch_multiplier=1,

    # Time limits: soft limit raises SoftTimeLimitExceeded inside the task so
    # it can log and exit cleanly; hard limit sends SIGKILL after the grace period.
    # Typical SPIR extraction takes 5–30s. 300s / 360s gives a 10x safety margin.
    task_soft_time_limit=300,    # 5 minutes → raises SoftTimeLimitExceeded
    task_time_limit=360,         # 6 minutes → SIGKILL (hard kill)

    # Worker recycling: replace the worker process after N tasks to prevent
    # openpyxl memory from accumulating over many jobs. Set to 25 for a 2-vCPU
    # VPS shared with WordPress — more frequent recycling keeps memory in check.
    worker_max_tasks_per_child=25,

    # Hard memory cap per worker child (kilobytes). 1 GB = 1_048_576 KB.
    # Recycles the child if it exceeds this limit — prevents runaway memory.
    worker_max_memory_per_child=1_048_576,

    # Reconnect on startup instead of crashing if Redis is briefly unavailable
    # when the worker first connects (e.g. systemd start-order races).
    broker_connection_retry_on_startup=True,

    # Visibility timeout must exceed task_time_limit so Redis does not
    # re-deliver a task that is still running. 900s = 2.5× the hard kill limit.
    broker_transport_options={"visibility_timeout": 900},

    timezone="UTC",
    enable_utc=True,
    result_expires=_settings.batch_ttl_seconds,

    # Lifecycle cleanup has no caller to pick a queue for it the way
    # batch_router does for extractions, so without a route it goes to Celery's
    # default "celery" queue. The deployed workers consume the extraction
    # queues (-Q normal,heavy and -Q giant) — nothing reads "celery", so Beat
    # enqueued a cleanup every night that was never executed and stale source
    # objects accumulated. Route it to CLEANUP_QUEUE ("normal" by default),
    # which every deployment runs a worker for.
    task_routes={
        "spir_dynamic.tasks.lifecycle_cleanup": {"queue": _settings.cleanup_queue},
    },

    # Imported at worker boot only — not at Python import time of this module
    include=[
        "spir_dynamic.tasks.base",
        "spir_dynamic.tasks.extraction_tasks",
        "spir_dynamic.tasks.cleanup_tasks",
    ],
)

# ── Celery Beat schedule ──────────────────────────────────────────────────────
# Beat runs as a separate process: celery -A spir_dynamic.celery_app beat
# The worker must also be running to execute the triggered tasks.
from celery.schedules import crontab  # noqa: E402

celery_app.conf.beat_schedule = {
    "lifecycle-cleanup-daily": {
        "task": "spir_dynamic.tasks.lifecycle_cleanup",
        "schedule": crontab(hour=2, minute=0),  # 02:00 UTC every day
        # dry_run=False means "do not force dry-run from the schedule"; the
        # task still honours CLEANUP_DRY_RUN, which is what actually decides
        # whether a production run deletes (cleanup_tasks.lifecycle_cleanup_task).
        "kwargs": {"dry_run": False},
        # Explicit, and identical to the task_routes entry above: a schedule
        # entry that carries no queue is only as good as the route, and this is
        # the one task whose queue has to be right or it silently never runs.
        "options": {"queue": _settings.cleanup_queue},
    },
}
