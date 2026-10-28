"""
extraction/column_reference.py
-------------------------------
Resolve "Refer column 12A" style values in EQPT MAKE / MODEL / SR NO.

SPIR templates number their data columns in a row under the header
(7, 8, 9, 10A, 10B, 11A, 12A, ...). When the MANUFACTURER metadata says
"Refer column 12A", the real value lives per item in the column numbered 12A
(e.g. SUPPLIER/OCM NAME). An optional name after a slash
("Refer column 12A/YOKOGAWA") is the vendor's explicit equipment-level value.

Resolution (structure-driven — the column-number row is detected, never assumed):
  - Spare rows (have ITEM NUMBER): value of the referenced column for that item.
  - Equipment rows (no ITEM NUMBER): the named value after the slash when given,
    else the most common resolved value among that tag's spare rows on the
    same sheet.
  - Unresolvable references are left unchanged (logged), never guessed.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

from spir_dynamic.analysis.header_detector import find_metadata
from spir_dynamic.models.sheet_profile import SheetProfile

log = logging.getLogger(__name__)

# Row fields that may carry a column reference.
REFERENCE_FIELDS = ("manufacturer", "model", "serial")

# "Refer column 12A", "Refer to col. 12A", "REFER COL NO 12A/YOKOGAWA", "See column 9"
_COLUMN_REF_RE = re.compile(
    r"^\s*(?:refer|see)(?:\s+to)?\s+col(?:umn)?\.?\s*(?:no\.?\s*)?"
    r"(\d{1,2}[A-Z]?)\b\s*(?:[/,]\s*(.*?))?\s*$",
    re.IGNORECASE,
)

# A column-number label: "7", "10A", "12B".
_COLUMN_CODE_RE = re.compile(r"^\d{1,2}[A-Z]?$", re.IGNORECASE)

# A row is the column-number row when at least this many mapped columns carry
# a code AND they make up at least this share of the mapped columns.
_MIN_CODED_COLUMNS = 3
_MIN_CODED_SHARE = 0.6

# column_map key -> output row field (strategies rename these on output).
_MAP_KEY_TO_ROW_FIELD = {
    "description": "desc",
    "quantity": "qty_identical",
    "item_number": "item_num",
    "part_number": "mfr_part_no",
    "supplier": "supplier_name",
    "delivery_weeks": "delivery",
    "sap_number": "sap_no",
    "tag": "tag_no",
}


def parse_column_reference(value: Any) -> tuple[str, str | None] | None:
    """Return (column_code, named_value) for a "Refer column X[/NAME]" value."""
    if value is None:
        return None
    m = _COLUMN_REF_RE.match(str(value))
    if not m:
        return None
    named = (m.group(2) or "").strip() or None
    return m.group(1).upper(), named


def _code_text(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return s.upper() if _COLUMN_CODE_RE.match(s) else None


def find_column_codes(ws, column_map: dict[str, int]) -> dict[str, str]:
    """
    Detect the column-number row and return {code: row_field}, e.g.
    {"12A": "supplier_name", "10A": "mfr_part_no"}. Empty when no such row.
    """
    if not column_map:
        return {}
    cols = {key: col for key, col in column_map.items() if col}
    for r in range(1, (ws.max_row or 0) + 1):
        codes = {key: _code_text(ws.cell(r, col).value) for key, col in cols.items()}
        coded = {key: code for key, code in codes.items() if code}
        if (
            len(coded) >= _MIN_CODED_COLUMNS
            and len(coded) >= _MIN_CODED_SHARE * len(cols)
            and len(set(coded.values())) == len(coded)
        ):
            return {
                code: _MAP_KEY_TO_ROW_FIELD.get(key, key)
                for key, code in coded.items()
            }
    return {}


def _is_spare(row: dict) -> bool:
    return row.get("item_num") not in (None, "")


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def resolve_column_references(
    wb, rows: list[dict[str, Any]], profiles: list[SheetProfile]
) -> None:
    """Replace column-reference values in REFERENCE_FIELDS in place."""
    if not any(parse_column_reference(r.get(f)) for r in rows for f in REFERENCE_FIELDS):
        return

    by_name = {p.name.strip().upper(): p for p in profiles}

    # Column-number maps: per sheet, with the first one found as the workbook
    # fallback (continuation sheets reuse the main sheet's column numbering).
    code_maps: dict[str, dict[str, str]] = {}
    workbook_codes: dict[str, str] = {}
    for p in profiles:
        if p.column_map and p.name in wb.sheetnames:
            codes = find_column_codes(wb[p.name], p.column_map)
            code_maps[p.name.strip().upper()] = codes
            if codes and not workbook_codes:
                workbook_codes = codes

    # A sheet's OWN reference (e.g. "Refer column 12A/YOKOGAWA" on a
    # continuation sheet) wins over the one propagated from the main sheet.
    own_meta: dict[str, dict[str, Any]] = {}

    def _sheet_reference(sheet: str, fld: str) -> tuple[str, str | None] | None:
        if sheet not in own_meta:
            p = by_name.get(sheet)
            own_meta[sheet] = (
                find_metadata(wb[p.name], p.header_row)
                if p is not None and p.name in wb.sheetnames else {}
            )
        return parse_column_reference(own_meta[sheet].get(fld))

    def _reference_for(row: dict, fld: str) -> tuple[str, str | None] | None:
        ref = parse_column_reference(row.get(fld))
        if ref is None:
            return None
        sheet = str(row.get("sheet") or "").strip().upper()
        return _sheet_reference(sheet, fld) or ref

    unresolved: set[str] = set()
    for fld in REFERENCE_FIELDS:
        # Pass 1 — spare rows: value of the referenced column for that item.
        resolved_by_group: dict[tuple[str, str], list[str]] = {}
        for row in rows:
            if not _is_spare(row):
                continue
            ref = _reference_for(row, fld)
            if ref is None:
                continue
            code, named = ref
            sheet = str(row.get("sheet") or "").strip().upper()
            target = (code_maps.get(sheet) or workbook_codes).get(code)
            if target is None or target == fld:
                if named:
                    row[fld] = named
                else:
                    unresolved.add(f"{fld}: column {code}")
                continue
            value = _clean(row.get(target))
            if value and not parse_column_reference(value):
                row[fld] = value
                group = (sheet, str(row.get("tag_no") or "").strip().upper())
                resolved_by_group.setdefault(group, []).append(value)
            else:
                # Referenced cell is blank for this item: named value or blank,
                # never the reference text itself.
                row[fld] = named

        # Pass 2 — equipment rows: named value, else most common of the tag's spares.
        for row in rows:
            if _is_spare(row):
                continue
            ref = _reference_for(row, fld)
            if ref is None:
                continue
            code, named = ref
            if named:
                row[fld] = named
                continue
            sheet = str(row.get("sheet") or "").strip().upper()
            group = (sheet, str(row.get("tag_no") or "").strip().upper())
            values = resolved_by_group.get(group)
            if values:
                row[fld] = Counter(values).most_common(1)[0][0]
            else:
                unresolved.add(f"{fld}: column {code} (equipment row, no resolved spares)")

    if unresolved:
        log.warning("Column references left unresolved: %s", sorted(unresolved))
