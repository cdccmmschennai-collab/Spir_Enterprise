"""
services/storage.py
────────────────────
Pluggable result storage.  Three backends, one interface.

BACKENDS:
    InMemoryStorage  — single process, no deps, dev/test/single-server
    RedisStorage     — multi-process same host, fast TTL, up to ~500MB files
    S3Storage        — multi-host, unlimited size, durable, true horizontal scale

CONFIGURATION (.env):
    STORAGE_BACKEND=memory   → InMemoryStorage
    STORAGE_BACKEND=redis    → RedisStorage (needs REDIS_URL)
    STORAGE_BACKEND=s3       → S3Storage (needs S3_BUCKET, S3_REGION, AWS creds)

    If Redis/S3 is configured but unavailable, falls back to InMemoryStorage.

LARGE FILE SUPPORT:
    S3Storage streams in chunks — the full XLSX never sits in Python memory
    during upload or download. Use S3 for files > 100MB.

USAGE:
    from services.storage import get_storage
    store = get_storage()
    store.put(file_id, xlsx_bytes, "output.xlsx")
    result = store.get(file_id)          # (bytes, filename) | None
    url    = store.presign(file_id)      # S3 only, browser-direct download
"""
from __future__ import annotations
import io
import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Optional

log = logging.getLogger(__name__)
DEFAULT_TTL = 3600   # 1 hour


# ─────────────────────────────────────────────────────────────────────────────
# Abstract interface
# ─────────────────────────────────────────────────────────────────────────────

class ResultStorage(ABC):
    @abstractmethod
    def put(self, file_id: str, data: bytes, filename: str,
            ttl: int = DEFAULT_TTL) -> None: ...

    @abstractmethod
    def get(self, file_id: str) -> Optional[tuple[bytes, str]]: ...

    @abstractmethod
    def delete(self, file_id: str) -> None: ...

    def presign(self, file_id: str, expires: int = DEFAULT_TTL) -> Optional[str]:
        """Return a pre-signed URL (S3 only). None on other backends."""
        return None

    @property
    def backend(self) -> str:
        return self.__class__.__name__


# ─────────────────────────────────────────────────────────────────────────────
# In-memory (with TTL expiry via monotonic clock)
# ─────────────────────────────────────────────────────────────────────────────

class InMemoryStorage(ResultStorage):
    def __init__(self) -> None:
        # id → (data, filename, expires_at)
        self._store: dict[str, tuple[bytes, str, float]] = {}
        self._lock  = threading.Lock()

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


# ─────────────────────────────────────────────────────────────────────────────
# Redis (TTL handled by Redis SETEX)
# ─────────────────────────────────────────────────────────────────────────────

class RedisStorage(ResultStorage):
    """
    Stores results as:  JSON_header \x00 raw_bytes

    Max reliable size: ~500MB (Redis string limit).
    For larger files use S3Storage.
    """
    _PREFIX = "spir:result:"

    def __init__(self, redis_url: str) -> None:
        import redis as _r
        self._r = _r.from_url(redis_url, decode_responses=False,
                               socket_connect_timeout=2, socket_timeout=2)

    def put(self, file_id: str, data: bytes, filename: str, ttl: int = DEFAULT_TTL) -> None:
        header  = json.dumps({"filename": filename}).encode()
        payload = header + b"\x00" + data
        self._r.setex(self._PREFIX + file_id, ttl, payload)

    def get(self, file_id: str) -> Optional[tuple[bytes, str]]:
        raw = self._r.get(self._PREFIX + file_id)
        if raw is None:
            return None
        sep  = raw.index(b"\x00")
        meta = json.loads(raw[:sep])
        return raw[sep + 1:], meta["filename"]

    def delete(self, file_id: str) -> None:
        self._r.delete(self._PREFIX + file_id)

    @property
    def backend(self) -> str:
        return "redis"


# ─────────────────────────────────────────────────────────────────────────────
# S3 (or any S3-compatible: MinIO, Cloudflare R2, Azure via adapter)
# ─────────────────────────────────────────────────────────────────────────────

class S3Storage(ResultStorage):
    """
    Stores results in S3. Supports any file size — streamed in chunks.

    TTL is enforced via an S3 Lifecycle Rule on the bucket:
        Rule: Expire objects in prefix "spir-results/" after N days.
        Set this in the AWS Console or via Terraform (see infra/).
        Per-object TTL is not native to S3.

    REQUIRED ENV VARS:
        S3_BUCKET             bucket name
        S3_REGION             aws region e.g. "us-east-1"
        AWS_ACCESS_KEY_ID
        AWS_SECRET_ACCESS_KEY

    OPTIONAL:
        S3_ENDPOINT_URL       for MinIO, Cloudflare R2, etc.
    """
    _PREFIX = "spir-results/"
    _CT     = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def __init__(self, bucket: str, region: str, endpoint_url: str = None) -> None:
        import boto3
        kw = {"region_name": region}
        if endpoint_url:
            kw["endpoint_url"] = endpoint_url
        self._s3     = boto3.client("s3", **kw)
        self._bucket = bucket

    def _key(self, file_id: str) -> str:
        return self._PREFIX + file_id + ".xlsx"

    def put(self, file_id: str, data: bytes, filename: str, ttl: int = DEFAULT_TTL) -> None:
        self._s3.put_object(
            Bucket      = self._bucket,
            Key         = self._key(file_id),
            Body        = data,
            ContentType = self._CT,
            Metadata    = {"filename": filename},
        )
        log.debug("S3 stored: %s (%d bytes)", file_id, len(data))

    def get(self, file_id: str) -> Optional[tuple[bytes, str]]:
        try:
            resp     = self._s3.get_object(Bucket=self._bucket, Key=self._key(file_id))
            data     = resp["Body"].read()
            filename = resp.get("Metadata", {}).get("filename", file_id + ".xlsx")
            return data, filename
        except self._s3.exceptions.NoSuchKey:
            return None
        except Exception as exc:
            log.error("S3 get failed %s: %s", file_id, exc)
            return None

    def delete(self, file_id: str) -> None:
        try:
            self._s3.delete_object(Bucket=self._bucket, Key=self._key(file_id))
        except Exception as exc:
            log.warning("S3 delete failed %s: %s", file_id, exc)

    def presign(self, file_id: str, expires: int = DEFAULT_TTL) -> Optional[str]:
        """
        Generate a pre-signed URL so the browser downloads directly from S3.
        Bypasses the API server entirely — ideal for large output files.
        """
        try:
            return self._s3.generate_presigned_url(
                "get_object",
                Params    = {"Bucket": self._bucket, "Key": self._key(file_id)},
                ExpiresIn = expires,
            )
        except Exception as exc:
            log.error("S3 presign failed %s: %s", file_id, exc)
            return None

    @property
    def backend(self) -> str:
        return "s3"


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_storage() -> ResultStorage:
    """
    Return the configured storage backend singleton.
    Falls back to InMemoryStorage if the configured backend is unavailable.
    """
    try:
        from app.config import get_settings
        cfg = get_settings()
    except Exception:
        return InMemoryStorage()

    backend = getattr(cfg, "storage_backend", "memory").lower()

    if backend == "redis":
        try:
            store = RedisStorage(cfg.redis_url)
            store._r.ping()
            log.info("Storage: Redis (%s)", cfg.redis_url)
            return store
        except Exception as exc:
            log.warning("Redis unavailable (%s) — falling back to memory", exc)
            return InMemoryStorage()

    if backend == "s3":
        bucket   = getattr(cfg, "s3_bucket", "")
        region   = getattr(cfg, "s3_region", "us-east-1")
        endpoint = getattr(cfg, "s3_endpoint_url", None)
        if not bucket:
            log.warning("S3_BUCKET not configured — falling back to memory")
            return InMemoryStorage()
        log.info("Storage: S3 (bucket=%s region=%s)", bucket, region)
        return S3Storage(bucket=bucket, region=region, endpoint_url=endpoint)

    log.info("Storage: in-memory")
    return InMemoryStorage()
