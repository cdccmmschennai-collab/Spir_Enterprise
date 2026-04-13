"""
Real-time currency conversion to QAR.

Fetches live rates from free public APIs, caches for 1 hour.
Falls back to hard-coded rates if all APIs are unreachable.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.error
from typing import Optional

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 3600
BASE_CURRENCY = "USD"

_RATE_APIS = [
    "https://open.er-api.com/v6/latest/{base}",
    "https://api.exchangerate-api.com/v4/latest/{base}",
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/{base_lower}.json",
]

FALLBACK_RATES: dict[str, float] = {
    "USD": 3.64, "EUR": 3.95, "GBP": 4.62, "AED": 0.991,
    "SAR": 0.970, "KWD": 11.86, "OMR": 9.45, "BHD": 9.65,
    "JPY": 0.0243, "CNY": 0.502, "INR": 0.0437, "SGD": 2.70,
    "AUD": 2.37, "CAD": 2.67, "CHF": 4.10, "SEK": 0.346,
    "NOK": 0.341, "DKK": 0.530, "QAR": 1.0,
}

_cache: dict[str, dict] = {}
_code_cache: dict[str, str] = {}   # memoize _extract_code; values are pure strings


def clear_cache() -> None:
    _cache.clear()
    _code_cache.clear()


def _fetch_rates(base: str) -> Optional[dict[str, float]]:
    base_lower = base.lower()
    for api_template in _RATE_APIS:
        url = api_template.format(base=base, base_lower=base_lower)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SPIR-Dynamic/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            rates = data.get("rates") or data.get(base_lower, {}) or {}
            if rates:
                return {k.upper(): float(v) for k, v in rates.items()}
        except Exception as exc:
            log.debug("API %s failed: %s", url.split("/")[2], exc)
    return None


def get_rates_to_qar() -> dict[str, float]:
    now = time.time()
    cached = _cache.get("USD_to_all")
    if cached and (now - cached["fetched_at"]) < CACHE_TTL_SECONDS:
        return cached["rates"]

    usd_rates = _fetch_rates("USD")
    if usd_rates:
        qar_per_usd = usd_rates.get("QAR", 3.64)
        to_qar: dict[str, float] = {}
        for code, per_usd in usd_rates.items():
            if per_usd and per_usd > 0:
                to_qar[code] = round(qar_per_usd / per_usd, 6)
        to_qar["QAR"] = 1.0
        _cache["USD_to_all"] = {"rates": to_qar, "fetched_at": now}
        return to_qar

    if cached:
        return cached["rates"]

    # FIX: cache the fallback so repeated calls (one per row) don't each
    # retry the network.  Without this, a failed fetch causes 477+ HTTP
    # round-trips for a single file — the dominant bottleneck.
    fallback = FALLBACK_RATES.copy()
    _cache["USD_to_all"] = {"rates": fallback, "fetched_at": now}
    return fallback


def to_qar(amount: float, currency_code: str) -> Optional[float]:
    if amount is None:
        return None
    code = _extract_code(currency_code)
    if not code:
        return None
    if code == "QAR":
        return round(float(amount), 2)
    rates = get_rates_to_qar()
    rate = rates.get(code)
    if rate is None:
        return None
    return round(float(amount) * rate, 2)


def _extract_code(raw: str) -> str:
    """Extract 3-letter ISO currency code from a raw cell value. Memoized."""
    cached_code = _code_cache.get(raw)
    if cached_code is not None:
        return cached_code
    if not raw:
        _code_cache[raw] = ""
        return ""
    s = str(raw).strip()
    if len(s) >= 3 and s[:3].isalpha():
        result = s[:3].upper()
    else:
        result = s.upper()
    _code_cache[raw] = result
    return result


def conversion_summary() -> dict:
    rates = get_rates_to_qar()
    return {
        "base": "QAR",
        "description": "Units of each currency equal to 1 QAR equivalent",
        "rates_to_qar": rates,
        "cache_age_seconds": int(
            time.time() - _cache.get("USD_to_all", {}).get("fetched_at", 0)
        ),
    }
