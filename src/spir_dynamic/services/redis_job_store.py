"""
Redis-backed batch job store.

Drop-in replacement for JobStore when Celery workers are enabled.
Each file result is stored as an independent Redis key so concurrent
worker tasks can update their own slot without locking each other.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta

import redis

from spir_dynamic.services.job_store import BatchJob, FileResult

log = logging.getLogger(__name__)

_KEY_META = "batch:meta:{}"       # hash: total, created_at, expires_at
_KEY_RESULT = "batch:result:{}:{}"  # string: JSON-serialised FileResult


class RedisJobStore:
    """Redis-backed job store with TTL. Same interface as JobStore."""

    def __init__(self, redis_url: str, ttl_seconds: int = 7200) -> None:
        self._r = redis.Redis.from_url(redis_url, decode_responses=True)
        self._ttl = ttl_seconds

    def create(self, job_id: str, filenames: list[str]) -> BatchJob:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=self._ttl)
        key_ttl = self._ttl + 300  # slight padding so keys outlive the TTL check

        pipe = self._r.pipeline()
        meta_key = _KEY_META.format(job_id)
        pipe.hset(meta_key, mapping={
            "total": len(filenames),
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        })
        pipe.expire(meta_key, key_ttl)

        for idx, fn in enumerate(filenames):
            result_key = _KEY_RESULT.format(job_id, idx)
            pipe.set(
                result_key,
                json.dumps({
                    "filename": fn, "status": "pending",
                    "total_rows": 0, "total_tags": 0,
                    "spir_no": "", "file_id": "", "error": "",
                }),
                ex=key_ttl,
            )

        pipe.execute()
        job = self.get(job_id)
        assert job is not None  # just created
        return job

    def get(self, job_id: str) -> BatchJob | None:
        meta_key = _KEY_META.format(job_id)
        meta = self._r.hgetall(meta_key)
        if not meta:
            return None

        total = int(meta["total"])
        created_at = datetime.fromisoformat(meta["created_at"])
        expires_at = datetime.fromisoformat(meta["expires_at"])

        if datetime.now(timezone.utc) > expires_at:
            return None

        # Fetch all per-file results in a single pipeline round-trip
        pipe = self._r.pipeline()
        for idx in range(total):
            pipe.get(_KEY_RESULT.format(job_id, idx))
        raw_results = pipe.execute()

        results: list[FileResult] = []
        for idx, raw in enumerate(raw_results):
            if raw:
                try:
                    d = json.loads(raw)
                    results.append(FileResult(**d))
                except Exception:
                    results.append(FileResult(filename=f"file_{idx}", status="error", error="corrupt state"))
            else:
                results.append(FileResult(filename=f"file_{idx}"))

        return BatchJob(
            job_id=job_id,
            total=total,
            results=results,
            created_at=created_at,
            expires_at=expires_at,
        )

    def update_result(self, job_id: str, idx: int, result: FileResult) -> None:
        meta_key = _KEY_META.format(job_id)
        if not self._r.exists(meta_key):
            log.warning("update_result: job %s not found in Redis", job_id)
            return
        result_key = _KEY_RESULT.format(job_id, idx)
        self._r.set(result_key, json.dumps(result.to_dict()), ex=self._ttl + 300)
