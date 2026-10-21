"""
Saipem spare-part matrix: one SpareLine per SPIR position row.

A line belongs to an equipment group when that group's per-unit installed
quantity cell holds a value (a number — zero included — or text). A blank
cell means the line does not apply to that group; nothing is invented.

Quantities are kept apart:
  per_unit[col]         QUANTITY OF PARTS INSTALLED PER ONE UNIT (per group)
  total_installed       TOTAL QUANTITY OF IDENTICAL PARTS INSTALLED (source
                        formula's cached result, never recomputed)
  op2y_* / cap_*        2-year operating and capital spare quantities
Computed values: op2y_vendor_total = PM + CM (see vendor_total) and
new_description_of_parts (see new_description).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from spir_dynamic.extraction.saipem.layout import SaipemLayout
from spir_dynamic.extraction.saipem.profile import COLUMN_BY_ID, NEW_DESCRIPTION_FIELD_IDS
from spir_dynamic.extraction.saipem.values import clean_value, identifier_text, is_number
from spir_dynamic.utils.cell_utils import is_placeholder


@dataclass
class SpareLine:
    row: int
    values: dict[str, Any]                       # field id → value
    per_unit: dict[int, Any] = field(default_factory=dict)   # equipment col → installed qty per unit
    total_installed: Any = None
    extras: dict[int, Any] = field(default_factory=dict)     # extra matrix col → value


def read_spare_lines(ws, layout: SaipemLayout) -> list[SpareLine]:
    lines: list[SpareLine] = []
    pos_col = layout.columns.get("spir_pos_no", layout.pos_col)
    desc_col = layout.columns.get("description_of_parts")
    for r in range(layout.data_first, layout.data_last + 1):
        pos = _read(ws, layout, r, pos_col, identifier=True)
        desc = _read(ws, layout, r, desc_col) if desc_col else None
        if pos is None and desc is None:
            continue   # spacer, sub-total or footer row

        values: dict[str, Any] = {}
        for fid, col in layout.columns.items():
            values[fid] = _read(ws, layout, r, col, identifier=COLUMN_BY_ID[fid].identifier)
        if "op2y_vendor_total" not in layout.columns:
            values["op2y_vendor_total"] = vendor_total(values.get("op2y_vendor_pm"),
                                                       values.get("op2y_vendor_cm"))
        if "price_currency" not in layout.columns:
            values["price_currency"] = layout.currency
        values["new_description_of_parts"] = new_description(values)

        lines.append(SpareLine(
            row=r,
            values=values,
            per_unit={c: _read(ws, layout, r, c) for c in layout.equipment_cols},
            total_installed=_read(ws, layout, r, layout.total_col) if layout.total_col else None,
            extras={mc.col: _read(ws, layout, r, mc.col) for mc in layout.extra_columns},
        ))
    return lines


def new_description(values: dict[str, Any]) -> Optional[str]:
    """DESCRIPTION OF PARTS,MATERIAL,ORIGINAL MAKERS NAME,ORIGINAL MAKERS PART No.

    Same "," join as the Standard NEW DESCRIPTION; blank, "-" and "NA" parts
    are left out so the text never carries placeholders.
    """
    parts = [str(values[f]).strip() for f in NEW_DESCRIPTION_FIELD_IDS
             if not is_placeholder(values.get(f))]
    return ",".join(parts) or None


def vendor_total(pm: Any, cm: Any) -> Optional[float]:
    """RECOMMEND. By VENDOR total = PM + CM (Instruction sheet, Column U).

    Blank parts count as nothing; a text part (e.g. 'NA', '-') makes the
    total undefined, so it stays blank rather than guessed.
    """
    parts = [v for v in (pm, cm) if v is not None]
    if not parts or not all(is_number(v) for v in parts):
        return None
    total = sum(parts)
    return int(total) if isinstance(total, float) and total.is_integer() else total


def _read(ws, layout: SaipemLayout, row: int, col: int, identifier: bool = False) -> Any:
    ar, ac = layout.merges.anchor(row, col)
    cell = ws.cell(ar, ac)
    return identifier_text(cell) if identifier else clean_value(cell.value)
