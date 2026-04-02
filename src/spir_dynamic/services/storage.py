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


# Singleton
_storage: InMemoryStorage | None = None


def get_storage() -> InMemoryStorage:
    global _storage
    if _storage is None:
        _storage = InMemoryStorage()
    return _storage
