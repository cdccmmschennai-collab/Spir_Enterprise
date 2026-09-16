"""
Object storage layer.

    Application code
        |
        v
    ObjectStorage contract (base.py) + backend selection (factory.py)
        |
        +-- LocalFilesystemStorage (local.py)   <- active backend (Phase 3B)
        +-- MinioObjectStorage     (minio.py)   <- available, not yet used by any workflow

Import from this package only; the backend modules are implementation detail.
The Phase 3A health probe (probe.py) keeps its original import path here.
"""
from __future__ import annotations

from spir_dynamic.services.object_storage.base import (
    InvalidObjectKey,
    ObjectInfo,
    ObjectNotFound,
    ObjectStorage,
    StorageConfigError,
    StorageError,
    StorageUnavailable,
    normalize_key,
)
from spir_dynamic.services.object_storage.factory import (
    BACKEND_FILESYSTEM,
    BACKEND_MINIO,
    SUPPORTED_BACKENDS,
    StorageArea,
    build_object_storage,
    get_object_storage,
    reset_object_storage,
)
from spir_dynamic.services.object_storage.local import LocalFilesystemStorage
from spir_dynamic.services.object_storage.minio import MinioObjectStorage
from spir_dynamic.services.object_storage.probe import (
    MinioProbe,
    check_minio_reachable,
    minio_health_url,
)

__all__ = [
    "BACKEND_FILESYSTEM",
    "BACKEND_MINIO",
    "InvalidObjectKey",
    "LocalFilesystemStorage",
    "MinioObjectStorage",
    "MinioProbe",
    "ObjectInfo",
    "ObjectNotFound",
    "ObjectStorage",
    "SUPPORTED_BACKENDS",
    "StorageArea",
    "StorageConfigError",
    "StorageError",
    "StorageUnavailable",
    "build_object_storage",
    "check_minio_reachable",
    "get_object_storage",
    "minio_health_url",
    "normalize_key",
    "reset_object_storage",
]
