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
default and the only backend the application workflow uses in this phase).
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


def build_object_storage(area: StorageArea, settings: Settings | None = None) -> ObjectStorage:
    """Construct a fresh backend for `area` from settings (no caching)."""
    cfg = settings or get_settings()
    backend = cfg.storage_backend
    if backend == BACKEND_FILESYSTEM:
        return LocalFilesystemStorage(local_root(area, cfg))
    if backend == BACKEND_MINIO:
        return MinioObjectStorage.from_settings(cfg, prefix=f"{area.value}/")
    raise StorageConfigError(
        f"unsupported STORAGE_BACKEND={backend!r}; expected one of {SUPPORTED_BACKENDS}"
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
