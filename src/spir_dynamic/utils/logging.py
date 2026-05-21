"""Structured logging configuration using structlog."""
from __future__ import annotations

import functools
import logging
import sys
import time

import structlog


def timed(fn):
    """Decorator that emits a structured timer event with elapsed wall-clock time."""
    _log = structlog.stdlib.get_logger(fn.__module__)
    _name = fn.__qualname__

    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            _log.info("timer", fn=_name, duration_s=round(time.perf_counter() - t0, 4))

    return _wrapper


def setup_logging(log_level: str = "INFO", log_format: str = "text") -> None:
    """Configure structlog + stdlib logging for the application.

    All records — from structlog.get_logger() AND from logging.getLogger() —
    flow through the same processor chain and are rendered in the same format.
    structlog contextvars (request_id, job_id, etc.) are merged into every line.

    Args:
        log_level:  Logging level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_format: "json" → JSONRenderer (production / machine-parseable).
                    "text" → ConsoleRenderer (development / human-readable).
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    use_json = log_format.lower() == "json"

    # Processors run on EVERY record — structlog native and stdlib foreign alike.
    # merge_contextvars injects request_id / job_id / user_id set by middleware/tasks.
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer = structlog.processors.JSONRenderer() if use_json else structlog.dev.ConsoleRenderer()

    # structlog pipeline: process up to wrap_for_formatter, then hand off to
    # ProcessorFormatter for the final render step (avoids double-rendering).
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # ProcessorFormatter renders both structlog records (via wrap_for_formatter)
    # and foreign stdlib records (via foreign_pre_chain → renderer).
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
