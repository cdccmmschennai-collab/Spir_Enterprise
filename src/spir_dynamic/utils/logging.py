"""Structured logging configuration using structlog."""
from __future__ import annotations

import functools
import logging
import sys
import time

import structlog


def timed(fn):
    """
    Decorator that logs entry, exit, and elapsed wall-clock time for a function.

    Log format:
        [TIMER START] qualified.name
        [TIMER END]   qualified.name — X.XXXXs
    """
    _log = logging.getLogger(fn.__module__)
    _name = fn.__qualname__

    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        _log.info("[TIMER START] %s", _name)
        _t0 = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            _log.info("[TIMER END]   %s — %.4fs", _name, time.perf_counter() - _t0)

    return _wrapper


def setup_logging(log_level: str = "INFO") -> None:
    """Configure structlog + stdlib logging for the application."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )
