"""
extraction/output_schema.py
----------------------------
Single source of truth for output column structure.

HOW TO MODIFY COLUMNS:
  Add column    -> add a dict entry to OUTPUT_COLUMNS at the right position
  Remove column -> comment out its dict entry
  Rename column -> change its "col" value
  Reorder       -> move the dict entry up or down
  Change width  -> update "width"

No other file needs to change when you modify this file.
"""
from __future__ import annotations

OUTPUT_COLUMNS: list[dict] = [

    # -- Identity --
    {"col": "SPIR NO",    "field": "spir_no",   "default": None, "width": 24},
    {"col": "TAG NO",     "field": "tag_no",     "default": None, "width": 22},

    # -- Equipment metadata --
    {"col": "EQPT MAKE",  "field": "manufacturer","default": None, "width": 28},
    {"col": "EQPT MODEL", "field": "model",        "default": None, "width": 24},
    {"col": "EQPT SR NO", "field": "serial",       "default": None, "width": 16},
    {"col": "EQPT QTY",   "field": "eqpt_qty",     "default": None, "width": 10},

    # -- Item data --
    {"col": "QUANTITY IDENTICAL PARTS FITTED", "field": "qty_identical",
     "default": None, "width": 14},
    {"col": "ITEM NUMBER", "field": "item_num", "default": None, "width": 10},

    # -- Two computed columns (filled by post_processor) --
    {"col": "POSITION NUMBER",
     "field": "position_number", "default": None, "width": 14},
    {"col": "OLD MATERIAL NUMBER/SPF NUMBER",
     "field": "old_material_no", "default": None, "width": 22},

    # -- Part data --
    {"col": "DESCRIPTION OF PARTS",     "field": "desc",        "default": None, "width": 50},
    {"col": "NEW DESCRIPTION OF PARTS", "field": "new_desc",    "default": None, "width": 60},
    {"col": "DWG NO INCL POSN NO",      "field": "dwg_no",      "default": None, "width": 42},
    {"col": "MANUFACTURER PART NUMBER", "field": "mfr_part_no", "default": None, "width": 36},
    {"col": "MATERIAL SPECIFICATION",   "field": "material_spec","default": None, "width": 30},
    {"col": "SUPPLIER/ OCM NAME",       "field": "supplier_name","default": None, "width": 28},

    # -- Commercial --
    {"col": "CURRENCY",               "field": "currency",      "default": None, "width": 24},
    {"col": "UNIT PRICE",             "field": "unit_price",    "default": None, "width": 12},
    {"col": "UNIT PRICE (QAR)",       "field": "unit_price_qar","default": None, "width": 16},
    {"col": "DELIVERY TIME IN WEEKS", "field": "delivery",      "default": None, "width": 14},
    {"col": "MIN MAX STOCK LVLS QTY", "field": "min_max",       "default": None, "width": 14},
    {"col": "UNIT OF MEASURE",        "field": "uom",           "default": None, "width": 14},

    # -- SAP / Classification --
    {"col": "SAP NUMBER",              "field": "sap_no",        "default": None, "width": 16},
    {"col": "CLASSIFICATION OF PARTS", "field": "classification","default": None, "width": 20},

    # -- System columns --
    {"col": "SPIR ERROR", "field": "duplicate_id", "default": 0,    "width": 22},
    {"col": "SHEET",      "field": "sheet",         "default": None, "width": 22},
    {"col": "SPIR TYPE",  "field": "spir_type",     "default": None, "width": 26},
]


# -- Derived lookups (computed once, used everywhere) --

OUTPUT_COLS: list[str]      = [e["col"]   for e in OUTPUT_COLUMNS]
CI:          dict[str, int] = {e["col"]:  i for i, e in enumerate(OUTPUT_COLUMNS)}
COL_WIDTHS:  dict[str, int] = {e["col"]:  e["width"] for e in OUTPUT_COLUMNS}
FIELD_CI:    dict[str, int] = {e["field"]: i for i, e in enumerate(OUTPUT_COLUMNS)}


def make_empty_row() -> list:
    """Return a new OUTPUT_COLS-length list filled with schema defaults."""
    return [e["default"] for e in OUTPUT_COLUMNS]


def row_from_dict(item: dict) -> list:
    """
    Convert a field-name dict -> OUTPUT_COLS-ordered list.
    Supports both new field names (tag, description) and old column names.
    """
    row = make_empty_row()
    for entry in OUTPUT_COLUMNS:
        col_idx = CI[entry["col"]]
        val = item.get(entry["field"])
        if val is None:
            val = item.get(entry["col"])
        if val is not None:
            row[col_idx] = val
    return row
