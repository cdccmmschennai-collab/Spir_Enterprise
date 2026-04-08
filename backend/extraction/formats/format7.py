"""
extraction/formats/format7.py
──────────────────────────────
FORMAT7 — Multiple numbered main sheets (MAIN SHEET 1, MAIN SHEET 2, ...).

Each "MAIN SHEET N" contains one equipment tag and its spare parts.
The tag is extracted from the sheet's header area.

Detection:
  ≥2 sheets matching "main sheet N" (without category suffix).
"""
from __future__ import annotations
import logging
import re
from extraction.formats.base import BaseParser, clean_str, clean_num
from extraction.formats.adaptive import _find_header_row, _map_headers, _is_footer

log = logging.getLogger(__name__)

_NUMBERED_MAIN_RE = re.compile(r'NUMBERED_SHEET_PATTERN', re.IGNORECASE)


class Format7Parser(BaseParser):
    FORMAT_NAME = "FORMAT7"

    @classmethod
    def detect(cls, wb) -> bool:
        numbered = [s for s in wb.sheetnames if _NUMBERED_MAIN_RE.match(s.strip())]
        return len(numbered) >= 2

    @classmethod
    def _extract_raw(cls, wb) -> list[dict]:
        rows: list[dict] = []
        for sheet_name in wb.sheetnames:
            if not _NUMBERED_MAIN_RE.match(sheet_name.strip()):
                continue
            ws  = wb[sheet_name]
            tag = cls._extract_sheet_tag(ws)
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
                item = {"sheet": sheet_name, "tag": tag}
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
    def _extract_sheet_tag(cls, ws) -> str | None:
        """Find the equipment tag in the first 8 rows of the sheet."""
        import re as _re
        tag_re = _re.compile(r'\b([A-Z0-9]{2,}[-/][A-Z0-9][A-Z0-9\-]*)', _re.IGNORECASE)
        for r in range(1, min(9, ws.max_row + 1)):
            for c in range(1, min(ws.max_column + 1, 20)):
                v = ws.cell(r, c).value
                if v:
                    m = tag_re.search(str(v))
                    if m:
                        return m.group(1)
        return None
