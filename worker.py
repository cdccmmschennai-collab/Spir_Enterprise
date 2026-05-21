"""
Celery worker entry point for spir_dynamic.

Usage
-----
Local (recommended):
    celery -A worker worker --loglevel=info

Windows (prefork pool unsupported on Windows):
    celery -A worker worker --loglevel=info --pool=solo

Direct Python:
    python worker.py worker --loglevel=debug

Docker CMD:
    CMD ["celery", "-A", "worker", "worker", "--loglevel=info", "--concurrency=4"]

The `celery -A worker` flag resolves the Celery app by looking for an `app`
attribute in this module — provided by `app = celery_app` below.

Environment
-----------
LOG_LEVEL  : logging level passed to setup_logging (default INFO)
LOG_FORMAT : "text" (default, human-readable) or "json" (production/JSON)
"""
from __future__ import annotations

import os

# Configure structured logging before importing any spir_dynamic modules.
# The worker process is separate from the API — main.py never runs here —
# so we initialise logging explicitly using the same setup_logging() call.
from spir_dynamic.utils.logging import setup_logging

setup_logging(
    log_level=os.getenv("LOG_LEVEL", "INFO"),
    log_format=os.getenv("LOG_FORMAT", "text"),
)

from spir_dynamic.celery_app import celery_app  # noqa: E402

app = celery_app  # `celery -A worker` discovers the app via this attribute

if __name__ == "__main__":
    celery_app.start()
