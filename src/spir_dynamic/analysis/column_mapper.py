"""
Dynamic column mapping — maps header cells to logical field names.

Scans the header row and matches cell text against keyword lists to determine
which column contains which data field. Handles merged cells and multi-row headers.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Field keywords: logical field name -> list of header text patterns
# ---------------------------------------------------------------------------
FIELD_KEYWORDS: dict[str, list[str]] = {
    "tag": [
        "equipment tag", "equip tag", "tag no", "tag number",
        "tag #", "equip't", "tag",
    ],
    "description": [
        "description of parts", "description of part",
        "description", "desc of part", "part description",
    ],
    "quantity": [
        "total no. of identical", "identical parts fitted",
        "no. of identical", "qty identical", "quantity",
        "qty", "no of parts",
    ],
    "item_number": [
        "item number", "item no", "item #", "s/n", "sl no",
    ],
    "unit_price": [
        "unit price", "price per unit", "unit cost", "price",
    ],
    "total_price": [
        "total price", "total cost", "extended price",
    ],
    "currency": [
        "currency",
    ],
    "part_number": [
        "manufacturer part", "mfr part", "part number", "part no",
        "part#", "vendor part", "supplier part",
    ],
    "manufacturer": [
        "manufacturer name", "manufacturer", "mfr name", "make",
    ],
    "supplier": [
        "supplier/ocm", "supplier ocm", "supplier name",
        "ocm name", "vendor",
    ],
    "uom": [
        "unit of measure", "uom",
    ],
    "delivery_weeks": [
        "delivery time", "lead time", "delivery",
    ],
    "sap_number": [
        "sap number", "sap no", "sap #", "material number",
    ],
    "classification": [
        "classification", "class of part", "spare type",
    ],
    "dwg_no": [
        "drawing no", "dwg no", "drawing number",
    ],
    "material_spec": [
        "material spec", "material specification",
    ],
    "model": [
        "model", "eqpt model",
    ],
    "serial": [
        "serial", "sr no", "serial no",
    ],
    "eqpt_qty": [
        "eqpt qty", "equipment qty", "no of eqpt",
    ],
    "min_max": [
        "min max", "min/max", "stock level",
    ],
}


def map_headers(ws, header_row: int) -> dict[str, int]:
    """
    Map header row cells to logical field names.

    Returns {field_name: 1-based_column_index} for all recognized columns.
    """
    mapping: dict[str, int] = {}
    max_col = min(ws.max_column or 50, 80)

    for c in range(1, max_col + 1):
        cell_text = _get_header_text(ws, header_row, c)
        if not cell_text:
            continue

        # Normalize whitespace: "UNIT  OF MEASURE" → "unit of measure"
        import re as _re
        cell_lower = _re.sub(r"\s+", " ", cell_text.lower().strip())

        for field, keywords in FIELD_KEYWORDS.items():
            if field in mapping:
                continue
            for kw in keywords:
                if kw in cell_lower:
                    mapping[field] = c
                    break

    # Try the row above for any unmapped fields (multi-row headers)
    if header_row > 1:
        _try_secondary_row(ws, header_row - 1, max_col, mapping)

    if mapping:
        log.debug(
            "Column mapping for '%s' row %d: %s",
            ws.title, header_row,
            {k: v for k, v in sorted(mapping.items(), key=lambda x: x[1])},
        )

    return mapping


def _get_header_text(ws, row: int, col: int) -> str | None:
    """
    Get header text from a cell, handling merged cells.
    For merged cells, returns the value from the top-left cell of the merge range.
    Safe for read-only worksheets (which don't have merged_cells).
    """
    cell = ws.cell(row, col)
    if cell.value is not None:
        return str(cell.value).strip()

    # Check if this cell is part of a merged range (not available in read-only mode)
    try:
        for merge_range in ws.merged_cells.ranges:
            if cell.coordinate in merge_range:
                top_left = ws.cell(merge_range.min_row, merge_range.min_col)
                if top_left.value is not None:
                    return str(top_left.value).strip()
                break
    except AttributeError:
        pass  # read-only worksheets don't have merged_cells

    return None


def _try_secondary_row(
    ws, row: int, max_col: int, existing: dict[str, int]
) -> None:
    """Try to map unmapped fields from a secondary header row."""
    for c in range(1, max_col + 1):
        cell_text = _get_header_text(ws, row, c)
        if not cell_text:
            continue

        cell_lower = cell_text.lower().strip()

        for field, keywords in FIELD_KEYWORDS.items():
            if field in existing:
                continue
            for kw in keywords:
                if kw in cell_lower:
                    existing[field] = c
                    break


def get_unmapped_columns(ws, header_row: int, mapped: dict[str, int]) -> dict[int, str]:
    """
    Return columns that weren't mapped to any known field.
    Useful for detecting extra tag columns or custom data fields.

    Returns {1-based_column_index: header_text}.
    """
    max_col = min(ws.max_column or 50, 80)
    mapped_cols = set(mapped.values())
    unmapped: dict[int, str] = {}

    for c in range(1, max_col + 1):
        if c in mapped_cols:
            continue
        text = _get_header_text(ws, header_row, c)
        if text:
            unmapped[c] = text

    return unmapped
