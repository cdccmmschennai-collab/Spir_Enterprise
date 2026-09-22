"""
Daily currency conversion to QAR.

    File Processor -> CurrencyRateService -> RateProvider (Frankfurter) -> API

The pipeline asks the service for ONE snapshot per processing job: every
currency the file needs is looked up once (``build_snapshot``), the result is
frozen, and every row of that job is converted from the frozen snapshot. The
provider is never called per row, and a rate that changes upstream while a
long job runs cannot leak into that job.

Fallback chain when the live API cannot answer (timeout, 5xx, unreachable,
bad response) — the user must never get a blank QAR column just because the
provider is down:

    1. live      — today's quote from the provider (per the in-process cache policy)
    2. cached    — the most recent successful quote, persisted in the shared
                   rate store (Redis) by an earlier job on any API/worker process
    3. fallback  — the project's original static rate table (last resort)
    4. no rate at all -> the job FAILS with an actionable error; an invented
                   rate would be wrong financial data.

Every snapshot entry records which source was used and, for 2/3, why the
live rate was not available. An *invalid* currency code (provider answers
422) is not an availability problem: it stays blank, as it always did.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

import structlog

from spir_dynamic.services.currency_providers import (
    CurrencyRateError,
    FrankfurterProvider,
    RateProvider,
    RateProviderError,
    RateQuote,
    UnsupportedCurrencyError,
)
from spir_dynamic.services.currency_rate_store import NullRateStore, RateStore

log = structlog.stdlib.get_logger(__name__)

# Business constant: every conversion in this application targets QAR.
TARGET_CURRENCY = "QAR"

# Last-resort rates (units of QAR per 1 unit of currency) — the table the
# previous implementation shipped with. Used ONLY when the live API and the
# persisted last-success store both have nothing, and always marked
# source="fallback" in the snapshot together with the reason. These values
# are not refreshed automatically; review them periodically.
STATIC_FALLBACK_RATES: dict[str, float] = {
    "USD": 3.64, "EUR": 3.95, "GBP": 4.62, "AED": 0.991,
    "SAR": 0.970, "KWD": 11.86, "OMR": 9.45, "BHD": 9.65,
    "JPY": 0.0243, "CNY": 0.502, "INR": 0.0437, "SGD": 2.70,
    "AUD": 2.37, "CAD": 2.67, "CHF": 4.10, "SEK": 0.346,
    "NOK": 0.341, "DKK": 0.530,
}
STATIC_FALLBACK_SOURCE = "static_table"

SOURCE_LIVE = "live"
SOURCE_CACHED = "cached"
SOURCE_FALLBACK = "fallback"


class RateUnavailableError(CurrencyRateError):
    """No rate from the live API, the persisted store or the static table."""


class CurrencyConversionError(CurrencyRateError):
    """A processing job cannot convert prices — raised to fail the job clearly."""

# Symbol / alias spellings seen in SPIR files -> ISO 4217. Only unambiguous
# ones; "Rs" (INR/PKR/LKR) is deliberately absent.
_CURRENCY_ALIASES: dict[str, str] = {
    "$": "USD", "US$": "USD", "USD$": "USD", "U.S.D": "USD",
    "€": "EUR", "EURO": "EUR", "EUROS": "EUR",
    "£": "GBP", "GBP£": "GBP",
    "¥": "JPY",
    "₹": "INR",
    "QR": "QAR", "QAR.": "QAR",
}

_CODE_RE = re.compile(r"^[A-Z]{3}$")
_code_cache: dict[str, Optional[str]] = {}


def normalize_currency_code(raw: object) -> Optional[str]:
    """
    Map a raw currency cell value to an ISO 4217 code, or None if unrecognised.

    Keeps the behaviour of the previous extractor (first three letters of an
    alphabetic prefix, e.g. "USD (FOB)" -> "USD") and adds symbol aliases the
    tabular strategy already used ("US$" -> "USD"). Memoised: a file repeats
    the same handful of values thousands of times.
    """
    if raw is None:
        return None
    key = str(raw)
    if key in _code_cache:
        return _code_cache[key]

    s = key.strip().upper()
    result: Optional[str] = None
    if s:
        if s in _CURRENCY_ALIASES:
            result = _CURRENCY_ALIASES[s]
        elif _CODE_RE.match(s):
            result = s
        elif len(s) >= 3 and s[:3].isalpha():
            # Legacy behaviour: the alphabetic prefix is tried as a code
            # ("USD (FOB)" -> "USD", "EURO" -> "EUR"); the provider decides
            # whether it exists ("DOLLARS" -> "DOL" -> unsupported).
            result = s[:3]
    _code_cache[key] = result
    return result


def clear_cache() -> None:
    """Reset memoised codes and the process-wide rate cache (tests / admin)."""
    _code_cache.clear()
    svc = _service_singleton.get()
    if svc is not None:
        svc.clear_cache()


# ── Snapshot ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SnapshotEntry:
    source_currency: str
    target_currency: str
    exchange_rate: Optional[float]      # None when status != "ok"
    rate_date: Optional[str]            # ISO date the rate applies to (None for the static table)
    fetched_at: Optional[str]           # ISO timestamp (UTC) the rate was originally fetched
    provider: str                       # who produced the rate: "frankfurter" | "static_table"
    status: str                         # "ok" | "unsupported" | "unavailable"
    error: Optional[str] = None
    source: Optional[str] = None        # "live" | "cached" | "fallback" (when status == "ok")
    fallback_reason: Optional[str] = None   # why the live rate was not used
    used_at: Optional[str] = None       # ISO timestamp (UTC) this job resolved the rate

    def to_dict(self) -> dict:
        d = {
            "source_currency": self.source_currency,
            "target_currency": self.target_currency,
            "exchange_rate": self.exchange_rate,
            "rate_date": self.rate_date,
            "fetched_at": self.fetched_at,
            "used_at": self.used_at,
            "provider": self.provider,
            "source": self.source,
            "status": self.status,
        }
        if self.fallback_reason:
            d["fallback_reason"] = self.fallback_reason
        if self.error:
            d["error"] = self.error
        return d


@dataclass(frozen=True)
class RateSnapshot:
    """The frozen set of rates one processing job converts with."""

    target_currency: str
    provider: str
    created_at: str
    entries: tuple[SnapshotEntry, ...] = ()
    job_id: Optional[str] = None
    unrecognized: tuple[str, ...] = ()
    _rates: dict[str, float] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Populate the lookup map once; the dataclass is frozen so use object.__setattr__.
        rates = {e.source_currency: e.exchange_rate for e in self.entries if e.status == "ok" and e.exchange_rate}
        object.__setattr__(self, "_rates", rates)

    def rate_for(self, source_currency: str) -> Optional[float]:
        if source_currency == self.target_currency:
            return 1.0
        return self._rates.get(source_currency)

    @property
    def ok_count(self) -> int:
        return sum(1 for e in self.entries if e.status == "ok")

    @property
    def failed(self) -> list[SnapshotEntry]:
        return [e for e in self.entries if e.status != "ok"]

    @property
    def unavailable(self) -> list[SnapshotEntry]:
        """Currencies with no rate anywhere — the job cannot convert these."""
        return [e for e in self.entries if e.status == "unavailable"]

    @property
    def fallback_used(self) -> bool:
        return any(e.status == "ok" and e.source != SOURCE_LIVE for e in self.entries)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "target_currency": self.target_currency,
            "provider": self.provider,
            "created_at": self.created_at,
            "fallback_used": self.fallback_used,
            "rates": [e.to_dict() for e in self.entries],
            "unrecognized": list(self.unrecognized),
        }


# ── Service ──────────────────────────────────────────────────────────────────

class CurrencyRateService:
    """
    Looks rates up through a provider and hands the pipeline frozen snapshots.

    A small in-process cache (TTL from config) means a 20-file batch does not
    ask the provider 20 times for the same USD->QAR daily rate; the cached
    quote keeps its original ``fetched_at`` and ``rate_date`` so the snapshot
    stays truthful. Nothing stale is ever served once the TTL has passed — a
    provider failure then surfaces as ``unavailable`` rather than an old rate.
    """

    def __init__(
        self,
        provider: RateProvider,
        cache_ttl_seconds: int = 3600,
        target_currency: str = TARGET_CURRENCY,
        rate_store: Optional[RateStore] = None,
        static_fallback_rates: Optional[dict[str, float]] = None,
    ) -> None:
        """
        Args:
            provider:   live rate source (Frankfurter).
            rate_store: persisted last-success store shared across processes
                        (Redis in the app); None = no persisted fallback.
            static_fallback_rates: last-resort table; None = STATIC_FALLBACK_RATES,
                        {} = no static fallback.
        """
        self.provider = provider
        self.cache_ttl_seconds = max(0, int(cache_ttl_seconds))
        self.target_currency = target_currency.upper()
        self.rate_store: RateStore = rate_store if rate_store is not None else NullRateStore()
        self.static_fallback_rates: dict[str, float] = (
            dict(STATIC_FALLBACK_RATES) if static_fallback_rates is None else dict(static_fallback_rates)
        )
        # value: (monotonic stored_at, RateQuote | UnsupportedCurrencyError)
        self._cache: dict[tuple[str, str], tuple[float, "RateQuote | UnsupportedCurrencyError"]] = {}
        self._lock = threading.Lock()

    # ── single-rate API ───────────────────────────────────────────────────

    def get_rate(self, source_currency: str, target_currency: Optional[str] = None) -> RateQuote:
        """Return the daily quote for source->target (cached). Raises CurrencyRateError."""
        source = source_currency.strip().upper()
        target = (target_currency or self.target_currency).strip().upper()
        key = (source, target)

        cached = self._cache_get(key)
        if isinstance(cached, UnsupportedCurrencyError):
            raise cached
        if cached is not None:
            log.info(
                "currency.rate_retrieved",
                currency=source, target=target,
                rate=cached.rate, rate_date=cached.rate_date.isoformat(),
                provider=cached.provider, cached=True,
            )
            return cached

        log.info("currency.rate_lookup_started", currency=source, target=target, provider=self.provider.name)
        try:
            quote = self.provider.get_rate(source, target)
        except UnsupportedCurrencyError as exc:
            # A code the provider does not know stays unknown all day — remember
            # the answer so a batch does not repeat the same failing request.
            self._cache_put(key, exc)
            raise
        self._cache_put(key, quote)
        # Every live success refreshes the persisted last-success store so a
        # later job can convert even if the provider is down by then.
        self.rate_store.save_success(quote)
        log.info(
            "currency.rate_retrieved",
            currency=source, target=target,
            rate=quote.rate, rate_date=quote.rate_date.isoformat(),
            provider=quote.provider, cached=False,
        )
        return quote

    # ── fallback chain ────────────────────────────────────────────────────

    def resolve_rate(self, source_currency: str, target_currency: Optional[str] = None) -> SnapshotEntry:
        """
        Resolve one rate through the chain: live -> persisted cache -> static table.

        Returns a status="ok" entry tagged with its source. Raises
        UnsupportedCurrencyError when the provider says the code is invalid
        (not an availability problem) and RateUnavailableError when nothing
        in the chain can supply a rate.
        """
        source = source_currency.strip().upper()
        target = (target_currency or self.target_currency).strip().upper()
        now = _iso(datetime.now(timezone.utc))

        try:
            q = self.get_rate(source, target)
        except UnsupportedCurrencyError:
            raise
        except CurrencyRateError as exc:
            live_error = str(exc)
        else:
            return SnapshotEntry(
                source_currency=source, target_currency=target,
                exchange_rate=q.rate, rate_date=q.rate_date.isoformat(),
                fetched_at=_iso(q.fetched_at), used_at=now, provider=q.provider,
                status="ok", source=SOURCE_LIVE,
            )

        log.error("currency.rate_live_failed", currency=source, target=target, exc_message=live_error)

        # 2. most recent successful quote persisted by any earlier job
        cached = self.rate_store.get_last_success(source, target)
        if cached is not None:
            log.warning(
                "currency.rate_fallback_cached",
                currency=source, target=target, rate=cached.rate,
                rate_date=cached.rate_date.isoformat(), fetched_at=_iso(cached.fetched_at),
                store=getattr(self.rate_store, "name", "store"), reason=live_error,
            )
            return SnapshotEntry(
                source_currency=source, target_currency=target,
                exchange_rate=cached.rate, rate_date=cached.rate_date.isoformat(),
                fetched_at=_iso(cached.fetched_at), used_at=now, provider=cached.provider,
                status="ok", source=SOURCE_CACHED,
                fallback_reason=f"live rate unavailable ({live_error}); using last successful rate",
            )

        # 3. static last-resort table
        static = self.static_fallback_rates.get(source) if target == TARGET_CURRENCY else None
        if static:
            log.warning(
                "currency.rate_fallback_static",
                currency=source, target=target, rate=static, reason=live_error,
            )
            return SnapshotEntry(
                source_currency=source, target_currency=target,
                exchange_rate=static, rate_date=None, fetched_at=None, used_at=now,
                provider=STATIC_FALLBACK_SOURCE, status="ok", source=SOURCE_FALLBACK,
                fallback_reason=(
                    f"live rate unavailable ({live_error}); no previously successful rate "
                    f"in the rate store; using static fallback table"
                ),
            )

        raise RateUnavailableError(
            f"{source}->{target}: live rate unavailable ({live_error}); "
            f"no previously successful rate in the rate store; no static fallback rate"
        )

    # ── per-job snapshot API ──────────────────────────────────────────────

    def build_snapshot(
        self,
        currencies: Iterable[str],
        job_id: Optional[str] = None,
        unrecognized: Iterable[str] = (),
    ) -> RateSnapshot:
        """
        Resolve each required currency ONCE (through the fallback chain) and
        freeze the result.

        ``currencies`` are ISO codes already normalised by the caller; the
        target currency itself is skipped (no conversion needed). Nothing
        raises here: an invalid code is recorded as ``unsupported`` (cell stays
        blank, as before) and a code with no rate anywhere as ``unavailable``
        — the pipeline turns the latter into a job failure.
        """
        target = self.target_currency
        wanted = sorted({c.strip().upper() for c in currencies if c} - {target})
        entries: list[SnapshotEntry] = []

        for code in wanted:
            try:
                entries.append(self.resolve_rate(code, target))
            except UnsupportedCurrencyError as exc:
                log.warning("currency.rate_unsupported", currency=code, target=target, exc_message=str(exc))
                entries.append(SnapshotEntry(
                    source_currency=code, target_currency=target,
                    exchange_rate=None, rate_date=None, fetched_at=None,
                    used_at=_iso(datetime.now(timezone.utc)),
                    provider=self.provider.name, status="unsupported", error=str(exc),
                ))
            except CurrencyRateError as exc:
                log.error("currency.rate_unavailable", currency=code, target=target, exc_message=str(exc))
                entries.append(SnapshotEntry(
                    source_currency=code, target_currency=target,
                    exchange_rate=None, rate_date=None, fetched_at=None,
                    used_at=_iso(datetime.now(timezone.utc)),
                    provider=self.provider.name, status="unavailable", error=str(exc),
                ))

        snapshot = RateSnapshot(
            target_currency=target,
            provider=self.provider.name,
            created_at=_iso(datetime.now(timezone.utc)),
            entries=tuple(entries),
            job_id=job_id,
            unrecognized=tuple(sorted({str(u) for u in unrecognized if u})),
        )
        log.info(
            "currency.snapshot_built",
            job_id=job_id, target=target, provider=snapshot.provider,
            currencies=wanted, ok=snapshot.ok_count,
            sources={e.source_currency: e.source for e in snapshot.entries if e.status == "ok"},
            fallback_used=snapshot.fallback_used,
            failed=[e.source_currency for e in snapshot.failed],
        )
        return snapshot

    # ── cache ─────────────────────────────────────────────────────────────

    def _cache_get(self, key: tuple[str, str]) -> "RateQuote | UnsupportedCurrencyError | None":
        if self.cache_ttl_seconds <= 0:
            return None
        with self._lock:
            hit = self._cache.get(key)
            if hit is None:
                return None
            stored_at, value = hit
            if time.monotonic() - stored_at >= self.cache_ttl_seconds:
                del self._cache[key]
                return None
            return value

    def _cache_put(self, key: tuple[str, str], value: "RateQuote | UnsupportedCurrencyError") -> None:
        if self.cache_ttl_seconds <= 0:
            return
        with self._lock:
            self._cache[key] = (time.monotonic(), value)

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def cached_quotes(self) -> list[dict]:
        with self._lock:
            items = list(self._cache.items())
        now = time.monotonic()
        return [
            {
                "source_currency": k[0], "target_currency": k[1],
                "exchange_rate": q.rate, "rate_date": q.rate_date.isoformat(),
                "fetched_at": _iso(q.fetched_at), "provider": q.provider,
                "cache_age_seconds": int(now - t),
            }
            for k, (t, q) in items
            if isinstance(q, RateQuote)
        ]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# ── Process-wide instance ────────────────────────────────────────────────────

class _Singleton:
    def __init__(self) -> None:
        self._svc: Optional[CurrencyRateService] = None
        self._lock = threading.Lock()

    def get(self) -> Optional[CurrencyRateService]:
        return self._svc

    def get_or_create(self) -> CurrencyRateService:
        with self._lock:
            if self._svc is None:
                from spir_dynamic.app.config import get_settings
                cfg = get_settings()
                from spir_dynamic.services.currency_rate_store import RedisRateStore
                provider = FrankfurterProvider(
                    base_url=cfg.currency_api_base_url,
                    timeout_seconds=cfg.currency_api_timeout_seconds,
                )
                # Shared, persisted last-success store: the same Redis the job
                # store / result cache already use, so API and workers agree.
                store = RedisRateStore(cfg.redis_url)
                self._svc = CurrencyRateService(
                    provider=provider,
                    cache_ttl_seconds=cfg.currency_rate_cache_ttl_seconds,
                    target_currency=TARGET_CURRENCY,
                    rate_store=store,
                )
                log.info(
                    "currency.service_initialised",
                    provider=provider.name, base_url=provider.base_url,
                    target=TARGET_CURRENCY, cache_ttl_s=cfg.currency_rate_cache_ttl_seconds,
                    rate_store=store.name, static_fallback_currencies=len(STATIC_FALLBACK_RATES),
                )
            return self._svc

    def reset(self, svc: Optional[CurrencyRateService] = None) -> None:
        with self._lock:
            self._svc = svc


_service_singleton = _Singleton()


def get_currency_rate_service() -> CurrencyRateService:
    """The application's rate service (built from settings on first use)."""
    return _service_singleton.get_or_create()


def set_currency_rate_service(svc: Optional[CurrencyRateService]) -> None:
    """Replace the process-wide service (tests). None restores settings-based construction."""
    _service_singleton.reset(svc)


def conversion_summary() -> dict:
    """Description of the conversion setup for GET /api/currencies (no file data)."""
    svc = get_currency_rate_service()
    return {
        "target_currency": svc.target_currency,
        "provider": svc.provider.name,
        "provider_base_url": getattr(svc.provider, "base_url", None),
        "description": "Daily rates fetched once per processing job and frozen for that job",
        "fallback_chain": [SOURCE_LIVE, SOURCE_CACHED, SOURCE_FALLBACK],
        "rate_store": getattr(svc.rate_store, "name", "none"),
        "static_fallback_currencies": sorted(svc.static_fallback_rates),
        "cache_ttl_seconds": svc.cache_ttl_seconds,
        "cached_rates": svc.cached_quotes(),
    }


__all__ = [
    "STATIC_FALLBACK_RATES",
    "SOURCE_CACHED",
    "SOURCE_FALLBACK",
    "SOURCE_LIVE",
    "TARGET_CURRENCY",
    "CurrencyConversionError",
    "CurrencyRateError",
    "CurrencyRateService",
    "RateUnavailableError",
    "RateProviderError",
    "RateQuote",
    "RateSnapshot",
    "SnapshotEntry",
    "UnsupportedCurrencyError",
    "clear_cache",
    "conversion_summary",
    "get_currency_rate_service",
    "normalize_currency_code",
    "set_currency_rate_service",
]
