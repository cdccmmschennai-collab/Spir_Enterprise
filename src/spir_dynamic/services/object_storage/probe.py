"""
MinIO connectivity helpers — Phase 3A (infrastructure only).

This module deliberately does NOT provide an S3 client or a storage backend.
It only turns the MINIO_* settings into a health-probe URL and checks that the
MinIO service is reachable over the Docker network, so the wiring
(Compose env -> Settings -> network) can be verified before any workflow is
migrated. Nothing in the API, workers or Beat imports this at startup.

The storage abstraction itself (Phase 3B) lives next to this module:
base.py (contract), local.py, minio.py and factory.py. Migration of the
batch_uploads / extracted_rows workflows onto it is a later phase.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass

import structlog

from spir_dynamic.app.config import Settings, get_settings

log = structlog.stdlib.get_logger(__name__)

# Unauthenticated liveness endpoint served by the MinIO S3 API port.
_HEALTH_PATH = "/minio/health/live"


@dataclass(frozen=True)
class MinioProbe:
    reachable: bool
    url: str
    status: int | None = None
    error: str | None = None


def minio_health_url(settings: Settings | None = None) -> str:
    """Absolute URL of MinIO's liveness endpoint for the configured endpoint."""
    cfg = settings or get_settings()
    if not cfg.minio_endpoint:
        raise ValueError("MINIO_ENDPOINT is not configured")
    return cfg.minio_endpoint.rstrip("/") + _HEALTH_PATH


def check_minio_reachable(settings: Settings | None = None, timeout: float = 5.0) -> MinioProbe:
    """
    GET the liveness endpoint. Never raises for network problems — returns a
    MinioProbe with reachable=False and the error text instead, so callers can
    log/skip without taking the service down.
    """
    cfg = settings or get_settings()
    if not cfg.minio_configured:
        return MinioProbe(reachable=False, url=cfg.minio_endpoint, error="minio not configured")
    url = minio_health_url(cfg)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("minio.unreachable", url=url, error=str(exc))
        return MinioProbe(reachable=False, url=url, error=str(exc))
    ok = status == 200
    log.info("minio.probe", url=url, status=status, reachable=ok)
    return MinioProbe(reachable=ok, url=url, status=status)
