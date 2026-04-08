"""
extraction/formats/format4.py
──────────────────────────────
FORMAT4 — Matrix SPIR + single continuation sheet.

Structure:
  Main sheet: rows with tag numbers in dedicated column, qty columns per tag unit.
  One "CONTINUATION" sheet referencing items from the main sheet.

Detection:
  Exactly one sheet contains "continuation" (case-insensitive).
  No comma-separated multi-tag cells in the main sheet's first row.
"""
from __future__ import annotations
import logging
from extraction.formats.base import BaseParser, clean_str, clean_num
from extraction.formats.adaptive import _find_header_row, _map_headers, _is_footer

log = logging.getLogger(__name__)


class Format4Parser(BaseParser):
    FORMAT_NAME = "FORMAT4"

    @classmethod
    def detect(cls, wb) -> bool:
        names = [s.lower() for s in wb.sheetnames]
        cont  = [n for n in names if "continuation" in n]
        if len(cont) != 1:
            return False
        # No categorised main sheets (that's FORMAT8)
        main  = [n for n in names if "main sheet" in n and "(" in n]
        return len(main) == 0

    @classmethod
    def _extract_raw(cls, wb) -> list[dict]:
        rows: list[dict] = []
        for sheet_name in wb.sheetnames:
            ws  = wb[sheet_name]
            hdr = _find_header_row(ws)
            if hdr is None:
                continue
            col_map = _map_headers(ws, hdr)
            rows.extend(cls._read_rows(ws, hdr, col_map, sheet_name))
        return rows

    @classmethod
    def _read_rows(cls, ws, hdr, col_map, sheet_name) -> list[dict]:
        result = []
        for r in range(hdr + 1, ws.max_row + 1):
            desc_col = col_map.get("description")
            desc     = clean_str(ws.cell(r, desc_col).value) if desc_col else None
            if desc and _is_footer(desc):
                break
            if all(ws.cell(r, c).value is None
                   for c in range(1, min(ws.max_column + 1, 20))):
                continue
            item = {"sheet": sheet_name}
            for field, col in col_map.items():
                raw = ws.cell(r, col).value
                if field in ("quantity", "unit_price", "total_price", "delivery_weeks"):
                    item[field] = clean_num(raw)
                else:
                    item[field] = clean_str(raw)
            if item.get("description") or item.get("part_number"):
                result.append(item)
        return result
