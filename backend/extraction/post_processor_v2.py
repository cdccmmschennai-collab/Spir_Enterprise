"""
extraction/post_processor_v2.py
────────────────────────────────
Post-processing: adds POSITION NUMBER and OLD MATERIAL NUMBER/SPF NUMBER
to every extracted row.

Rules verified against all 8 real SPIR output files.

POSITION NUMBER
───────────────
- Equipment header row (ITEM NUMBER is None/blank) → "0010", counter unchanged
- Spare rows under a tag → 0010, 0020, 0030 … in steps of 10
- Cross-sheet continuation: same tag resumes where it left off
  e.g. tag has 24 spares on sheet 1 (0010…0240), sheet 2 continues at 0250
- New tag always resets to 0010

OLD MATERIAL NUMBER / SPF NUMBER
──────────────────────────────────
- Exactly 18 characters, always
- Format: {trimmed_spir_base}-{suffix}
  suffix = "L01","L02"… for sheet 1
           "1L1","1L2"… for sheet 2
           "2L1"… for sheet 3, etc.
- Base = SPIR number with letters/brackets stripped, hyphens kept
- Trimming priority (never removes 4-digit groups):
    1. Single-digit segments rightmost first
    2. Leading zeros from segments rightmost first (value-preserving)
    3. Hyphens rightmost first
- line_idx = sequential spare line within that sheet (per sheet, 1-based)
"""
from __future__ import annotations
import re
import logging

log = logging.getLogger(__name__)

TARGET_LEN = 18


# ─────────────────────────────────────────────────────────────────────────────
# SPF number
# ─────────────────────────────────────────────────────────────────────────────

def _spf_base(spir_no: str) -> str:
    """Strip everything except digits and hyphens, collapse double-hyphens."""
    s = re.sub(r'[^0-9\-]', '-', spir_no or '')
    s = re.sub(r'-{2,}', '-', s)
    return s.strip('-')


def _spf_suffix(sheet_idx: int, line_idx: int) -> str:
    """
    sheet_idx is 1-based (first data sheet = 1).
    line_idx  is 1-based within that sheet.
    """
    if sheet_idx == 1:
        return f"L{line_idx:02d}" if line_idx < 100 else f"L{line_idx}"
    return f"{sheet_idx - 1}L{line_idx}"


def _trim_to_budget(base: str, budget: int) -> str:
    """Trim base string to at most `budget` characters."""
    if len(base) <= budget:
        return base

    segs = base.split('-')

    # Pass 1: remove single-digit segments, rightmost first
    changed = True
    while changed and len('-'.join(segs)) > budget:
        changed = False
        for i in range(len(segs) - 1, -1, -1):
            if len(segs[i]) == 1 and segs[i].isdigit():
                segs.pop(i)
                changed = True
                break

    base = '-'.join(segs)
    if len(base) <= budget:
        return base

    # Pass 2: remove leading zeros from segments, rightmost first
    # "0006" → "006" → "06" → "6"  (preserves numeric value)
    # "6100" stays "6100"            (leading digit is not zero)
    segs = base.split('-')
    changed = True
    while changed and len('-'.join(segs)) > budget:
        changed = False
        for i in range(len(segs) - 1, -1, -1):
            if len(segs[i]) >= 2 and segs[i][0] == '0':
                segs[i] = segs[i][1:]
                changed = True
                break

    base = '-'.join(segs)
    if len(base) <= budget:
        return base

    # Pass 3: fuse segments by removing rightmost hyphens
    while '-' in base and len(base) > budget:
        idx  = base.rfind('-')
        base = base[:idx] + base[idx + 1:]

    return base[:budget]


def build_spf_number(spir_no: str, sheet_idx: int, line_idx: int) -> str:
    """
    Return an exactly-18-character SPF number.

    Args:
        spir_no:   Full SPIR document number (e.g. "VEN-4460-KAHS-5-43-1002-2")
        sheet_idx: 1-based sheet index (1 = first data sheet)
        line_idx:  1-based spare line within sheet_idx
    """
    base   = _spf_base(spir_no)
    suffix = _spf_suffix(sheet_idx, line_idx)
    budget = TARGET_LEN - 1 - len(suffix)   # 1 = separator hyphen

    trimmed = _trim_to_budget(base, budget)

    # Pad if trimmed is shorter than budget
    if len(trimmed) < budget:
        trimmed = trimmed + '0' * (budget - len(trimmed))

    result = f"{trimmed}-{suffix}"

    # Hard guarantee: exactly 18 chars
    if len(result) != TARGET_LEN:
        result = result[:TARGET_LEN]

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Helpers to locate column indices
# ─────────────────────────────────────────────────────────────────────────────

def _get_ci():
    from extraction.output_schema import CI
    return CI


def _col(ci: dict, *names) -> int | None:
    """Return the first column index that exists in CI."""
    for name in names:
        idx = ci.get(name)
        if idx is not None:
            return idx
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Position number engine
# ─────────────────────────────────────────────────────────────────────────────

class PositionEngine:
    """
    Cross-sheet position number tracker.
    Same tag continues across sheets; new tag resets to 0010.
    """

    def __init__(self):
        self._last: dict[str, int] = {}   # tag → last assigned value (multiple of 10)

    def next(self, tag: str | None, is_spare: bool) -> str:
        """
        Return the position number for this row.

        is_spare = True  → spare row (item number present)
        is_spare = False → equipment header row
        """
        key = (tag or '__GLOBAL__').strip().upper()

        if not is_spare:
            # Equipment header row always gets "0010", counter unchanged
            return "0010"

        if key not in self._last:
            self._last[key] = 10          # first spare: 0010
        else:
            self._last[key] += 10         # continue: 0020, 0030 …

        return str(self._last[key]).zfill(4)


# ─────────────────────────────────────────────────────────────────────────────
# Sheet index tracker
# ─────────────────────────────────────────────────────────────────────────────

class SheetTracker:
    """Maps sheet names to 1-based sheet indices, tracks line indices per sheet."""

    def __init__(self):
        self._sheet_to_idx: dict[str, int] = {}
        self._sheet_line:   dict[str, int] = {}   # sheet → current spare line count

    def get_sheet_idx(self, sheet: str | None) -> int:
        key = (sheet or 'MAIN').strip()
        if key not in self._sheet_to_idx:
            self._sheet_to_idx[key] = len(self._sheet_to_idx) + 1
        return self._sheet_to_idx[key]

    def next_line(self, sheet: str | None) -> int:
        key = (sheet or 'MAIN').strip()
        self._sheet_line[key] = self._sheet_line.get(key, 0) + 1
        return self._sheet_line[key]


# ─────────────────────────────────────────────────────────────────────────────
# Main post-processor
# ─────────────────────────────────────────────────────────────────────────────

def post_process_rows(rows: list[list], spir_no: str) -> list[list]:
    """
    Assign POSITION NUMBER and OLD MATERIAL NUMBER/SPF NUMBER to every row.

    Modifies rows in-place and returns the same list.
    Safe to call with rows=[] (no-op).
    """
    if not rows:
        return rows

    ci = _get_ci()

    # Locate columns — support both old schema (enterprise) and new schema (v2)
    tag_col  = _col(ci, 'TAG NO', 'tag')
    item_col = _col(ci, 'ITEM NUMBER', 'item_number')
    pos_col  = _col(ci, 'POSITION NUMBER', 'position_number')
    spf_col  = _col(ci, 'OLD MATERIAL NUMBER/SPF NUMBER', 'old_material_number')
    sheet_col= _col(ci, 'SHEET', 'sheet')

    if pos_col is None and spf_col is None:
        log.debug("post_process_rows: no POSITION NUMBER or SPF NUMBER column found — skipping")
        return rows

    pos_engine   = PositionEngine()
    sheet_tracker = SheetTracker()

    spir_no_clean = (spir_no or '').strip()

    for row in rows:
        # Safety: ensure row is long enough
        ncols = len(row)

        tag    = row[tag_col]   if tag_col   is not None and tag_col   < ncols else None
        item   = row[item_col]  if item_col  is not None and item_col  < ncols else None
        sheet  = row[sheet_col] if sheet_col is not None and sheet_col < ncols else None

        is_spare = item is not None and str(item).strip() not in ('', 'None')

        # ── Position number ───────────────────────────────────────────────────
        if pos_col is not None and pos_col < ncols:
            row[pos_col] = pos_engine.next(tag, is_spare)

        # ── SPF / Old Material Number ─────────────────────────────────────────
        if spf_col is not None and spf_col < ncols and is_spare and spir_no_clean:
            sheet_idx  = sheet_tracker.get_sheet_idx(sheet)
            line_idx   = sheet_tracker.next_line(sheet)
            row[spf_col] = build_spf_number(spir_no_clean, sheet_idx, line_idx)

    return rows