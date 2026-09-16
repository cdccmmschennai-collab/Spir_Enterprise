"""
Backend selection — Phase 3B.

The application has three storage *areas*, each of which today is one
directory. A backend instance is built per area so the existing layout is
kept as-is on the filesystem and mirrored as a key prefix on MinIO:

    area             filesystem root (Settings)     minio key prefix
    ---------------  -----------------------------  -----------------
    EXTRACTED_ROWS   rows_storage_path              extracted_rows/
    BATCH_UPLOADS    batch_upload_dir               batch_uploads/
    AVATARS          avatar_dir                     avatars/

Settings.storage_backend picks the implementation ("filesystem" is the
default). Phase 3C adds one per-area override: Settings.upload_storage_backend
selects the backend for BATCH_UPLOADS alone (the source workbooks handed to the
Celery workers), so MinIO can hold those while the other areas stay on disk.
"""
from __future__ import annotations

import threading
from enum import Enum

import structlog

from spir_dynamic.app.config import Settings, get_settings
from spir_dynamic.services.object_storage.base import ObjectStorage, StorageConfigError
from spir_dynamic.services.object_storage.local import LocalFilesystemStorage
from spir_dynamic.services.object_storage.minio import MinioObjectStorage

log = structlog.stdlib.get_logger(__name__)

BACKEND_FILESYSTEM = "filesystem"
BACKEND_MINIO = "minio"
SUPPORTED_BACKENDS = (BACKEND_FILESYSTEM, BACKEND_MINIO)


class StorageArea(str, Enum):
    EXTRACTED_ROWS = "extracted_rows"
    BATCH_UPLOADS = "batch_uploads"
    AVATARS = "avatars"


def local_root(area: StorageArea, settings: Settings) -> str:
    """The directory an area lives in today — unchanged from the pre-3B call sites."""
    if area is StorageArea.EXTRACTED_ROWS:
        return settings.rows_storage_path
    if area is StorageArea.BATCH_UPLOADS:
        return settings.batch_upload_dir
    if area is StorageArea.AVATARS:
        return settings.avatar_dir
    raise StorageConfigError(f"unknown storage area: {area!r}")   # pragma: no cover


def area_backend(area: StorageArea, settings: Settings) -> str:
    """Backend name for `area`: the global setting, unless the area has an explicit override."""
    if area is StorageArea.BATCH_UPLOADS and settings.upload_storage_backend:
        return settings.upload_storage_backend
    return settings.storage_backend


def build_object_storage(area: StorageArea, settings: Settings | None = None) -> ObjectStorage:
    """Construct a fresh backend for `area` from settings (no caching)."""
    cfg = settings or get_settings()
    backend = area_backend(area, cfg)
    if backend == BACKEND_FILESYSTEM:
        return LocalFilesystemStorage(local_root(area, cfg))
    if backend == BACKEND_MINIO:
        return MinioObjectStorage.from_settings(cfg, prefix=f"{area.value}/")
    raise StorageConfigError(
        f"unsupported storage backend {backend!r} for area {area.value!r}; "
        f"expected one of {SUPPORTED_BACKENDS}"
    )


_instances: dict[StorageArea, ObjectStorage] = {}
_lock = threading.Lock()


def get_object_storage(area: StorageArea) -> ObjectStorage:
    """Process-wide backend for `area`, built once from get_settings()."""
    inst = _instances.get(area)
    if inst is None:
        with _lock:
            inst = _instances.get(area)
            if inst is None:
                inst = build_object_storage(area)
                _instances[area] = inst
                log.info("object_storage.backend", area=area.value, backend=inst.backend, target=repr(inst))
    return inst


def reset_object_storage() -> None:
    """Drop cached instances (tests / settings reload)."""
    with _lock:
        _instances.clear()
