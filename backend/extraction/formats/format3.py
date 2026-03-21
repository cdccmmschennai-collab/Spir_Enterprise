"""
extraction/formats/format3.py
──────────────────────────────
FORMAT3 — Single-sheet, multiple tag COLUMNS.

Structure:
  One data sheet. Tag numbers appear as column headers (one column per tag).
  Quantities per tag appear in each column.
  Description and part number are in dedicated columns.

Detection:
  Single data sheet with multiple tag-column headers (≥2 tag-like columns).
"""
from __future__ import annotations
import logging
import re
from extraction.formats.base import BaseParser, clean_str, clean_num

log = logging.getLogger(__name__)

_TAG_COL_RE = re.compile(r'^[A-Z0-9]{2,}[-/][A-Z0-9]', re.IGNORECASE)


class Format3Parser(BaseParser):
    FORMAT_NAME = "FORMAT3"

    @classmethod
    def detect(cls, wb) -> bool:
        data = [s for s in wb.sheetnames
                if not any(k in s.lower()
                           for k in ("validation", "lookup", "reference", "instructions"))]
        if len(data) != 1:
            return False
        ws      = wb[data[0]]
        tag_cols = cls._find_tag_columns(ws)
        return len(tag_cols) >= 2

    @classmethod
    def _find_tag_columns(cls, ws) -> dict[int, str]:
        """Return {col_index: tag_name} for columns that look like tag headers."""
        tag_cols: dict[int, str] = {}
        for r in range(1, min(12, ws.max_row + 1)):
            for c in range(1, min(ws.max_column + 1, 60)):
                v = ws.cell(r, c).value
                if v and _TAG_COL_RE.match(str(v).strip()):
                    tag_cols[c] = str(v).strip()
        return tag_cols

    @classmethod
    def _extract_raw(cls, wb) -> list[dict]:
        data = [s for s in wb.sheetnames
                if not any(k in s.lower() for k in ("validation", "lookup"))]
        if not data:
            return []

        ws         = wb[data[0]]
        sheet_name = data[0]
        tag_cols   = cls._find_tag_columns(ws)

        if not tag_cols:
            return []

        from extraction.formats.adaptive import _find_header_row, _map_headers, _is_footer
        hdr     = _find_header_row(ws)
        if hdr is None:
            return []
        col_map = _map_headers(ws, hdr)

        rows: list[dict] = []
        for r in range(hdr + 1, ws.max_row + 1):
            desc_col = col_map.get("description")
            desc     = clean_str(ws.cell(r, desc_col).value) if desc_col else None
            if desc and _is_footer(desc):
                break
            if all(ws.cell(r, c).value is None
                   for c in range(1, min(ws.max_column + 1, 20))):
                continue
            if not desc and not clean_str(ws.cell(r, col_map.get("part_number", 1)).value if col_map.get("part_number") else None):
                continue

            base = {
                "sheet":       sheet_name,
                "description": desc,
                "part_number": clean_str(ws.cell(r, col_map["part_number"]).value) if col_map.get("part_number") else None,
                "manufacturer":clean_str(ws.cell(r, col_map["manufacturer"]).value) if col_map.get("manufacturer") else None,
                "supplier":    clean_str(ws.cell(r, col_map["supplier"]).value) if col_map.get("supplier") else None,
                "uom":         clean_str(ws.cell(r, col_map["uom"]).value) if col_map.get("uom") else None,
                "sap_number":  clean_str(ws.cell(r, col_map["sap_number"]).value) if col_map.get("sap_number") else None,
            }

            # One row per tag column
            for col_idx, tag_name in tag_cols.items():
                qty = clean_num(ws.cell(r, col_idx).value)
                if qty is None:
                    continue
                row = dict(base)
                row["tag"]        = tag_name
                row["quantity"]   = qty
                row["unit_price"] = clean_num(ws.cell(r, col_map["unit_price"]).value) if col_map.get("unit_price") else None
                rows.append(row)

        return rows
