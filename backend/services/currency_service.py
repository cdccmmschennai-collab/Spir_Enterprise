"""
services/currency_service.py
──────────────────────────────
Currency Conversion Service.

Converts all prices to INR (as required by spec).
Also supports QAR (for Qatar Energy context) and all major currencies.

CONFIGURATION:
  CONVERSION_ENABLED = False  → skip conversion, return original price
  BASE_CURRENCY               → target currency for conversion output
  EXCHANGE_RATES              → update rates here, no code changes elsewhere

SUPPORTED FORMATS:
  "USD"                       → "USD"
  "USD - United States Dollar" → "USD"
  "usd"                       → "USD"
  None / ""                   → ""

PUBLIC API:
  convert(unit_price, currency_raw, target="INR") → float | None
  convert_to_inr(unit_price, currency_raw)        → float | None
  convert_to_qar(unit_price, currency_raw)        → float | None
  parse_currency_code(raw)                        → str
  get_rate(code, target)                          → float | None
  conversion_summary()                            → dict
"""
from __future__ import annotations
import logging
import re

log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────
CONVERSION_ENABLED: bool = True
BASE_CURRENCY:      str  = "INR"    # primary output currency per spec

# All rates expressed as: 1 unit of KEY = N INR
# Source: approximate mid-market rates (update regularly in production)
EXCHANGE_RATES_TO_INR: dict[str, float] = {
    "INR":  1.000,     # base
    "USD":  83.50,     # US Dollar
    "EUR":  90.20,     # Euro
    "GBP": 105.80,     # British Pound
    "AED":  22.73,     # UAE Dirham
    "QAR":  22.93,     # Qatari Riyal
    "SAR":  22.27,     # Saudi Riyal
    "KWD": 271.50,     # Kuwaiti Dinar
    "BHD": 221.50,     # Bahraini Dinar
    "OMR": 216.90,     # Omani Rial
    "JPY":   0.56,     # Japanese Yen
    "CNY":  11.47,     # Chinese Yuan
    "CAD":  61.20,     # Canadian Dollar
    "AUD":  54.60,     # Australian Dollar
    "CHF":  93.80,     # Swiss Franc
    "SGD":  62.10,     # Singapore Dollar
    "MYR":  17.80,     # Malaysian Ringgit
    "JPY":   0.56,     # Japanese Yen
    "KRW":   0.063,    # South Korean Won
    "SEK":   7.90,     # Swedish Krona
    "NOK":   7.70,     # Norwegian Krone
    "CHF":  93.80,     # Swiss Franc
}

# Warn once per unknown currency
_warned: set[str] = set()


# ── Core helpers ───────────────────────────────────────────────────────────────

def parse_currency_code(raw: str | None) -> str:
    """
    Extract ISO currency code from raw cell value.

    "USD - United States Dollar" → "USD"
    "EUR"                        → "EUR"
    "usd"                        → "USD"
    None / ""                    → ""
    """
    if not raw:
        return ""
    s = str(raw).strip()
    m = re.match(r'^([A-Za-z]{2,4})', s)
    return m.group(1).upper() if m else s.upper()[:4]


def get_rate(from_code: str, to_code: str = "INR") -> float | None:
    """
    Return conversion rate: 1 unit of from_code = N to_code.

    Strategy: convert through INR as pivot currency.
    rate(USD→QAR) = rate(USD→INR) / rate(QAR→INR)
    """
    from_code = (from_code or "").upper()
    to_code   = (to_code   or "INR").upper()

    if from_code == to_code:
        return 1.0

    from_inr = EXCHANGE_RATES_TO_INR.get(from_code)
    if from_inr is None:
        return None

    if to_code == "INR":
        return from_inr

    to_inr = EXCHANGE_RATES_TO_INR.get(to_code)
    if to_inr is None:
        return None

    return round(from_inr / to_inr, 6)


def convert(
    unit_price:   float | int | None,
    currency_raw: str | None,
    target:       str = "INR",
) -> float | None:
    """
    Convert a unit price to the target currency.

    Args:
        unit_price:   Numeric price (None → returns None).
        currency_raw: Raw currency string from Excel cell.
        target:       Target currency code (default: INR per spec).

    Returns:
        Converted price (2 decimal places) or None if invalid.
    """
    if unit_price is None:
        return None

    try:
        price = float(unit_price)
    except (TypeError, ValueError):
        return None

    if not CONVERSION_ENABLED:
        return round(price, 2)

    code = parse_currency_code(currency_raw)
    if not code:
        return round(price, 2)   # assume already in target

    rate = get_rate(code, target)
    if rate is None:
        if code not in _warned:
            _warned.add(code)
            log.warning(
                "Currency '%s' not in EXCHANGE_RATES_TO_INR — "
                "returning original value. Add it to currency_service.py.",
                code,
            )
        return round(price, 2)

    return round(price * rate, 2)


def convert_to_inr(unit_price: float | int | None, currency_raw: str | None) -> float | None:
    """Convenience wrapper: convert to INR."""
    return convert(unit_price, currency_raw, target="INR")


def convert_to_qar(unit_price: float | int | None, currency_raw: str | None) -> float | None:
    """Convenience wrapper: convert to QAR (for Qatar Energy context)."""
    return convert(unit_price, currency_raw, target="QAR")


def conversion_summary() -> dict:
    """Return config summary for the /currencies endpoint."""
    return {
        "enabled":        CONVERSION_ENABLED,
        "base_currency":  BASE_CURRENCY,
        "output_note":    f"All prices converted to {BASE_CURRENCY}",
        "rates_to_inr":   dict(EXCHANGE_RATES_TO_INR),
        "currency_count": len(EXCHANGE_RATES_TO_INR),
    }
