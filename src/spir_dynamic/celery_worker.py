"""
Celery application factory.
Start the worker with:
    celery -A spir_dynamic.celery_worker worker --loglevel=info --concurrency=4
"""
from __future__ import annotations

from celery import Celery

from spir_dynamic.app.config import get_settings


def _make_app() -> Celery:
    cfg = get_settings()
    app = Celery(
        "spir_dynamic",
        broker=cfg.redis_url,
        backend=cfg.redis_url,
        include=["spir_dynamic.tasks"],
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_track_started=True,
        result_expires=cfg.batch_ttl_seconds,
        worker_prefetch_multiplier=1,
        task_acks_late=True,
    )
    return app


celery_app = _make_app()
