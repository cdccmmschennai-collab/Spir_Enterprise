"""
Base Celery task with structured lifecycle logging.

Uses structlog (configured by worker.py before any tasks are imported).
Each lifecycle hook emits a JSON-safe event dict with task name, task ID,
duration, retry count, and exception details where applicable.
"""
from __future__ import annotations

import time
from typing import Any

import structlog
from celery import Task

from spir_dynamic.celery_app import celery_app

log = structlog.stdlib.get_logger(__name__)

# Stores wall-clock start time per task ID so lifecycle hooks can emit duration_s.
# A plain dict is safe here: worker_prefetch_multiplier=1 means one task runs
# per process at a time, so there is no concurrent write to the same key.
_task_started_at: dict[str, float] = {}


def _pop_duration(task_id: str) -> float | None:
    started = _task_started_at.pop(task_id, None)
    return round(time.perf_counter() - started, 3) if started is not None else None


class BaseTask(Task):
    abstract = True  # not registered as a task itself

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        _task_started_at[self.request.id] = time.perf_counter()
        return super().__call__(*args, **kwargs)

    def on_success(self, retval: Any, task_id: str, args: tuple, kwargs: dict) -> None:
        log.info(
            "task.success",
            task=self.name,
            task_id=task_id,
            duration_s=_pop_duration(task_id),
        )

    def on_failure(
        self,
        exc: Exception,
        task_id: str,
        args: tuple,
        kwargs: dict,
        einfo: Any,
    ) -> None:
        log.exception(
            "task.failure",
            task=self.name,
            task_id=task_id,
            exc_type=type(exc).__name__,
            exc_message=str(exc),
            duration_s=_pop_duration(task_id),
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    def on_retry(
        self,
        exc: Exception,
        task_id: str,
        args: tuple,
        kwargs: dict,
        einfo: Any,
    ) -> None:
        # Don't pop duration on retry — the task will restart and record a new start time.
        _task_started_at.pop(task_id, None)
        log.warning(
            "task.retry",
            task=self.name,
            task_id=task_id,
            attempt=self.request.retries + 1,
            max_retries=self.max_retries,
            exc_type=type(exc).__name__,
            exc_message=str(exc),
        )


# Convenience decorator for defining new tasks
task = celery_app.task(base=BaseTask)


@celery_app.task(base=BaseTask, name="spir_dynamic.tasks.ping")
def ping(message: str = "pong") -> str:
    """Smoke-test task. Verify with: ping.delay('hello').get(timeout=10)"""
    log.info("ping", message=message)
    return message
