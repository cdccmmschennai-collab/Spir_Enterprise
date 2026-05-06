"""
In-memory result storage with TTL expiry.
Simplified from enterprise version — memory backend only.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_TTL = 3600  # 1 hour


class InMemoryStorage:
    """Thread-safe in-memory storage with TTL."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[bytes, str, float]] = {}
        self._lock = threading.Lock()

    def put(self, file_id: str, data: bytes, filename: str, ttl: int = DEFAULT_TTL) -> None:
        with self._lock:
            self._store[file_id] = (data, filename, time.monotonic() + ttl)

    def get(self, file_id: str) -> Optional[tuple[bytes, str]]:
        with self._lock:
            entry = self._store.get(file_id)
        if not entry:
            return None
        data, filename, exp = entry
        if time.monotonic() > exp:
            self.delete(file_id)
            return None
        return data, filename

    def delete(self, file_id: str) -> None:
        with self._lock:
            self._store.pop(file_id, None)

    @property
    def backend(self) -> str:
        return "memory"


# Singleton — type is InMemoryStorage or RedisStorage depending on config
_storage = None


def get_storage():
    """
    Return the active file storage backend.
    Uses Redis when celery_enabled=True so Celery workers (separate processes)
    can access files written by the API process and vice versa.
    Falls back to InMemoryStorage when Celery is disabled.
    """
    global _storage
    if _storage is None:
        try:
            from spir_dynamic.app.config import get_settings
            settings = get_settings()
            if settings.celery_enabled:
                from spir_dynamic.services.redis_store import RedisStorage
                _storage = RedisStorage(settings.redis_url)
                log.info("File storage: Redis (%s)", settings.redis_url)
            else:
                _storage = InMemoryStorage()
                log.info("File storage: in-memory")
        except Exception:
            _storage = InMemoryStorage()
    return _storage
