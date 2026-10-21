"""
Saipem SPIR format detection — structural, never by filename.

A sheet is a Saipem SPIR form when ALL of these markers are present:

  - equipment labels ITEM TAG No., SERIAL No. and MODEL OR TYPE stacked in one
    label column, with No. OF UNITS and QUANTITY OF PARTS INSTALLED PER ONE
    UNIT in that same column;
  - a TOTAL QUANTITY OF IDENTICAL PARTS INSTALLED header to the right of it;
  - a spare-part matrix header row where SPIR POS. No. and DESCRIPTION OF
    PARTS sit side by side, to the right of the total column.

The Instruction sheet lists the same labels down one column, so the
side-by-side matrix header is what tells the real form apart from it.

Excluded by name: sheets containing "example" / "sample" (template examples
with placeholder data) and instruction / guideline / reference sheets.
Hidden sheets are skipped, as in the generic analyzer.
"""
from __future__ import annotations

import re
from typing import Optional

from spir_dynamic.extraction.saipem.layout import (
    EQUIPMENT_LABELS,
    IS_DESCRIPTION,
    IS_SPIR_POS,
    IS_TOTAL_INSTALLED,
)
from spir_dynamic.extraction.saipem.values import norm_label

EXCLUDED_SHEET_RE = re.compile(r"example|sample|instruction|guideline|reference", re.IGNORECASE)

# The equipment block and matrix headers sit at the top of the form.
_SCAN_ROWS = 60
_SCAN_COLS = 80


def is_excluded_sheet_name(name: str) -> bool:
    return bool(EXCLUDED_SHEET_RE.search(name or ""))


def detect_saipem_sheets(wb) -> list[str]:
    """Names of the sheets that hold actual Saipem SPIR form data, in workbook order."""
    found: list[str] = []
    for name in wb.sheetnames:
        if is_excluded_sheet_name(name):
            continue
        ws = wb[name]
        if getattr(ws, "sheet_state", "visible") != "visible":
            continue
        if is_saipem_form(ws):
            found.append(name)
    return found


_REQUIRED_MARKERS = [
    ("ITEM TAG No. (MAIN ITEM)", EQUIPMENT_LABELS["tag"]),
    ("SERIAL No.", EQUIPMENT_LABELS["serial"]),
    ("MODEL OR TYPE", EQUIPMENT_LABELS["model"]),
    ("No. OF UNITS", EQUIPMENT_LABELS["units"]),
    ("QUANTITY OF PARTS INSTALLED PER ONE UNIT", EQUIPMENT_LABELS["per_unit"]),
    ("TOTAL QUANTITY OF IDENTICAL PARTS INSTALLED", IS_TOTAL_INSTALLED),
    ("SPIR POS. No.", IS_SPIR_POS),
    ("DESCRIPTION OF PARTS", IS_DESCRIPTION),
]
_MAX_EXPLAINED_SHEETS = 5


def explain_not_saipem(wb) -> str:
    """Why no sheet of `wb` was accepted as a Saipem SPIR form (logged for support; the user sees a short message)."""
    reasons: list[str] = []
    for name in wb.sheetnames:
        ws = wb[name]
        if is_excluded_sheet_name(name):
            reasons.append(f"'{name}' skipped (example / sample / instruction sheet)")
        elif getattr(ws, "sheet_state", "visible") != "visible":
            reasons.append(f"'{name}' skipped (hidden sheet)")
        else:
            norms = [n for _, _, n in _top_text_cells(ws)]
            missing = [label for label, pred in _REQUIRED_MARKERS if not any(pred(n) for n in norms)]
            if missing:
                reasons.append(f"'{name}' is missing {', '.join(missing)}")
            else:
                reasons.append(
                    f"'{name}' has the Saipem labels but not the Saipem form layout "
                    "(equipment block beside a SPIR POS. No. / DESCRIPTION OF PARTS matrix)"
                )
    more = len(reasons) - _MAX_EXPLAINED_SHEETS
    details = "; ".join(reasons[:_MAX_EXPLAINED_SHEETS]) + (f"; …and {more} more sheet(s)" if more > 0 else "")
    return (
        "This workbook is not a supported Saipem SPIR form, so it was not extracted. "
        "Select Standard to extract it with the standard template. "
        f"Details: {details or 'the workbook has no sheets'}."
    )


def is_saipem_form(ws) -> bool:
    cells = _top_text_cells(ws)
    if not cells:
        return False

    def where(pred) -> list[tuple[int, int]]:
        return [(r, c) for r, c, n in cells if pred(n)]

    tags = where(EQUIPMENT_LABELS["tag"])
    for tag_r, label_col in tags:
        same_col = lambda pred: [rc for rc in where(pred) if rc[1] <= label_col and rc[0] > tag_r]
        if not all(same_col(EQUIPMENT_LABELS[k]) for k in ("serial", "model", "units", "per_unit")):
            continue
        totals = [c for _, c in where(IS_TOTAL_INSTALLED) if c > label_col]
        if not totals:
            continue
        total_col = min(totals)
        if _matrix_header_found(where, total_col):
            return True
    return False


def _matrix_header_found(where, total_col: int) -> bool:
    descriptions = where(IS_DESCRIPTION)
    for pos_r, pos_c in where(IS_SPIR_POS):
        if pos_c <= total_col:
            continue
        if any(r == pos_r and c > pos_c for r, c in descriptions):
            return True
    return False


def _top_text_cells(ws) -> list[tuple[int, int, str]]:
    """(row, col, normalized text) of text cells in the top-left scan window.

    Bounded by the sheet's own dimensions: reading never creates cells, so the
    generic extractor sees exactly the same worksheet afterwards.
    """
    max_row: Optional[int] = min(ws.max_row or 0, _SCAN_ROWS)
    max_col: Optional[int] = min(ws.max_column or 0, _SCAN_COLS)
    if not max_row or not max_col:
        return []
    out: list[tuple[int, int, str]] = []
    for r_idx, row in enumerate(
        ws.iter_rows(min_row=1, max_row=max_row, max_col=max_col, values_only=True), start=1
    ):
        for c_idx, v in enumerate(row, start=1):
            if isinstance(v, str):
                n = norm_label(v)
                if n:
                    out.append((r_idx, c_idx, n))
    return out
