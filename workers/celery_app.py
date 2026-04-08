"""
workers/celery_app.py
──────────────────────
Celery application — broker and result backend configuration.

Kept in workers/ (not app/) so the Celery worker process can import
it without loading the full FastAPI app.

To start workers:
    celery -A workers.celery_app worker --loglevel=info --concurrency=2
    celery -A workers.celery_app flower     ← monitoring UI (port 5555)

CONCURRENCY:
    --concurrency=2 means 2 tasks at a time per worker process.
    Each task loads a 50-2000MB file into memory.
    RAM per worker ≈ 2 × (file_size × 3)  (openpyxl uses ~3× raw file size).
    For 4 workers × 2 concurrent × 500MB = ~12GB RAM minimum.
    Tune concurrency to your server's available RAM.

SCALING:
    Add more worker containers in docker-compose.yml:
        docker compose up --scale worker=4
    Each container runs independently; Redis coordinates.
"""
from __future__ import annotations
import os
import sys

# Ensure backend/ is on the path when the worker is started from root
_here = os.path.dirname(__file__)
_backend = os.path.join(_here, '..', 'backend')
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from celery import Celery
from app.config import get_settings

cfg = get_settings()

celery_app = Celery(
    "spir_workers",
    broker  = cfg.celery_broker,
    backend = cfg.celery_backend,
    include = ["workers.tasks"],
)

celery_app.conf.update(
    task_serializer            = "json",
    result_serializer          = "json",
    accept_content             = ["json"],
    result_expires             = cfg.result_ttl_seconds,
    task_track_started         = True,
    task_acks_late             = True,      # re-queue if worker crashes mid-task
    worker_prefetch_multiplier = 1,         # one task at a time per worker slot
    task_soft_time_limit       = 600,       # 10 min soft limit → raises SoftTimeLimitExceeded
    task_time_limit            = 660,       # 11 min hard kill
    task_routes                = {
        "workers.tasks.process_spir_task": {"queue": "spir_extraction"},
    },
)
