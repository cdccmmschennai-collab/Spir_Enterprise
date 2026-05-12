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

    # Imported at worker boot only — not at Python import time of this module
    include=[
        "spir_dynamic.tasks.base",
        "spir_dynamic.tasks.extraction_tasks",
    ],
)
