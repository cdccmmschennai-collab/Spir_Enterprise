"""
extraction/formats/format6.py
──────────────────────────────
FORMAT6 — Single continuation sheet + multi-tag comma/slash cells.

Key features:
  • Quantity identical parts column with N tags in one cell (e.g. "8" for 8 tags)
  • Tags appear as "30-GV-146, 171, 169, 150" — comma-separated with prefix sharing
  • Continuation sheet tags also comma/slash separated

Detection:
  Exactly one "continuation" sheet AND the main sheet has comma-separated tag
  cells in its first row header area.
"""
from __future__ import annotations
import logging
import re
from extraction.formats.base import BaseParser, clean_str, clean_num, split_tags
from extraction.formats.adaptive import _find_header_row, _map_headers, _is_footer

log = logging.getLogger(__name__)

_COMMA_TAG_RE = re.compile(r'[A-Z0-9].*,.*[A-Z0-9]', re.IGNORECASE)


class Format6Parser(BaseParser):
    FORMAT_NAME = "FORMAT6"

    @classmethod
    def detect(cls, wb) -> bool:
        names_lower = [s.lower() for s in wb.sheetnames]
        cont_count  = sum(1 for n in names_lower if "continuation" in n)
        if cont_count != 1:
            return False
        # Check for comma-separated tags in first main sheet header area
        main_sheets = [s for s in wb.sheetnames if "continuation" not in s.lower()]
        if not main_sheets:
            return False
        ws = wb[main_sheets[0]]
        for r in range(1, min(9, ws.max_row + 1)):
            for c in range(1, min(ws.max_column + 1, 20)):
                v = ws.cell(r, c).value
                if v and _COMMA_TAG_RE.search(str(v)):
                    return True
        return False

    @classmethod
    def _extract_raw(cls, wb) -> list[dict]:
        rows: list[dict] = []
        for sheet_name in wb.sheetnames:
            ws       = wb[sheet_name]
            is_cont  = "continuation" in sheet_name.lower()
            hdr      = _find_header_row(ws)
            if hdr is None:
                continue
            col_map  = _map_headers(ws, hdr)

            # For continuation sheets, the tag is in the header area
            cont_tag = None
            if is_cont:
                cont_tag = cls._find_cont_tag(ws)

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

                if is_cont and cont_tag and not item.get("tag"):
                    item["tag"] = cont_tag

                if item.get("description") or item.get("part_number"):
                    # Adjust quantity for multi-tag rows:
                    # If qty_identical == n_tags, each tag gets qty/n_tags
                    tag_raw = item.get("tag")
                    if tag_raw:
                        n_tags = len(split_tags(tag_raw))
                        qty    = item.get("quantity")
                        if n_tags > 1 and qty and qty == n_tags:
                            item["quantity"] = 1
                    rows.append(item)

        return rows

    @classmethod
    def _find_cont_tag(cls, ws) -> str | None:
        """Find the tag value in a continuation sheet's header area."""
        for r in range(1, min(9, ws.max_row + 1)):
            for c in range(1, min(ws.max_column + 1, 20)):
                v = ws.cell(r, c).value
                if v and _COMMA_TAG_RE.search(str(v)):
                    return str(v).strip()
        return None
