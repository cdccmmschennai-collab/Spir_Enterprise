"""
Persisted "last successful rate" store — the secondary source in the
conversion fallback chain (live API -> this store -> static table).

Every successful live fetch is written here; when the provider is down the
service reads the most recent success back so a job can still convert. The
store is shared by the API and every Celery worker (Redis, AOF-persisted in
Docker / a system service in production), so it survives container restarts
— an in-process cache alone would not.

Keys never expire: a stale-but-real rate, clearly marked ``cached`` in the
snapshot with its original rate_date, beats a blank financial column. Every
error here is swallowed and logged — the store must never fail a job.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Optional, Protocol

import structlog

from spir_dynamic.services.currency_providers.base import RateQuote

log = structlog.stdlib.get_logger(__name__)

_KEY = "spir:currency:last_rate:{base}:{quote}"


class RateStore(Protocol):
    def get_last_success(self, base: str, quote: str) -> Optional[RateQuote]: ...
    def save_success(self, quote: RateQuote) -> None: ...


class NullRateStore:
    """No persisted cache (tests / explicit opt-out)."""

    name = "none"

    def get_last_success(self, base: str, quote: str) -> Optional[RateQuote]:
        return None

    def save_success(self, quote: RateQuote) -> None:
        return None


class InMemoryRateStore:
    """Dict-backed store — process-local; used by tests and as a stand-in."""

    name = "memory"

    def __init__(self) -> None:
        self._d: dict[tuple[str, str], RateQuote] = {}

    def get_last_success(self, base: str, quote: str) -> Optional[RateQuote]:
        return self._d.get((base.upper(), quote.upper()))

    def save_success(self, quote: RateQuote) -> None:
        self._d[(quote.base.upper(), quote.quote.upper())] = quote


class RedisRateStore:
    """Last successful quote per pair in Redis (shared by API + workers)."""

    name = "redis"

    def __init__(self, redis_url: str, client: Any = None) -> None:
        if client is None:
            import redis as _redis
            client = _redis.Redis.from_url(
                redis_url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2,
            )
        self._r = client

    def get_last_success(self, base: str, quote: str) -> Optional[RateQuote]:
        key = _KEY.format(base=base.upper(), quote=quote.upper())
        try:
            raw = self._r.get(key)
        except Exception as exc:
            log.warning("currency.rate_store_read_failed", key=key, exc_message=str(exc))
            return None
        if not raw:
            return None
        try:
            return _decode(raw if isinstance(raw, str) else raw.decode("utf-8"))
        except Exception as exc:
            log.warning("currency.rate_store_corrupt", key=key, exc_message=str(exc))
            return None

    def save_success(self, quote: RateQuote) -> None:
        key = _KEY.format(base=quote.base.upper(), quote=quote.quote.upper())
        try:
            self._r.set(key, _encode(quote))   # no TTL — see module docstring
        except Exception as exc:
            log.warning("currency.rate_store_write_failed", key=key, exc_message=str(exc))


def _encode(q: RateQuote) -> str:
    return json.dumps({
        "base": q.base, "quote": q.quote, "rate": q.rate,
        "rate_date": q.rate_date.isoformat(), "fetched_at": q.fetched_at.isoformat(),
        "provider": q.provider,
    })


def _decode(raw: str) -> RateQuote:
    d = json.loads(raw)
    rate = float(d["rate"])
    if not rate > 0:
        raise ValueError(f"non-positive stored rate {rate!r}")
    return RateQuote(
        base=str(d["base"]).upper(), quote=str(d["quote"]).upper(), rate=rate,
        rate_date=date.fromisoformat(d["rate_date"]),
        fetched_at=datetime.fromisoformat(d["fetched_at"]),
        provider=str(d.get("provider") or "unknown"),
    )
