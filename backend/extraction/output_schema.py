"""
extraction/output_schema.py
────────────────────────────
Single source of truth for the normalized output schema.

Every format parser must map its data to these columns.
Add/remove/rename columns here ONLY — nothing else changes.

NORMALIZED OUTPUT_COLS (as required by spec):
  tag, description, quantity, unit_price, total_price
  Plus enrichment columns for production use.
"""
from __future__ import annotations

# ── Column definitions ────────────────────────────────────────────────────────
OUTPUT_COLUMNS: list[dict] = [
    # Core required columns (spec)
    {"col": "tag",             "field": "tag",             "default": None, "width": 22},
    {"col": "description",     "field": "description",     "default": None, "width": 50},
    {"col": "quantity",        "field": "quantity",        "default": None, "width": 12},
    {"col": "unit_price",      "field": "unit_price",      "default": None, "width": 14},
    {"col": "total_price",     "field": "total_price",     "default": None, "width": 14},

    # Enrichment columns
    {"col": "currency",        "field": "currency",        "default": None, "width": 12},
    {"col": "unit_price_inr",  "field": "unit_price_inr",  "default": None, "width": 16},
    {"col": "part_number",     "field": "part_number",     "default": None, "width": 30},
    {"col": "manufacturer",    "field": "manufacturer",    "default": None, "width": 28},
    {"col": "supplier",        "field": "supplier",        "default": None, "width": 28},
    {"col": "uom",             "field": "uom",             "default": None, "width": 10},
    {"col": "delivery_weeks",  "field": "delivery_weeks",  "default": None, "width": 14},
    {"col": "sap_number",      "field": "sap_number",      "default": None, "width": 16},
    {"col": "classification",  "field": "classification",  "default": None, "width": 20},
    {"col": "sheet",           "field": "sheet",           "default": None, "width": 22},
    {"col": "format_source",   "field": "format_source",   "default": None, "width": 16},
    {"col": "duplicate_flag",  "field": "duplicate_flag",  "default": "",   "width": 22},
]

# Derived lookups
OUTPUT_COLS: list[str]      = [e["col"]   for e in OUTPUT_COLUMNS]
CI:          dict[str, int] = {e["col"]:  i for i, e in enumerate(OUTPUT_COLUMNS)}
COL_WIDTHS:  dict[str, int] = {e["col"]:  e["width"] for e in OUTPUT_COLUMNS}
FIELD_CI:    dict[str, int] = {e["field"]: i for i, e in enumerate(OUTPUT_COLUMNS)}


def make_empty_row() -> list:
    """Return a fresh row filled with schema defaults."""
    return [e["default"] for e in OUTPUT_COLUMNS]


def row_from_dict(item: dict) -> list:
    """Convert a field-name dict to an OUTPUT_COLS-ordered list."""
    row = make_empty_row()
    for entry in OUTPUT_COLUMNS:
        val = item.get(entry["field"], entry["default"])
        row[CI[entry["col"]]] = val
    return row
