"""
services/currency_service.py
──────────────────────────────
Real-time currency conversion to QAR.

HOW IT WORKS:
  - Fetches live rates from a free public API on first use
  - Caches the rates for CACHE_TTL_SECONDS (1 hour by default)
  - On network failure, falls back to the last known rates
  - Falls back to hard-coded rates if never fetched successfully

TO UPDATE DEFAULT RATES:
  Edit FALLBACK_RATES below. These are used only when all APIs are unreachable.

RATE UPDATE:
  Rates are fetched fresh each time the cache expires.
  No manual update needed — the system self-refreshes every hour automatically.
  If you want to force a refresh: call currency_service.clear_cache()
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.error
from typing import Optional

log = logging.getLogger(__name__)

# Cache TTL: refresh rates every 1 hour
CACHE_TTL_SECONDS = 3600

# Base currency for all conversions (output is always QAR)
BASE_CURRENCY = "USD"

# Free APIs tried in order — first one to respond wins
_RATE_APIS = [
    "https://open.er-api.com/v6/latest/{base}",
    "https://api.exchangerate-api.com/v4/latest/{base}",
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/{base_lower}.json",
]

# Fallback rates (used only when all APIs fail)
# QAR is pegged to USD: 1 USD ≈ 3.64 QAR (fixed peg since 1980)
# Other rates as of 2024 — will be overridden by live fetch
FALLBACK_RATES: dict[str, float] = {
    "USD": 3.64,
    "EUR": 3.95,
    "GBP": 4.62,
    "AED": 0.991,
    "SAR": 0.970,
    "KWD": 11.86,
    "OMR": 9.45,
    "BHD": 9.65,
    "JPY": 0.0243,
    "CNY": 0.502,
    "INR": 0.0437,
    "SGD": 2.70,
    "AUD": 2.37,
    "CAD": 2.67,
    "CHF": 4.10,
    "SEK": 0.346,
    "NOK": 0.341,
    "DKK": 0.530,
    "QAR": 1.0,
}

# Module-level cache
_cache: dict[str, dict] = {}   # base_currency → {rates, fetched_at}


def clear_cache() -> None:
    """Force next conversion to re-fetch rates."""
    _cache.clear()
    log.info("Currency cache cleared")


def _fetch_rates(base: str) -> Optional[dict[str, float]]:
    """
    Try each API in order. Return {currency_code: rate_vs_base} or None.
    """
    base_lower = base.lower()

    for api_template in _RATE_APIS:
        url = api_template.format(base=base, base_lower=base_lower)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SPIR-Tool/2.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())

            # Each API has a different response shape — normalise
            rates = (
                data.get("rates")                           # er-api.com / exchangerate-api
                or data.get(base_lower, {})                 # fawazahmed0 format
                or {}
            )
            if rates:
                log.info("Currency rates fetched from %s", url.split("/")[2])
                return {k.upper(): float(v) for k, v in rates.items()}

        except Exception as exc:
            log.debug("API %s failed: %s", url.split("/")[2], exc)

    return None


def get_rates_to_qar() -> dict[str, float]:
    """
    Return a dict mapping every currency code → QAR equivalent per 1 unit.
    Uses cache; refreshes every CACHE_TTL_SECONDS.
    """
    now = time.time()
    cached = _cache.get("USD_to_all")
    if cached and (now - cached["fetched_at"]) < CACHE_TTL_SECONDS:
        return cached["rates"]

    # Fetch USD-based rates (most reliable)
    usd_rates = _fetch_rates("USD")

    if usd_rates:
        # usd_rates = {currency: how_many_X_per_1_USD}
        # We want {currency: how_many_QAR_per_1_unit_of_currency}
        # QAR per 1 USD = usd_rates["QAR"]
        qar_per_usd = usd_rates.get("QAR", 3.64)

        # rate of X to QAR = (1/usd_rates[X]) * qar_per_usd
        # i.e. 1 EUR = (1/usd_rates["EUR"]) USD = (1/usd_rates["EUR"]) * qar_per_usd QAR
        to_qar: dict[str, float] = {}
        for code, per_usd in usd_rates.items():
            if per_usd and per_usd > 0:
                to_qar[code] = round(qar_per_usd / per_usd, 6)
        to_qar["QAR"] = 1.0

        _cache["USD_to_all"] = {"rates": to_qar, "fetched_at": now}
        log.info("Rates updated: USD→QAR = %.4f", qar_per_usd)
        return to_qar

    # Network unavailable — use last cached or fallback
    if cached:
        log.warning("API fetch failed — using cached rates from %.0f min ago",
                    (now - cached["fetched_at"]) / 60)
        return cached["rates"]

    log.warning("API fetch failed — using hard-coded fallback rates")
    return FALLBACK_RATES.copy()


def to_qar(amount: float, currency_code: str) -> Optional[float]:
    """
    Convert amount in currency_code to QAR.
    Returns None if amount is None or currency unknown.

    Examples:
        to_qar(80, "USD")  → 291.2  (at rate 3.64)
        to_qar(100, "EUR") → ~395    (at live rate)
        to_qar(50, "QAR")  → 50.0
    """
    if amount is None:
        return None

    # Normalise: "USD - United States Dollar" → "USD"
    code = _extract_code(currency_code)
    if not code:
        return None

    if code == "QAR":
        return round(float(amount), 2)

    rates = get_rates_to_qar()
    rate  = rates.get(code)

    if rate is None:
        log.warning("Unknown currency code: %r — no QAR conversion", code)
        return None

    return round(float(amount) * rate, 2)


def _extract_code(raw: str) -> str:
    """
    Extract 3-letter ISO code from various input formats.
    'USD - United States Dollar' → 'USD'
    'USD'                        → 'USD'
    'usd'                        → 'USD'
    """
    if not raw:
        return ""
    raw = str(raw).strip()
    # First 3 chars if they are all alpha
    if len(raw) >= 3 and raw[:3].isalpha():
        return raw[:3].upper()
    return raw.upper()


def conversion_summary() -> dict:
    """Return current rates for the /currencies API endpoint."""
    rates = get_rates_to_qar()
    return {
        "base":         "QAR",
        "description":  "Units of each currency equal to 1 QAR equivalent",
        "rates_to_qar": rates,
        "cache_age_seconds": int(time.time() - _cache.get("USD_to_all", {}).get("fetched_at", 0)),
    }