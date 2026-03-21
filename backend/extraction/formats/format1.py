"""
extraction/formats/format1.py
──────────────────────────────
FORMAT1 — Multi-annexure SPIR.

Structure:
  Multiple sheets named "ANNEXURE X" or "ANNEXURE-X".
  Each annexure sheet has its own header row with tag, description, qty, price columns.
  Global metadata (SPIR No, Manufacturer) in the first non-annexure sheet.

Detection:
  At least one sheet name contains "annexure" (case-insensitive).
"""
from __future__ import annotations
import logging
from extraction.formats.base import BaseParser, clean_str, clean_num

log = logging.getLogger(__name__)


class Format1Parser(BaseParser):
    FORMAT_NAME = "FORMAT1"

    # Column header keywords for FORMAT1 annexure sheets
    _HDR_KEYWORDS = ["description", "part number", "quantity", "price", "tag"]

    @classmethod
    def detect(cls, wb) -> bool:
        names = [s.lower() for s in wb.sheetnames]
        return any("annexure" in n for n in names)

    @classmethod
    def _extract_raw(cls, wb) -> list[dict]:
        rows: list[dict] = []
        for sheet_name in wb.sheetnames:
            if "annexure" not in sheet_name.lower():
                continue
            ws = wb[sheet_name]
            hdr = cls._find_header_row(ws, cls._HDR_KEYWORDS)
            if hdr is None:
                continue
            rows.extend(cls._read_data_rows(ws, hdr, sheet_name))
        return rows

    @classmethod
    def _read_data_rows(cls, ws, header_row: int, sheet_name: str) -> list[dict]:
        from extraction.formats.adaptive import _map_headers, _is_footer
        col_map = _map_headers(ws, header_row)
        result  = []
        for r in range(header_row + 1, ws.max_row + 1):
            desc_col = col_map.get("description")
            desc     = clean_str(ws.cell(r, desc_col).value) if desc_col else None
            if desc and _is_footer(desc):
                break
            # Skip blank rows
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
