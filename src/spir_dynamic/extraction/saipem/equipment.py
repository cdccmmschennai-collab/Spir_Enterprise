"""
Saipem equipment groups: one per equipment column of the form.

Each group's ITEM TAG No. may hold a paired-tag expression such as
005400PH011A/B. It is expanded with the shared split_tags helper, but only
after checking the result really is a set of sibling tags (common stem,
tag-like parts). Anything else is kept whole — part numbers and free text
containing "/" are never split, because only the tag cell is ever expanded.

Serials are paired with the expanded tags by position. A single serial value
(including "-" or "NA") is group-level and repeats for every tag. Model, Free
Field and Sub Item are group-level and repeat for every tag.

No. OF UNITS is read per column, exactly as the generic COLUMN_HEADERS path
does (columnar._read_tag_metadata: eqpt_qty_by_col). The generic fallback that
substitutes the tag count when the cell is missing is NOT applied: a missing
unit count stays blank.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from spir_dynamic.extraction.saipem.layout import SaipemLayout
from spir_dynamic.extraction.saipem.values import clean_value, identifier_text
from spir_dynamic.utils.cell_utils import split_tags
from spir_dynamic.extraction.unified_extractor import _split_serial_range

_TAG_SEPARATORS = re.compile(r"[,/|;]")
_TAG_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._\-]*$")


@dataclass
class EquipmentGroup:
    col: int
    raw_tag: Optional[str]
    tags: list[str]
    serials: list[Optional[str]]          # one per tag (positional pairing)
    meta: dict[str, Any] = field(default_factory=dict)   # free_field / model_or_type / equipment_sub_item_no
    no_of_units: Any = None
    warnings: list[str] = field(default_factory=list)


def expand_equipment_tag(raw: Optional[str]) -> tuple[list[str], Optional[str]]:
    """
    Expand one ITEM TAG No. cell into individual tags.

        '005400PH011A/B' -> ['005400PH011A', '005400PH011B']
        '004300PA001A/B' -> ['004300PA001A', '004300PA001B']
        'P-3425 A/B/C'   -> ['P-3425 A', 'P-3425 B', 'P-3425 C']

    Returns (tags, warning). An expression that does not expand into sibling
    tags is kept as one tag and a warning says so.
    """
    if raw is None:
        return [], None
    raw = raw.strip()
    if not raw:
        return [], None
    if not _TAG_SEPARATORS.search(raw):
        return [raw], None
    parts = list(dict.fromkeys(p.strip() for p in split_tags(raw) if p and p.strip()))
    if len(parts) >= 2 and _are_sibling_tags(parts):
        return parts, None
    return [raw], f"Tag expression '{raw}' does not expand into sibling tags; kept as one tag."


def _are_sibling_tags(parts: list[str]) -> bool:
    if not all(len(p) >= 3 and any(ch.isdigit() for ch in p) and _TAG_PART_RE.match(p) for p in parts):
        return False
    stem = os.path.commonprefix([p.upper() for p in parts])
    return len(stem) >= min(len(p) for p in parts) / 2


def read_equipment_groups(ws, layout: SaipemLayout) -> list[EquipmentGroup]:
    groups: list[EquipmentGroup] = []
    for col in layout.equipment_cols:
        raw_tag = _equipment_value(ws, layout, "tag", col, identifier=True)
        tags, warning = expand_equipment_tag(raw_tag)
        group = EquipmentGroup(col=col, raw_tag=raw_tag, tags=tags, serials=[])
        if warning:
            group.warnings.append(warning)

        group.serials = _pair_serials(_equipment_value(ws, layout, "serial", col, identifier=True), tags, group)
        group.meta = {
            "free_field": _equipment_value(ws, layout, "free_field", col, identifier=True),
            "model_or_type": _equipment_value(ws, layout, "model", col, identifier=True),
            "equipment_sub_item_no": _equipment_value(ws, layout, "sub_item", col, identifier=True),
        }
        group.no_of_units = _equipment_value(ws, layout, "units", col)
        if isinstance(group.no_of_units, (int, float)) and len(tags) not in (0, group.no_of_units):
            group.warnings.append(
                f"Tag '{raw_tag}': {len(tags)} tag(s) but No. OF UNITS is {group.no_of_units}; "
                "values extracted as found."
            )
        groups.append(group)
    return groups


def _pair_serials(raw: Optional[str], tags: list[str], group: EquipmentGroup) -> list[Optional[str]]:
    if not tags:
        return []
    if raw is None:
        return [None] * len(tags)
    serials = [s for s in _split_serial_range(raw, len(tags)) if s] if len(tags) > 1 else [raw]
    if len(serials) <= 1:
        return [raw] * len(tags)   # one group-level value (or "-", "NA")
    if len(serials) != len(tags):
        group.warnings.append(
            f"Tag '{group.raw_tag}': {len(serials)} serial(s) for {len(tags)} tag(s); "
            "paired by position, unmatched entries left blank."
        )
    return [serials[i] if i < len(serials) else None for i in range(len(tags))]


def _equipment_value(ws, layout: SaipemLayout, key: str, col: int, identifier: bool = False) -> Any:
    """First value in this column across the rows of the label's merged range."""
    rows = layout.equipment_rows.get(key)
    if rows is None:
        return None
    for r in range(rows[0], rows[1] + 1):
        ar, ac = layout.merges.anchor(r, col)
        if ar != r:
            continue
        cell = ws.cell(ar, ac)
        v = identifier_text(cell) if identifier else clean_value(cell.value)
        if v is not None:
            return v
    return None
