"""
extraction/formats/format8.py
──────────────────────────────
FORMAT8 — Categorised numbered main sheets.

Structure:
  Sheets named "MAIN SHEET 1 (PUMP)", "MAIN SHEET 2 (MOTOR)", etc.
  Each sheet contains equipment of that category.
  Spare type is detected per-item from specific columns.

Detection:
  ≥2 sheets matching "MAIN SHEET N (CATEGORY)" pattern.
"""
from __future__ import annotations
import logging
import re
from extraction.formats.format7 import Format7Parser

log = logging.getLogger(__name__)

_CAT_RE = re.compile(r'main\s+sheet\s+\d+\s*\(', re.IGNORECASE)


class Format8Parser(Format7Parser):
    """
    Inherits all extraction logic from Format7Parser.
    Detection differs: requires the category suffix in parentheses.
    """
    FORMAT_NAME = "FORMAT8"

    @classmethod
    def detect(cls, wb) -> bool:
        categorised = [s for s in wb.sheetnames if _CAT_RE.match(s.strip())]
        return len(categorised) >= 2

    @classmethod
    def _extract_raw(cls, wb) -> list[dict]:
        """Override to process categorised sheets and extract category as metadata."""
        rows: list[dict] = []
        for sheet_name in wb.sheetnames:
            if not _CAT_RE.match(sheet_name.strip()):
                continue

            # Extract category from sheet name e.g. "MAIN SHEET 1 (PUMP)" → "PUMP"
            cat_match = re.search(r'\(([^)]+)\)', sheet_name)
            category  = cat_match.group(1).strip() if cat_match else None

            ws  = wb[sheet_name]
            tag = cls._extract_sheet_tag(ws)

            from extraction.formats.adaptive import _find_header_row, _map_headers, _is_footer
            from extraction.formats.base import clean_str, clean_num

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

                item = {"sheet": sheet_name, "tag": tag, "classification": category}
                for field, col in col_map.items():
                    if field in ("tag", "classification"):
                        continue
                    raw = ws.cell(r, col).value
                    if field in ("quantity", "unit_price", "total_price", "delivery_weeks"):
                        item[field] = clean_num(raw)
                    else:
                        item[field] = clean_str(raw)

                if item.get("description") or item.get("part_number"):
                    rows.append(item)

        return rows
