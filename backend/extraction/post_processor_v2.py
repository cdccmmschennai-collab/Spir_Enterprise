"""
extraction/post_processor_v2.py
─────────────────────────────────
Post-processing pipeline (v2).

Runs AFTER all format extractors, on the final merged row list.

Passes (in order):
    1. Fix qty_identical for multi-tag expansions.
    2. Assign POSITION NUMBER (cross-sheet, per spec).
    3. Assign OLD MATERIAL NUMBER / SPF NUMBER (18 chars, per spec).

Both computed columns use the new engines in:
    extraction/position_number.py
    extraction/spf_number.py

The old post_processor.py is kept for backward compatibility.
This file is the new version used by the v2 pipeline.
"""
from __future__ import annotations
import logging
from extraction.position_number import PositionNumberEngine
from extraction.spf_number import build_old_material_number

log = logging.getLogger(__name__)


def _get_ci():
    from extraction.output_schema import CI
    return CI


# ─────────────────────────────────────────────────────────────────────────────
# Pass 1: Fix qty for expanded multi-tag rows
# ─────────────────────────────────────────────────────────────────────────────

def _fix_qty(rows: list[list], CI: dict) -> None:
    """
    When N tags are expanded from one cell and qty_identical == N,
    each tag actually has qty = 1, not N.

    Example: 8 rows, ITEM=12, QTY_IDENTICAL=8 → set each to 1.
    """
    qi_col   = CI.get('QUANTITY IDENTICAL PARTS FITTED')
    item_col = CI.get('ITEM NUMBER')
    spir_col = CI.get('SPIR NO')

    if qi_col is None or item_col is None:
        return

    i = 0
    while i < len(rows):
        row  = rows[i]
        item = row[item_col]
        if item is None:
            i += 1; continue
        qi = row[qi_col]
        if qi is None or qi <= 1:
            i += 1; continue

        spir = row[spir_col] if spir_col is not None else None
        j    = i
        while j < len(rows):
            r2 = rows[j]
            if r2[item_col] == item and (spir_col is None or r2[spir_col] == spir):
                j += 1
            else:
                break
        n = j - i
        if n > 1 and qi == n:
            for k in range(i, j):
                rows[k][qi_col] = 1
        i = j if j > i else i + 1


# ─────────────────────────────────────────────────────────────────────────────
# Pass 2: Assign POSITION NUMBER
# ─────────────────────────────────────────────────────────────────────────────

def _assign_positions(rows: list[list], CI: dict) -> None:
    """
    Uses PositionNumberEngine to assign cross-sheet position numbers.
    Same tag → continues. New tag → resets to 0010.
    """
    tag_col  = CI.get('TAG NO')
    item_col = CI.get('ITEM NUMBER')
    pos_col  = CI.get('POSITION NUMBER')
    if pos_col is None:
        return

    engine = PositionNumberEngine()
    for row in rows:
        tag_no   = row[tag_col]  if tag_col  is not None else None
        item_num = row[item_col] if item_col is not None else None
        is_spare = item_num is not None
        if tag_no is None and item_num is None:
            continue
        row[pos_col] = engine.next(tag_no=tag_no, is_spare=is_spare)


# ─────────────────────────────────────────────────────────────────────────────
# Pass 3: Assign OLD MATERIAL NUMBER / SPF NUMBER
# ─────────────────────────────────────────────────────────────────────────────

def _assign_old_material(rows: list[list], spir_no: str, CI: dict) -> None:
    """
    Assign OLD MATERIAL NUMBER / SPF NUMBER using build_old_material_number().

    sheet_idx: 1-based order in which each unique sheet name first appears.
    line_idx:  1-based count of spare rows within that sheet
               (same item under multiple tags = same line number —
                line tracks physical position in the SPIR, not output rows).
    """
    old_col   = CI.get('OLD MATERIAL NUMBER/SPF NUMBER')
    item_col  = CI.get('ITEM NUMBER')
    sheet_col = CI.get('SHEET')
    spir_col  = CI.get('SPIR NO')
    if old_col is None:
        return

    # Build sheet → 1-based index (order of first appearance)
    sheet_order: dict[str, int] = {}
    for row in rows:
        s = row[sheet_col] if sheet_col is not None else None
        if s and s not in sheet_order:
            sheet_order[s] = len(sheet_order) + 1

    # Build per-sheet mapping: item_num → line_index
    # Same item under multiple tags in the same sheet = same line number.
    sheet_item_line: dict[str, dict] = {}   # sheet → {item_num: line_no}
    sheet_seq:       dict[str, int]  = {}   # sheet → next seq

    for row in rows:
        item_num = row[item_col] if item_col is not None else None
        if item_num is None:
            continue
        sheet = (row[sheet_col] if sheet_col is not None else None) or 'SHEET1'
        if sheet not in sheet_item_line:
            sheet_item_line[sheet] = {}
            sheet_seq[sheet]       = 0
        if item_num not in sheet_item_line[sheet]:
            sheet_seq[sheet] += 1
            sheet_item_line[sheet][item_num] = sheet_seq[sheet]

    # Assign
    for row in rows:
        item_num = row[item_col] if item_col is not None else None
        if item_num is None:
            continue
        sheet    = (row[sheet_col] if sheet_col is not None else None) or 'SHEET1'
        sheet_i  = sheet_order.get(sheet, 1)
        line_i   = sheet_item_line.get(sheet, {}).get(item_num, 1)
        effective_spir = (row[spir_col] if spir_col is not None else None) or spir_no or ''
        row[old_col] = build_old_material_number(effective_spir, sheet_i, line_i)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def post_process_rows(rows: list[list], spir_no: str) -> list[list]:
    """
    Run all post-processing passes on the extracted row list.

    Args:
        rows:    List of OUTPUT_COLS-length lists. Mutated in place.
        spir_no: SPIR document number (for OLD MATERIAL NUMBER).

    Returns:
        Same list (mutated).
    """
    CI = _get_ci()
    _fix_qty(rows, CI)
    _assign_positions(rows, CI)
    _assign_old_material(rows, spir_no, CI)
    return rows
