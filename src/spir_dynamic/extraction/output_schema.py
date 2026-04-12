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

DynamicSchema builds an output schema from what was actually found in a specific
SPIR file. Canonical columns come first (in OUTPUT_COLUMNS order), then any
extra/unknown columns found in the source file are appended at the end.
"""
from __future__ import annotations

from dataclasses import dataclass, field

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
    {"col": "ERROR",      "field": "duplicate_id", "default": 0,    "width": 22},
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
        # PHASE 5 FIX: For supplier_name (SUPPLIER/ OCM NAME column),
        # prefer manufacturer over supplier as fallback.
        # In SPIR files, the OCM (Original Component Manufacturer) is the
        # manufacturer, not the supplier/distributor.
        if val is None and entry["field"] == "supplier_name":
            val = item.get("manufacturer")
        if val is None:
            val = item.get(entry["col"])
        if val is not None:
            row[col_idx] = val
    return row


# ---------------------------------------------------------------------------
# DynamicSchema — output schema built from what was found in a specific file
# ---------------------------------------------------------------------------

@dataclass
class DynamicSchema:
    """
    Output schema built from actual columns found in a SPIR file.

    Canonical OUTPUT_COLUMNS appear first (in their defined order), followed
    by any extra columns discovered in the source that aren't part of the
    standard 27-column schema.
    """
    columns: list[dict] = field(default_factory=list)
    col_names: list[str] = field(default_factory=list)
    ci: dict[str, int] = field(default_factory=dict)
    field_ci: dict[str, int] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        found_fields: dict[str, int],
        extra_cols: dict[int, str],
    ) -> "DynamicSchema":
        """
        Build schema:
        1. Walk canonical OUTPUT_COLUMNS in their defined order
        2. Append any extra_cols whose header text isn't already in a canonical column
        """
        columns: list[dict] = []
        col_names: list[str] = []
        ci: dict[str, int] = {}
        field_ci: dict[str, int] = {}

        # Canonical columns first
        for entry in OUTPUT_COLUMNS:
            idx = len(columns)
            columns.append(entry)
            col_names.append(entry["col"])
            ci[entry["col"]] = idx
            field_ci[entry["field"]] = idx

        # Extra discovered columns: only add if not already covered
        canonical_cols_lower = {c.lower() for c in col_names}
        for _ws_col, header_text in extra_cols.items():
            if not header_text:
                continue
            if header_text.lower() in canonical_cols_lower:
                continue
            idx = len(columns)
            extra_entry = {
                "col": header_text,
                "field": f"_extra_{idx}",
                "default": None,
                "width": 20,
            }
            columns.append(extra_entry)
            col_names.append(header_text)
            ci[header_text] = idx
            field_ci[extra_entry["field"]] = idx
            canonical_cols_lower.add(header_text.lower())

        schema = cls()
        schema.columns = columns
        schema.col_names = col_names
        schema.ci = ci
        schema.field_ci = field_ci
        return schema

    def make_empty_row(self) -> list:
        return [c.get("default") for c in self.columns]

    def row_from_dict(self, item: dict) -> list:
        row = self.make_empty_row()
        for entry in self.columns:
            field_name = entry["field"]
            col_name = entry["col"]
            idx = self.ci[col_name]
            val = item.get(field_name)
            if val is None and field_name == "supplier_name":
                val = item.get("manufacturer")
            if val is None:
                val = item.get(col_name)
            if val is not None:
                row[idx] = val
        return row

    @classmethod
    def from_standard(cls) -> "DynamicSchema":
        """Build a DynamicSchema from the standard OUTPUT_COLUMNS (no extras)."""
        return cls.build(found_fields={}, extra_cols={})
