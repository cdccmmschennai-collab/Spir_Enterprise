"""
Provider contract for daily exchange rates.

A provider answers one question: "what is the rate from *base* to *quote*
(optionally on a given date)?" and reports the date the rate applies to.
Everything else — caching, per-job snapshots, which currencies to look up —
lives in the service layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Protocol, runtime_checkable


class CurrencyRateError(Exception):
    """Base class for every exchange-rate failure."""


class UnsupportedCurrencyError(CurrencyRateError):
    """The provider does not quote this currency pair (invalid / unknown code)."""

    def __init__(self, base: str, quote: str, detail: str = "") -> None:
        self.base = base
        self.quote = quote
        msg = f"Currency pair {base}->{quote} is not supported by the rate provider"
        if detail:
            msg = f"{msg}: {detail}"
        super().__init__(msg)


class RateProviderError(CurrencyRateError):
    """The provider could not be reached or returned an unusable response."""


@dataclass(frozen=True)
class RateQuote:
    """One exchange rate as retrieved from a provider.

    ``rate`` is units of ``quote`` per 1 unit of ``base`` (e.g. USD->QAR 3.64
    means 1 USD = 3.64 QAR). ``rate_date`` is the day the provider says the
    rate applies to; ``fetched_at`` is when we actually retrieved it.
    """

    base: str
    quote: str
    rate: float
    rate_date: date
    fetched_at: datetime
    provider: str


@runtime_checkable
class RateProvider(Protocol):
    name: str

    def get_rate(self, base: str, quote: str, rate_date: Optional[date] = None) -> RateQuote:
        """Return the rate for base->quote. Raise UnsupportedCurrencyError or RateProviderError."""
        ...
