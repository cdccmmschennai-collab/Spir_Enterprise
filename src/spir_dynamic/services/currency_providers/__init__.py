"""
Exchange-rate providers.

The rest of the application talks to ``CurrencyRateService``
(spir_dynamic.services.currency_service); only this package knows how a
concrete upstream API is shaped. Swap the provider here, not in callers.
"""
from __future__ import annotations

from spir_dynamic.services.currency_providers.base import (
    CurrencyRateError,
    RateProvider,
    RateProviderError,
    RateQuote,
    UnsupportedCurrencyError,
)
from spir_dynamic.services.currency_providers.frankfurter import FrankfurterProvider

__all__ = [
    "CurrencyRateError",
    "FrankfurterProvider",
    "RateProvider",
    "RateProviderError",
    "RateQuote",
    "UnsupportedCurrencyError",
]
