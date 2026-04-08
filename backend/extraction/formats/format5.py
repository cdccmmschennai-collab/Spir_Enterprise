"""
extraction/formats/format5.py
──────────────────────────────
FORMAT5 — Flag SPIR + multiple continuation sheets.

Detection: ≥2 sheets containing "continuation" (case-insensitive).
"""
from __future__ import annotations
import logging
from extraction.formats.base import BaseParser, clean_str, clean_num
from extraction.formats.adaptive import _find_header_row, _map_headers, _is_footer

log = logging.getLogger(__name__)


class Format5Parser(BaseParser):
    FORMAT_NAME = "FORMAT5"

    @classmethod
    def detect(cls, wb) -> bool:
        names = [s.lower() for s in wb.sheetnames]
        return sum(1 for n in names if "continuation" in n) >= 2

    @classmethod
    def _extract_raw(cls, wb) -> list[dict]:
        rows: list[dict] = []
        for sheet_name in wb.sheetnames:
            ws  = wb[sheet_name]
            hdr = _find_header_row(ws)
            if hdr is None:
                continue
            col_map = _map_headers(ws, hdr)
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
                    rows.append(item)
        return rows
