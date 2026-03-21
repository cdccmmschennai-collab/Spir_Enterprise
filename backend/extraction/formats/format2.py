"""
extraction/formats/format2.py
──────────────────────────────
FORMAT2 — Single-sheet, single tag column.

Structure:
  One sheet (typically named "MAIN SHEET" or "Sheet1").
  Tag value appears once in a header cell, applies to all rows below.
  Standard column layout: item | description | qty | part no | price

Detection:
  Exactly one data sheet AND tag column appears only once in header area.
"""
from __future__ import annotations
import logging
import re
from extraction.formats.base import BaseParser, clean_str, clean_num

log = logging.getLogger(__name__)


class Format2Parser(BaseParser):
    FORMAT_NAME = "FORMAT2"

    @classmethod
    def detect(cls, wb) -> bool:
        data_sheets = cls._data_sheets(wb)
        if len(data_sheets) != 1:
            return False
        # Check that the sheet has a single-tag structure
        ws = wb[data_sheets[0]]
        tag_count = cls._count_tag_cells_in_header(ws)
        return tag_count <= 1

    @classmethod
    def _data_sheets(cls, wb) -> list[str]:
        return [s for s in wb.sheetnames
                if not any(kw in s.lower() for kw in
                           ("validation", "lookup", "reference",
                            "instructions", "cover"))]

    @classmethod
    def _count_tag_cells_in_header(cls, ws) -> int:
        """Count how many distinct tag-like values appear in the first 8 rows."""
        count = 0
        tag_pattern = re.compile(r'\b[A-Z0-9]{2,}-[A-Z0-9]', re.IGNORECASE)
        for r in range(1, min(9, ws.max_row + 1)):
            for c in range(1, min(20, ws.max_column + 1)):
                v = ws.cell(r, c).value
                if v and tag_pattern.search(str(v)):
                    count += 1
        return count

    @classmethod
    def _extract_raw(cls, wb) -> list[dict]:
        data_sheets = cls._data_sheets(wb)
        if not data_sheets:
            return []
        ws         = wb[data_sheets[0]]
        sheet_name = data_sheets[0]

        # Extract tag from header area
        global_tag = cls._extract_global_tag(ws)

        # Find header row
        from extraction.formats.adaptive import _find_header_row, _map_headers, _is_footer
        hdr = _find_header_row(ws)
        if hdr is None:
            return []

        col_map = _map_headers(ws, hdr)
        rows    = []

        for r in range(hdr + 1, ws.max_row + 1):
            desc_col = col_map.get("description")
            desc     = clean_str(ws.cell(r, desc_col).value) if desc_col else None
            if desc and _is_footer(desc):
                break
            if all(ws.cell(r, c).value is None
                   for c in range(1, min(ws.max_column + 1, 20))):
                continue

            item = {"sheet": sheet_name, "tag": global_tag}
            for field, col in col_map.items():
                if field == "tag":
                    continue
                raw = ws.cell(r, col).value
                if field in ("quantity", "unit_price", "total_price", "delivery_weeks"):
                    item[field] = clean_num(raw)
                else:
                    item[field] = clean_str(raw)

            if item.get("description") or item.get("part_number"):
                rows.append(item)

        return rows

    @classmethod
    def _extract_global_tag(cls, ws) -> str | None:
        """
        Find the equipment tag in the first 8 rows.
        Looks for cells matching tag patterns like "TAG-001", "30-P-001".
        """
        import re
        tag_re = re.compile(r'\b([A-Z0-9]{2,}-[A-Z0-9][A-Z0-9\-]*)', re.IGNORECASE)
        for r in range(1, min(9, ws.max_row + 1)):
            for c in range(1, min(20, ws.max_column + 1)):
                v = ws.cell(r, c).value
                if v is None:
                    continue
                m = tag_re.search(str(v))
                if m:
                    return m.group(1)
        return None
