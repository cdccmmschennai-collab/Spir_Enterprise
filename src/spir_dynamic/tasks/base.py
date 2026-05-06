"""
Base Celery task with lifecycle logging.
Uses stdlib logging only — structlog is not configured in the worker process.
"""
from __future__ import annotations

import logging
from typing import Any

from celery import Task

from spir_dynamic.celery_app import celery_app

log = logging.getLogger(__name__)


class BaseTask(Task):
    abstract = True  # not registered as a task itself

    def on_success(self, retval: Any, task_id: str, args: tuple, kwargs: dict) -> None:
        log.info("Task succeeded | task=%s id=%s", self.name, task_id)

    def on_failure(
        self,
        exc: Exception,
        task_id: str,
        args: tuple,
        kwargs: dict,
        einfo: Any,
    ) -> None:
        log.error(
            "Task failed | task=%s id=%s exc_type=%s exc=%s",
            self.name,
            task_id,
            type(exc).__name__,
            exc,
            exc_info=True,
        )

    def on_retry(
        self,
        exc: Exception,
        task_id: str,
        args: tuple,
        kwargs: dict,
        einfo: Any,
    ) -> None:
        log.warning(
            "Task retrying | task=%s id=%s exc=%s",
            self.name,
            task_id,
            exc,
        )


# Convenience decorator for defining new tasks
task = celery_app.task(base=BaseTask)


@celery_app.task(base=BaseTask, name="spir_dynamic.tasks.ping")
def ping(message: str = "pong") -> str:
    """Smoke-test task. Verify with: ping.delay('hello').get(timeout=10)"""
    log.info("ping received: %s", message)
    return message
