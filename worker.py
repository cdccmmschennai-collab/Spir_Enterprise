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
"""
from __future__ import annotations

import logging

# Configure logging before importing Celery app.
# The worker process never runs main.py, so setup_logging() from main.py
# is never called here — basicConfig provides a readable fallback.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from spir_dynamic.celery_app import celery_app  # noqa: E402

app = celery_app  # `celery -A worker` discovers the app via this attribute

if __name__ == "__main__":
    celery_app.start()
