"""
Frankfurter exchange-rate provider (https://frankfurter.dev).

Endpoint used (verified 2026-09-22 against the live service):

    GET {base_url}/v2/rate/{base}/{quote}[?date=YYYY-MM-DD]
    -> 200 {"date": "2026-09-22", "base": "USD", "quote": "QAR", "rate": 3.64}
    -> 422 (empty body) for an unknown currency code

Frankfurter is free, needs no API key, and publishes daily (not intraday)
rates. Only this module knows the URL layout and response shape; callers go
through CurrencyRateService.

HTTP client: stdlib urllib, the convention the previous currency module
already used — no second HTTP library is introduced.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

import structlog

from spir_dynamic.services.currency_providers.base import (
    RateProviderError,
    RateQuote,
    UnsupportedCurrencyError,
)

log = structlog.stdlib.get_logger(__name__)

DEFAULT_BASE_URL = "https://api.frankfurter.dev"
_USER_AGENT = "SPIR-Dynamic/1.0"
_MAX_RESPONSE_BYTES = 64 * 1024   # a rate payload is < 200 bytes; anything larger is not ours

# HTTP statuses Frankfurter uses for "this currency / date does not exist".
_UNSUPPORTED_STATUSES = (404, 422)


class FrankfurterProvider:
    name = "frankfurter"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 10.0,
        attempts: int = 2,
        retry_backoff_seconds: float = 0.5,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        """
        Args:
            base_url:  API root, e.g. https://api.frankfurter.dev (no trailing slash needed).
            timeout_seconds: socket timeout for connect and each read — the request can
                never hang a worker indefinitely.
            attempts:  total tries for transient failures (network / 5xx). A 4xx
                "unsupported currency" answer is never retried.
            opener:    urllib.request.urlopen-compatible callable (injected in tests).
        """
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.attempts = max(1, int(attempts))
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self._opener = opener or urllib.request.urlopen

    # ── public ───────────────────────────────────────────────────────────────

    def get_rate(self, base: str, quote: str, rate_date: Optional[date] = None) -> RateQuote:
        base_u = base.strip().upper()
        quote_u = quote.strip().upper()
        if not (base_u.isalpha() and quote_u.isalpha()):
            raise UnsupportedCurrencyError(base_u, quote_u, "currency codes must be alphabetic")

        url = self._rate_url(base_u, quote_u, rate_date)
        payload = self._get_json(url, base_u, quote_u)
        return self._parse(payload, base_u, quote_u)

    # ── internals ────────────────────────────────────────────────────────────

    def _rate_url(self, base: str, quote: str, rate_date: Optional[date]) -> str:
        url = f"{self.base_url}/v2/rate/{base.lower()}/{quote.lower()}"
        if rate_date is not None:
            url += "?" + urllib.parse.urlencode({"date": rate_date.isoformat()})
        return url

    def _get_json(self, url: str, base: str, quote: str) -> Any:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.attempts + 1):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"}
                )
                with self._opener(req, timeout=self.timeout_seconds) as resp:
                    status = getattr(resp, "status", None) or resp.getcode()
                    raw = resp.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise RateProviderError(f"Rate provider response too large ({len(raw)} bytes)")
                if status != 200:
                    raise RateProviderError(f"Rate provider returned HTTP {status}")
                try:
                    return json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise RateProviderError(f"Rate provider returned invalid JSON: {exc}") from exc

            except urllib.error.HTTPError as exc:
                if exc.code in _UNSUPPORTED_STATUSES:
                    raise UnsupportedCurrencyError(base, quote, f"HTTP {exc.code}") from exc
                last_error = RateProviderError(f"Rate provider returned HTTP {exc.code}")
                retryable = exc.code >= 500
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
                reason = getattr(exc, "reason", None) or exc
                last_error = RateProviderError(f"Rate provider unreachable: {reason}")
                retryable = True
            except RateProviderError as exc:
                last_error = exc
                retryable = False

            log.warning(
                "currency.provider_attempt_failed",
                provider=self.name,
                currency=base,
                target=quote,
                attempt=attempt,
                attempts=self.attempts,
                retryable=retryable,
                exc_message=str(last_error),
            )
            if not retryable or attempt >= self.attempts:
                break
            time.sleep(self.retry_backoff_seconds)

        assert last_error is not None
        raise last_error

    def _parse(self, payload: Any, base: str, quote: str) -> RateQuote:
        if not isinstance(payload, dict):
            raise RateProviderError("Rate provider response is not a JSON object")
        try:
            resp_base = str(payload["base"]).upper()
            resp_quote = str(payload["quote"]).upper()
            rate = float(payload["rate"])
            rate_date = date.fromisoformat(str(payload["date"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RateProviderError(f"Rate provider response missing/invalid field: {exc}") from exc

        if resp_base != base or resp_quote != quote:
            raise RateProviderError(
                f"Rate provider answered for {resp_base}->{resp_quote}, expected {base}->{quote}"
            )
        if not (rate > 0) or rate != rate or rate in (float("inf"), float("-inf")):
            raise RateProviderError(f"Rate provider returned a non-positive rate: {payload.get('rate')!r}")

        return RateQuote(
            base=base,
            quote=quote,
            rate=rate,
            rate_date=rate_date,
            fetched_at=datetime.now(timezone.utc),
            provider=self.name,
        )
