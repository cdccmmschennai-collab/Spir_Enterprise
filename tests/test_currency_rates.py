"""
Tests for daily currency conversion to QAR (services/currency_service.py,
services/currency_providers/frankfurter.py, pipeline._apply_currency_conversion).

The provider is faked (or urllib's opener is mocked) — no network access.
"""
from __future__ import annotations

import io
import json
import socket
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import spir_dynamic.services.currency_service as currency_service
from spir_dynamic.app.pipeline import _apply_currency_conversion
from spir_dynamic.db.database import Base
from spir_dynamic.db.models import ExtractionHistory
from spir_dynamic.extraction.output_schema import CI, make_empty_row
from spir_dynamic.services.audit_service import log_extraction_worker
from spir_dynamic.services.currency_providers import (
    FrankfurterProvider,
    RateProviderError,
    RateQuote,
    UnsupportedCurrencyError,
)
from spir_dynamic.services.currency_rate_store import InMemoryRateStore, RedisRateStore
from spir_dynamic.services.currency_service import (
    STATIC_FALLBACK_RATES,
    TARGET_CURRENCY,
    CurrencyConversionError,
    CurrencyRateService,
    RateUnavailableError,
    normalize_currency_code,
    set_currency_rate_service,
)

CUR = CI["CURRENCY"]
PRICE = CI["UNIT PRICE"]
QAR = CI["UNIT PRICE (QAR)"]


# ── helpers ───────────────────────────────────────────────────────────────────

class FakeProvider:
    """In-memory provider: a rate table plus a call log."""

    name = "fake"

    def __init__(self, rates: dict[str, float], rate_date: date = date(2026, 9, 22)):
        self.rates = dict(rates)
        self.rate_date = rate_date
        self.calls: list[tuple[str, str]] = []
        self.fail_with: Optional[Exception] = None

    def get_rate(self, base: str, quote: str, rate_date: Optional[date] = None) -> RateQuote:
        self.calls.append((base, quote))
        if self.fail_with is not None:
            raise self.fail_with
        if base not in self.rates:
            raise UnsupportedCurrencyError(base, quote, "HTTP 422")
        return RateQuote(
            base=base, quote=quote, rate=self.rates[base], rate_date=self.rate_date,
            fetched_at=datetime.now(timezone.utc), provider=self.name,
        )


def make_rows(*pairs: tuple[Optional[str], Optional[float]]) -> list[list]:
    rows = []
    for cur, price in pairs:
        r = make_empty_row()
        r[CUR] = cur
        r[PRICE] = price
        rows.append(r)
    return rows


def make_service(provider, ttl: int = 3600, store=None, static: Optional[dict] = None) -> CurrencyRateService:
    """Default: no persisted store and NO static table, so tests opt in to each fallback explicitly."""
    return CurrencyRateService(
        provider=provider, cache_ttl_seconds=ttl, target_currency=TARGET_CURRENCY,
        rate_store=store, static_fallback_rates={} if static is None else static,
    )


def result_for(file_id: str, snapshot) -> dict:
    """Minimal run_pipeline-shaped result dict for the history writers."""
    return {
        "filename": "x_Extraction.xlsx", "file_id": file_id, "spir_no": "S-1",
        "total_rows": 1, "total_tags": 1, "spare_items": 1,
        "currency_rates": snapshot.to_dict() if snapshot else None,
    }


@pytest.fixture(autouse=True)
def _isolate_service():
    currency_service.clear_cache()
    set_currency_rate_service(None)
    yield
    currency_service.clear_cache()
    set_currency_rate_service(None)


@pytest.fixture
def sqlite_settings(tmp_path: Path):
    """Point log_extraction_worker at a throwaway SQLite database with the real schema."""
    url = f"sqlite:///{tmp_path / 'history.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    cfg = SimpleNamespace(database_url=url)
    with patch("spir_dynamic.app.config.get_settings", return_value=cfg):
        yield url


def read_history(url: str) -> list[ExtractionHistory]:
    engine = create_engine(url)
    with sessionmaker(bind=engine)() as s:
        rows = s.execute(select(ExtractionHistory).order_by(ExtractionHistory.created_at)).scalars().all()
        s.expunge_all()
    engine.dispose()
    return rows


# ── Test 1: USD conversion uses the retrieved rate ─────────────────────────────

def test_usd_conversion_uses_retrieved_rate():
    provider = FakeProvider({"USD": 3.64})
    rows = make_rows(("USD", 100), ("USD", 1.5))

    snap = _apply_currency_conversion(rows, job_id="job-1", service=make_service(provider))

    assert rows[0][QAR] == 364.0
    assert rows[1][QAR] == 5.46
    assert provider.calls == [("USD", "QAR")]
    entry = snap.entries[0]
    assert (entry.source_currency, entry.target_currency, entry.exchange_rate) == ("USD", "QAR", 3.64)
    assert entry.status == "ok" and entry.rate_date == "2026-09-22" and entry.provider == "fake"


# ── Test 2: multiple currencies → only the required lookups ────────────────────

def test_multiple_currencies_fetch_each_required_rate_once():
    provider = FakeProvider({"USD": 3.64, "EUR": 4.18, "GBP": 4.8732, "JPY": 0.02313})
    rows = make_rows(
        ("USD", 10), ("USD", 20), ("USD", 30),
        ("EUR", 10), ("EUR", 20),
        ("QAR", 100),
        ("GBP", 10),
        ("JPY", None),          # no price -> no conversion -> no lookup
    )

    snap = _apply_currency_conversion(rows, job_id="job-2", service=make_service(provider))

    assert sorted(provider.calls) == [("EUR", "QAR"), ("GBP", "QAR"), ("USD", "QAR")]
    assert len(provider.calls) == 3          # 7 priced rows, 3 lookups
    assert [r[QAR] for r in rows] == [36.4, 72.8, 109.2, 41.8, 83.6, 100.0, 48.73, None]
    assert {e.source_currency for e in snap.entries} == {"USD", "EUR", "GBP"}


# ── Test 3: QAR input is not converted ────────────────────────────────────────

def test_qar_input_is_not_converted_and_needs_no_lookup():
    provider = FakeProvider({"USD": 3.64})
    rows = make_rows(("QAR", 100), ("qar", 12.345))

    snap = _apply_currency_conversion(rows, job_id="job-3", service=make_service(provider))

    assert rows[0][QAR] == 100.0
    assert rows[1][QAR] == 12.35            # existing 2-dp rounding policy kept
    assert provider.calls == []
    assert snap is not None and snap.entries == ()


def test_no_priced_rows_means_no_snapshot_and_no_lookup():
    provider = FakeProvider({"USD": 3.64})
    rows = make_rows(("USD", None), (None, 5))
    assert _apply_currency_conversion(rows, service=make_service(provider)) is None
    assert provider.calls == []


# ── Test 4: snapshot is stored against the processing job ─────────────────────

def test_rate_snapshot_is_persisted_with_the_job(sqlite_settings):
    provider = FakeProvider({"USD": 3.64})
    rows = make_rows(("USD", 100))
    snap = _apply_currency_conversion(rows, job_id="file-abc", service=make_service(provider))

    log_extraction_worker(user_id="u1", result=result_for("file-abc", snap), original_filename="a.xlsx")

    (rec,) = read_history(sqlite_settings)
    assert rec.file_id == "file-abc"
    assert rec.currency_rates["job_id"] == "file-abc"
    assert rec.currency_rates["provider"] == "fake"
    assert rec.currency_rates["target_currency"] == "QAR"
    (entry,) = rec.currency_rates["rates"]
    assert entry["source_currency"] == "USD"
    assert entry["exchange_rate"] == 3.64
    assert entry["rate_date"] == "2026-09-22"
    assert entry["fetched_at"]
    assert entry["provider"] == "fake"
    assert entry["status"] == "ok"


# ── Test 5: the rate is frozen for the whole job ──────────────────────────────

class DriftingProvider(FakeProvider):
    """Upstream rate changes after every answer — simulates a mid-job update."""

    def get_rate(self, base, quote, rate_date=None):
        q = super().get_rate(base, quote, rate_date)
        self.rates[base] = round(self.rates[base] + 1.0, 4)
        return q


def test_rate_is_frozen_for_the_job_even_if_upstream_changes():
    provider = DriftingProvider({"USD": 3.64})
    svc = make_service(provider, ttl=0)          # no cache: every lookup would hit the provider
    rows = make_rows(*[("USD", 100)] * 500)

    snap = _apply_currency_conversion(rows, job_id="job-5", service=svc)

    assert provider.calls == [("USD", "QAR")]    # exactly one lookup for 500 rows
    assert {r[QAR] for r in rows} == {364.0}     # every row used the original 3.64
    assert provider.rates["USD"] == 4.64         # upstream moved on...
    assert snap.rate_for("USD") == 3.64          # ...but the snapshot did not


def test_snapshot_object_is_immutable():
    snap = make_service(FakeProvider({"USD": 3.64})).build_snapshot(["USD"], job_id="j")
    with pytest.raises(Exception):
        snap.entries = ()                        # frozen dataclass


# ── Test 6: API failure → fallback chain, never a silent blank ────────────────

def test_api_failure_with_no_fallback_anywhere_fails_the_job():
    """Case 5: nothing live, nothing cached, no static entry -> clear job failure, no blanks."""
    provider = FakeProvider({"USD": 3.64})
    provider.fail_with = RateProviderError("Rate provider unreachable: timed out")
    rows = make_rows(("USD", 100), ("QAR", 5))
    svc = make_service(provider, store=None, static={})     # chain deliberately empty

    with pytest.raises(CurrencyConversionError) as exc:
        _apply_currency_conversion(rows, job_id="job-6", service=svc)

    msg = str(exc.value)
    assert "USD -> QAR" in msg and "unreachable" in msg and "Retry" in msg
    assert rows[0][QAR] is None      # nothing was written — the job is failed, not half-converted


def test_static_table_is_never_used_while_the_api_answers():
    provider = FakeProvider({"EUR": 4.18})
    rows = make_rows(("EUR", 100))
    snap = _apply_currency_conversion(rows, service=make_service(provider, static={"EUR": 3.95}))
    assert rows[0][QAR] == 418.0 and snap.entries[0].source == "live"
    assert not hasattr(currency_service, "get_rates_to_qar")   # old per-row path stays gone


# ── Test 7: invalid / unsupported currency ────────────────────────────────────

def test_unsupported_currency_is_recorded_and_others_still_convert():
    provider = FakeProvider({"USD": 3.64})
    rows = make_rows(("XXX", 3), ("USD", 2), ("DOLLARS", 1), ("###", 9))

    snap = _apply_currency_conversion(rows, job_id="job-7", service=make_service(provider))

    assert rows[0][QAR] is None
    assert rows[1][QAR] == 7.28
    assert rows[2][QAR] is None
    assert rows[3][QAR] is None
    by_code = {e.source_currency: e for e in snap.entries}
    assert by_code["XXX"].status == "unsupported" and by_code["XXX"].exchange_rate is None
    assert by_code["DOL"].status == "unsupported"           # legacy 3-letter prefix, rejected by provider
    assert by_code["USD"].status == "ok"
    assert snap.unrecognized == ("###",)                    # not even a code — never sent upstream


def test_unsupported_answer_is_cached_for_a_batch():
    provider = FakeProvider({"USD": 3.64})
    svc = make_service(provider)
    for _ in range(3):
        svc.build_snapshot(["XXX", "USD"])
    assert provider.calls.count(("XXX", "QAR")) == 1
    assert provider.calls.count(("USD", "QAR")) == 1


# ── Test 8: historical reproducibility ────────────────────────────────────────

def test_completed_jobs_keep_the_rate_they_actually_used(sqlite_settings):
    provider = FakeProvider({"USD": 3.64}, rate_date=date(2026, 9, 22))
    svc = make_service(provider, ttl=0)

    rows_a = make_rows(("USD", 100))
    snap_a = _apply_currency_conversion(rows_a, job_id="file-A", service=svc)
    log_extraction_worker(user_id="u1", result=result_for("file-A", snap_a), original_filename="A.xlsx")

    # Days later the daily rate is different.
    provider.rates["USD"] = 3.70
    provider.rate_date = date(2026, 9, 25)
    rows_b = make_rows(("USD", 100))
    snap_b = _apply_currency_conversion(rows_b, job_id="file-B", service=svc)
    log_extraction_worker(user_id="u1", result=result_for("file-B", snap_b), original_filename="B.xlsx")

    assert rows_a[0][QAR] == 364.0 and rows_b[0][QAR] == 370.0
    rec_a, rec_b = sorted(read_history(sqlite_settings), key=lambda r: r.file_id)
    assert rec_a.currency_rates["rates"][0]["exchange_rate"] == 3.64
    assert rec_a.currency_rates["rates"][0]["rate_date"] == "2026-09-22"
    assert rec_b.currency_rates["rates"][0]["exchange_rate"] == 3.70
    assert rec_b.currency_rates["rates"][0]["rate_date"] == "2026-09-25"


# ── Fallback chain: live -> persisted last-success -> static table -> fail ────

def _seed_store(base="USD", rate=3.64, rate_date=date(2026, 9, 19)) -> InMemoryRateStore:
    store = InMemoryRateStore()
    store.save_success(RateQuote(base=base, quote="QAR", rate=rate, rate_date=rate_date,
                                 fetched_at=datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc), provider="frankfurter"))
    return store


def test_case1_api_available_uses_live_rate_and_refreshes_store():
    provider = FakeProvider({"USD": 3.64})
    store = InMemoryRateStore()
    rows = make_rows(("USD", 1957))

    snap = _apply_currency_conversion(rows, service=make_service(provider, store=store))

    assert rows[0][QAR] == 7123.48
    e = snap.entries[0]
    assert e.source == "live" and e.provider == "fake" and e.fallback_reason is None and e.used_at
    assert store.get_last_success("USD", "QAR").rate == 3.64      # success persisted for later outages
    assert snap.fallback_used is False


@pytest.mark.parametrize(
    "failure",
    [RateProviderError("Rate provider unreachable: timed out"), RateProviderError("Rate provider returned HTTP 500")],
    ids=["timeout", "http-500"],
)
def test_case2_3_api_failure_uses_last_successful_persisted_rate(failure):
    provider = FakeProvider({"USD": 9.99})
    provider.fail_with = failure
    store = _seed_store(rate=3.64, rate_date=date(2026, 9, 19))
    rows = make_rows(("USD", 1957), ("USD", 10))

    snap = _apply_currency_conversion(rows, job_id="job-fb", service=make_service(provider, store=store, static={"USD": 1.0}))

    assert [r[QAR] for r in rows] == [7123.48, 36.4]          # converted, never blank
    e = snap.entries[0]
    assert e.status == "ok" and e.source == "cached" and e.exchange_rate == 3.64
    assert e.rate_date == "2026-09-19"                        # original date of the cached rate is kept
    assert e.fetched_at == "2026-09-19T08:00:00+00:00"
    assert e.provider == "frankfurter" and str(failure) in e.fallback_reason
    assert snap.fallback_used is True
    assert provider.calls == [("USD", "QAR")]                 # API was still tried first, once


def test_case4_no_cached_rate_uses_static_table_and_marks_fallback():
    provider = FakeProvider({"USD": 9.99})
    provider.fail_with = RateProviderError("Rate provider unreachable: connection refused")
    rows = make_rows(("USD", 1957))

    snap = _apply_currency_conversion(rows, service=make_service(provider, store=InMemoryRateStore(), static=STATIC_FALLBACK_RATES))

    assert rows[0][QAR] == round(1957 * STATIC_FALLBACK_RATES["USD"], 2) == 7123.48
    e = snap.entries[0]
    assert e.status == "ok" and e.source == "fallback" and e.provider == "static_table"
    assert e.rate_date is None and "static fallback" in e.fallback_reason and "connection refused" in e.fallback_reason
    assert snap.to_dict()["fallback_used"] is True
    assert snap.to_dict()["rates"][0]["source"] == "fallback"


def test_persisted_store_is_preferred_over_static_table():
    provider = FakeProvider({"USD": 9.99})
    provider.fail_with = RateProviderError("down")
    svc = make_service(provider, store=_seed_store(rate=3.70), static={"USD": 3.64})
    e = svc.resolve_rate("USD")
    assert (e.source, e.exchange_rate) == ("cached", 3.70)


def test_mixed_snapshot_marks_each_currency_source():
    class FlakyProvider(FakeProvider):
        def get_rate(self, base, quote, rate_date=None):
            if base == "EUR":
                self.calls.append((base, quote))
                raise RateProviderError("HTTP 503")
            return super().get_rate(base, quote, rate_date)

    provider = FlakyProvider({"USD": 3.64, "GBP": 4.87})
    store = _seed_store(base="EUR", rate=4.10)
    rows = make_rows(("USD", 1), ("EUR", 1), ("GBP", 1), ("QAR", 1))

    snap = _apply_currency_conversion(rows, service=make_service(provider, store=store))

    assert [r[QAR] for r in rows] == [3.64, 4.1, 4.87, 1.0]
    assert {e.source_currency: e.source for e in snap.entries} == {"USD": "live", "EUR": "cached", "GBP": "live"}
    assert snap.fallback_used is True


def test_no_conversion_cell_is_blank_after_temporary_api_failure():
    """Same file, API down between two jobs: the second job still fills every cell."""
    provider = FakeProvider({"USD": 3.64, "EUR": 4.18})
    store = InMemoryRateStore()
    svc = make_service(provider, ttl=0, store=store)
    pairs = [("USD", 100), ("EUR", 50), ("USD", 7.25), ("QAR", 3)] * 25

    rows_ok = make_rows(*pairs)
    _apply_currency_conversion(rows_ok, service=svc)
    provider.fail_with = RateProviderError("Rate provider unreachable: timed out")
    rows_down = make_rows(*pairs)
    snap = _apply_currency_conversion(rows_down, service=svc)

    assert all(r[QAR] is not None for r in rows_down)
    assert [r[QAR] for r in rows_down] == [r[QAR] for r in rows_ok]
    assert {e.source for e in snap.entries} == {"cached"}


def test_invalid_code_still_stays_blank_when_api_is_up():
    """A 422 is not an outage: behaviour for junk codes is unchanged (blank, job succeeds)."""
    provider = FakeProvider({"USD": 3.64})
    rows = make_rows(("XXX", 3), ("USD", 1))
    snap = _apply_currency_conversion(rows, service=make_service(provider, store=_seed_store(), static=STATIC_FALLBACK_RATES))
    assert rows[0][QAR] is None and rows[1][QAR] == 3.64
    assert {e.source_currency: e.status for e in snap.entries} == {"XXX": "unsupported", "USD": "ok"}


def test_resolve_rate_raises_when_chain_is_exhausted():
    provider = FakeProvider({})
    provider.fail_with = RateProviderError("down")
    with pytest.raises(RateUnavailableError, match="no static fallback rate"):
        make_service(provider, store=InMemoryRateStore(), static={}).resolve_rate("USD")


def test_fallback_source_is_persisted_in_history(sqlite_settings):
    provider = FakeProvider({"USD": 9.99})
    provider.fail_with = RateProviderError("Rate provider returned HTTP 500")
    rows = make_rows(("USD", 1957))
    snap = _apply_currency_conversion(rows, job_id="file-fb", service=make_service(provider, store=_seed_store()))
    log_extraction_worker(user_id="u1", result=result_for("file-fb", snap), original_filename="fb.xlsx")

    (rec,) = read_history(sqlite_settings)
    stored = rec.currency_rates
    assert stored["fallback_used"] is True
    (entry,) = stored["rates"]
    assert entry["source"] == "cached" and entry["exchange_rate"] == 3.64
    assert "HTTP 500" in entry["fallback_reason"] and entry["rate_date"] == "2026-09-19"


# ── Redis-backed rate store (client stubbed) ──────────────────────────────────

class _StubRedis:
    def __init__(self, fail=False):
        self.d: dict[str, str] = {}
        self.fail = fail

    def get(self, k):
        if self.fail:
            raise ConnectionError("redis down")
        return self.d.get(k)

    def set(self, k, v):
        if self.fail:
            raise ConnectionError("redis down")
        self.d[k] = v


def test_redis_rate_store_round_trip():
    stub = _StubRedis()
    store = RedisRateStore("redis://unused", client=stub)
    q = RateQuote(base="USD", quote="QAR", rate=3.64, rate_date=date(2026, 9, 22),
                  fetched_at=datetime(2026, 9, 22, 5, 0, tzinfo=timezone.utc), provider="frankfurter")
    store.save_success(q)
    assert "spir:currency:last_rate:USD:QAR" in stub.d
    back = store.get_last_success("usd", "qar")
    assert back == q
    assert store.get_last_success("EUR", "QAR") is None


def test_redis_rate_store_never_raises():
    store = RedisRateStore("redis://unused", client=_StubRedis(fail=True))
    q = RateQuote(base="USD", quote="QAR", rate=3.64, rate_date=date(2026, 9, 22),
                  fetched_at=datetime.now(timezone.utc), provider="frankfurter")
    store.save_success(q)                                    # logged, swallowed
    assert store.get_last_success("USD", "QAR") is None
    corrupt = _StubRedis()
    corrupt.d["spir:currency:last_rate:USD:QAR"] = '{"rate": -1}'
    assert RedisRateStore("redis://unused", client=corrupt).get_last_success("USD", "QAR") is None


# ── Service cache ─────────────────────────────────────────────────────────────

def test_cache_reuses_daily_quote_within_ttl_and_keeps_fetch_metadata():
    provider = FakeProvider({"USD": 3.64})
    svc = make_service(provider, ttl=3600)
    q1 = svc.get_rate("USD")
    q2 = svc.get_rate("usd", "qar")
    assert q1 is q2
    assert provider.calls == [("USD", "QAR")]


def test_cache_disabled_with_ttl_zero():
    provider = FakeProvider({"USD": 3.64})
    svc = make_service(provider, ttl=0)
    svc.get_rate("USD")
    svc.get_rate("USD")
    assert len(provider.calls) == 2


def test_expired_cache_is_not_served_as_fallback_on_failure():
    provider = FakeProvider({"USD": 3.64})
    svc = make_service(provider, ttl=1)
    svc.get_rate("USD")
    far_future = 1e12   # well past any real monotonic reading + the 1 s TTL
    with patch("spir_dynamic.services.currency_service.time.monotonic", return_value=far_future):
        provider.fail_with = RateProviderError("down")
        with pytest.raises(RateProviderError):
            svc.get_rate("USD")


# ── Currency normalisation ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("USD", "USD"), ("usd ", "USD"), ("US$", "USD"), ("$", "USD"),
        ("€", "EUR"), ("EURO", "EUR"), ("EUR/UNIT", "EUR"), ("GBP (CIF)", "GBP"),
        ("£", "GBP"), ("₹", "INR"), ("QR", "QAR"), ("qar", "QAR"),
        ("DOLLARS", "DOL"),          # legacy prefix behaviour preserved; provider rejects it
        ("Rs", None), ("###", None), ("12", None), ("", None), (None, None),
    ],
)
def test_normalize_currency_code(raw, expected):
    assert normalize_currency_code(raw) == expected


# ── Frankfurter provider (urllib opener mocked) ───────────────────────────────

class _Resp(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def _opener_returning(body: dict | bytes, status: int = 200, calls: list | None = None):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()

    def opener(req, timeout=None):
        if calls is not None:
            calls.append((req.full_url, timeout))
        return _Resp(raw, status)

    return opener


def _opener_raising(exc_factory, calls: list):
    def opener(req, timeout=None):
        calls.append(req.full_url)
        raise exc_factory()
    return opener


def test_frankfurter_parses_v2_rate_response_and_builds_url():
    calls: list = []
    body = {"date": "2026-09-22", "base": "USD", "quote": "QAR", "rate": 3.64}
    p = FrankfurterProvider(base_url="https://api.frankfurter.dev/", timeout_seconds=7,
                            opener=_opener_returning(body, calls=calls))
    q = p.get_rate("usd", "qar")
    assert calls == [("https://api.frankfurter.dev/v2/rate/usd/qar", 7.0)]
    assert (q.base, q.quote, q.rate, q.rate_date, q.provider) == ("USD", "QAR", 3.64, date(2026, 9, 22), "frankfurter")
    assert q.fetched_at.tzinfo is not None


def test_frankfurter_historical_date_query():
    calls: list = []
    body = {"date": "2026-09-20", "base": "USD", "quote": "QAR", "rate": 3.64}
    p = FrankfurterProvider(opener=_opener_returning(body, calls=calls))
    q = p.get_rate("USD", "QAR", rate_date=date(2026, 9, 20))
    assert calls[0][0].endswith("/v2/rate/usd/qar?date=2026-09-20")
    assert q.rate_date == date(2026, 9, 20)


def test_frankfurter_422_is_unsupported_currency_and_not_retried():
    calls: list = []
    p = FrankfurterProvider(attempts=3, retry_backoff_seconds=0,
                            opener=_opener_raising(lambda: urllib.error.HTTPError("u", 422, "Unprocessable", {}, None), calls))
    with pytest.raises(UnsupportedCurrencyError):
        p.get_rate("XXX", "QAR")
    assert len(calls) == 1


def test_frankfurter_timeout_is_provider_error_after_retries():
    calls: list = []
    p = FrankfurterProvider(attempts=2, retry_backoff_seconds=0,
                            opener=_opener_raising(lambda: socket.timeout("timed out"), calls))
    with pytest.raises(RateProviderError, match="unreachable"):
        p.get_rate("USD", "QAR")
    assert len(calls) == 2


def test_frankfurter_5xx_retried_then_provider_error():
    calls: list = []
    p = FrankfurterProvider(attempts=2, retry_backoff_seconds=0,
                            opener=_opener_raising(lambda: urllib.error.HTTPError("u", 503, "Unavailable", {}, None), calls))
    with pytest.raises(RateProviderError, match="HTTP 503"):
        p.get_rate("USD", "QAR")
    assert len(calls) == 2


@pytest.mark.parametrize(
    "body",
    [
        b"<html>not json</html>",
        {"date": "2026-09-22", "base": "USD", "quote": "QAR"},                 # missing rate
        {"date": "2026-09-22", "base": "USD", "quote": "QAR", "rate": 0},      # non-positive
        {"date": "2026-09-22", "base": "USD", "quote": "QAR", "rate": "abc"},  # not numeric
        {"date": "nope", "base": "USD", "quote": "QAR", "rate": 3.64},         # bad date
        {"date": "2026-09-22", "base": "EUR", "quote": "QAR", "rate": 4.18},   # wrong pair
        [1, 2, 3],
    ],
)
def test_frankfurter_invalid_response_is_provider_error(body):
    p = FrankfurterProvider(attempts=1, opener=_opener_returning(body))
    with pytest.raises(RateProviderError):
        p.get_rate("USD", "QAR")


def test_frankfurter_rejects_non_alphabetic_codes_without_network():
    calls: list = []
    p = FrankfurterProvider(opener=_opener_raising(lambda: AssertionError("must not be called"), calls))
    with pytest.raises(UnsupportedCurrencyError):
        p.get_rate("US1", "QAR")
    assert calls == []
