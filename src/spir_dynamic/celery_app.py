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
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    timezone="UTC",
    enable_utc=True,
    result_expires=_settings.batch_ttl_seconds,
    # Imported at worker boot only — not at Python import time of this module
    include=[
        "spir_dynamic.tasks.base",
        "spir_dynamic.tasks.extraction_tasks",
    ],
)
