"""
extraction/formats/adaptive.py
────────────────────────────────
Adaptive parser — handles ANY unknown SPIR format.

Algorithm per sheet:
  1. Find the header row (scan first 25 rows, score keyword matches)
  2. Map header cells to logical field names using FIELD_KEYWORDS
  3. Read every data row below the header
  4. Normalize to output schema

This parser is always tried LAST (lowest priority).
It works on any Excel layout regardless of format.
"""
from __future__ import annotations
import logging
import re
from typing import Any

from extraction.formats.base import BaseParser, clean_str, clean_num, _is_placeholder

log = logging.getLogger(__name__)

# Keywords that map header text → logical field name
_FIELD_KEYWORDS: dict[str, list[str]] = {
    "tag":           ["equipment tag", "equip tag", "tag no", "tag number",
                      "tag #", "equip't", "tag"],
    "description":   ["description of parts", "description of part",
                      "description", "desc of part", "part description"],
    "quantity":      ["total no. of identical", "identical parts fitted",
                      "no. of identical", "qty identical", "quantity",
                      "qty", "no of parts"],
    "unit_price":    ["unit price", "price per unit", "unit cost", "price"],
    "total_price":   ["total price", "total cost", "extended price"],
    "currency":      ["currency"],
    "part_number":   ["manufacturer part", "mfr part", "part number", "part no",
                      "part#", "vendor part", "supplier part"],
    "manufacturer":  ["manufacturer name", "manufacturer", "mfr name", "make"],
    "supplier":      ["supplier/ocm", "supplier name", "supplier",
                      "ocm name", "vendor"],
    "uom":           ["unit of measure", "uom", "unit"],
    "delivery_weeks":["delivery time", "lead time", "delivery"],
    "sap_number":    ["sap number", "sap no", "sap #", "material number"],
    "classification":["classification", "class of part", "spare type"],
}

# Row-level footers that indicate we've passed the data section
_FOOTER_STARTS = (
    "project", "company", "engineering by", "reminder", "technical data",
    "note:", "notes:", "end of", "signature", "requisition",
)


def _score_row(ws, row_idx: int, max_col: int) -> int:
    """Score a row for how many SPIR header keywords it contains."""
    score = 0
    all_keywords = [kw for kws in _FIELD_KEYWORDS.values() for kw in kws]
    for c in range(1, max_col + 1):
        v = ws.cell(row_idx, c).value
        if v is None:
            continue
        cell = str(v).lower().strip()
        for kw in all_keywords:
            if kw in cell:
                score += 1
                break
    return score


def _find_header_row(ws, scan: int = 25) -> int | None:
    """Return 1-based index of the most keyword-rich row in the first `scan` rows."""
    max_col   = min(ws.max_column or 50, 50)
    best, row = 0, None
    for r in range(1, min(scan + 1, (ws.max_row or 0) + 1)):
        s = _score_row(ws, r, max_col)
        if s > best:
            best, row = s, r
    return row if best >= 2 else None


def _map_headers(ws, header_row: int) -> dict[str, int]:
    """Return {field_name: col_index} for the detected header row."""
    mapping: dict[str, int] = {}
    max_col = min(ws.max_column or 50, 80)
    for c in range(1, max_col + 1):
        v = ws.cell(header_row, c).value
        if v is None:
            continue
        cell = str(v).lower().strip()
        for field, keywords in _FIELD_KEYWORDS.items():
            if field in mapping:
                continue
            for kw in keywords:
                if kw in cell:
                    mapping[field] = c
                    break
    return mapping


def _is_footer(desc: str) -> bool:
    dl = (desc or "").lower().strip()
    return any(dl.startswith(f) for f in _FOOTER_STARTS)


def _extract_sheet(ws, sheet_name: str) -> list[dict]:
    """Extract all data rows from a single sheet."""
    rows: list[dict] = []

    header_row = _find_header_row(ws)
    if header_row is None:
        log.debug("Adaptive: no header found in sheet '%s'", sheet_name)
        return rows

    col_map = _map_headers(ws, header_row)
    if not col_map:
        log.debug("Adaptive: no column mapping in sheet '%s'", sheet_name)
        return rows

    for r in range(header_row + 1, (ws.max_row or 0) + 1):
        # Read description first — use as stop-row signal
        desc_col = col_map.get("description")
        desc     = clean_str(ws.cell(r, desc_col).value) if desc_col else None

        # Skip blank rows
        row_vals = [ws.cell(r, c).value for c in range(1, min(ws.max_column + 1, 30))]
        if all(v is None or str(v).strip() == "" for v in row_vals):
            continue

        # Stop at footer rows
        if desc and _is_footer(desc):
            break

        item: dict[str, Any] = {"sheet": sheet_name}
        for field, col in col_map.items():
            raw = ws.cell(r, col).value
            if field in ("quantity", "unit_price", "total_price", "delivery_weeks"):
                item[field] = clean_num(raw)
            else:
                item[field] = clean_str(raw)

        # Skip rows with no description and no part number
        if not item.get("description") and not item.get("part_number"):
            continue

        rows.append(item)

    return rows


class AdaptiveParser(BaseParser):
    """
    Adaptive parser — handles any SPIR format.
    Always returns True from detect() — it is the final fallback.
    """
    FORMAT_NAME = "FORMAT_ADAPTIVE"

    @classmethod
    def detect(cls, wb) -> bool:
        # Always matches — used as fallback only
        return True

    @classmethod
    def _extract_raw(cls, wb) -> list[dict]:
        all_rows: list[dict] = []

        for sheet_name in wb.sheetnames:
            name_lower = sheet_name.lower()
            # Skip obvious non-data sheets
            if any(kw in name_lower for kw in
                   ("validation", "lookup", "reference", "dropdown", "list",
                    "instructions", "cover", "summary")):
                continue
            try:
                ws = wb[sheet_name]
                sheet_rows = _extract_sheet(ws, sheet_name)
                all_rows.extend(sheet_rows)
            except Exception as exc:
                log.warning("Adaptive: sheet '%s' failed: %s", sheet_name, exc)

        return all_rows
