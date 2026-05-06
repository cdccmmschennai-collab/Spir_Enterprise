"""
Redis-backed file storage.

Drop-in replacement for InMemoryStorage when Celery workers are enabled.
Celery workers run in separate processes and cannot share in-process memory,
so extracted files must be stored in Redis so both API and workers can access them.
"""
from __future__ import annotations

import logging
from typing import Optional

import redis

log = logging.getLogger(__name__)

DEFAULT_TTL = 3600  # must match services/storage.py DEFAULT_TTL

_KEY_DATA = "storage:data:{}"
_KEY_NAME = "storage:name:{}"


class RedisStorage:
    """Redis-backed file storage with TTL. Same interface as InMemoryStorage."""

    def __init__(self, redis_url: str) -> None:
        self._r = redis.Redis.from_url(redis_url, decode_responses=False)

    def put(self, file_id: str, data: bytes, filename: str, ttl: int = DEFAULT_TTL) -> None:
        pipe = self._r.pipeline()
        pipe.set(_KEY_DATA.format(file_id), data, ex=ttl)
        pipe.set(_KEY_NAME.format(file_id), filename.encode("utf-8"), ex=ttl)
        pipe.execute()

    def get(self, file_id: str) -> Optional[tuple[bytes, str]]:
        pipe = self._r.pipeline()
        pipe.get(_KEY_DATA.format(file_id))
        pipe.get(_KEY_NAME.format(file_id))
        data, name_raw = pipe.execute()
        if data is None:
            return None
        name = name_raw.decode("utf-8") if isinstance(name_raw, bytes) else (name_raw or "")
        return data, name

    def delete(self, file_id: str) -> None:
        self._r.delete(_KEY_DATA.format(file_id), _KEY_NAME.format(file_id))

    @property
    def backend(self) -> str:
        return "redis"
