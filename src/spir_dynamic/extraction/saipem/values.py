"""
Cell reading and value cleaning for Saipem SPIR workbooks.

Merged ranges: a value lives only in the top-left (anchor) cell of a merged
range; openpyxl returns None for every other cell. MergeIndex maps any cell to
its range so callers always read the anchor.

Cleaning rules (approved for Saipem):
  - strings: surrounding whitespace / non-breaking spaces trimmed, inner text
    (including line breaks) kept; an empty result is blank (None)
  - "NA" stays "NA", "-" stays "-" — the source's own way of saying
    not applicable / not available is never rewritten
  - numbers and zeros are kept as numbers; identifiers become text without
    numeric reformatting (identifier_text)
  - uppercasing happens once, when output rows are assembled
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional

_WS_RE = re.compile(r"\s+")
_ZERO_PAD_FORMAT_RE = re.compile(r"^0+$")


class MergeIndex:
    """(row, col) → merged range bounds (min_row, min_col, max_row, max_col)."""

    def __init__(self, ws):
        self._ranges: dict[tuple[int, int], tuple[int, int, int, int]] = {}
        for rng in ws.merged_cells.ranges:
            bounds = (rng.min_row, rng.min_col, rng.max_row, rng.max_col)
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    self._ranges[(r, c)] = bounds

    def bounds(self, row: int, col: int) -> tuple[int, int, int, int]:
        """Range containing the cell, or the cell itself when it is not merged."""
        return self._ranges.get((row, col), (row, col, row, col))

    def anchor(self, row: int, col: int) -> tuple[int, int]:
        r1, c1, _, _ = self.bounds(row, col)
        return r1, c1

    def is_anchor(self, row: int, col: int) -> bool:
        return self.anchor(row, col) == (row, col)


def anchor_cell(ws, merges: MergeIndex, row: int, col: int):
    r, c = merges.anchor(row, col)
    return ws.cell(r, c)


def clean_value(v: Any) -> Any:
    """Trim strings (blank → None); every other value is returned unchanged."""
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return v


def identifier_text(cell) -> Optional[str]:
    """
    An identifier (tag, POS. No., part / drawing / vendor code, phone) as text,
    exactly as the source shows it: '0001' stays '0001', 2120.1 stays '2120.1',
    88496189 never becomes '88496189.0', and a zero-padded number format
    ('0000') keeps its leading zeros.
    """
    v = clean_value(cell.value)
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, bool):
        return str(v).upper()
    if isinstance(v, (datetime, date)):
        return v.date().isoformat() if isinstance(v, datetime) else v.isoformat()
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, int):
        fmt = str(getattr(cell, "number_format", "") or "")
        if _ZERO_PAD_FORMAT_RE.match(fmt):
            return str(v).zfill(len(fmt))
        return str(v)
    return repr(v) if isinstance(v, float) else str(v)


def norm_label(v: Any) -> str:
    """Comparable form of a header / label cell: whitespace collapsed, uppercased."""
    if v is None:
        return ""
    return _WS_RE.sub(" ", str(v)).strip().upper()


def display_label(v: Any) -> str:
    """A source label for display: whitespace collapsed, capitalization kept."""
    return _WS_RE.sub(" ", str(v or "")).strip()


def is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)
