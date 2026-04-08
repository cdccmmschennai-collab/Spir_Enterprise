"""
extraction/formats/base.py
───────────────────────────
Abstract base class for all SPIR format parsers.

Every concrete parser must implement:
    FORMAT_NAME : str           — unique identifier e.g. "FORMAT6"
    detect(wb)  : classmethod   — return True if this workbook matches
    parse(wb)   : classmethod   — return list[dict] of normalized rows

The list[dict] uses the field names from output_schema.OUTPUT_COLUMNS.

Tag splitting is handled HERE in the base class — parsers return raw tag
values, the base class does the splitting. Each parser just needs to fill
the 'tag' field with whatever is in the cell (e.g. "1/2/3" or "A, B, C").
"""
from __future__ import annotations
import logging
import re
from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

# Separators that indicate multiple tags in one cell
_TAG_SEPARATORS = re.compile(r'[,/|;]')

# Placeholder values treated as empty
_PLACEHOLDERS = frozenset({
    '', '-', '--', '---', 'n/a', 'na', 'n.a', 'n.a.', 'tba', 'tbc',
    'nil', 'none', 'not applicable', 'not available', 'unknown', '.',
})


def _is_placeholder(v: Any) -> bool:
    if v is None:
        return True
    return str(v).strip().lower() in _PLACEHOLDERS


def clean_str(v: Any) -> str | None:
    """Return stripped string or None if placeholder."""
    if _is_placeholder(v):
        return None
    return str(v).strip()


def clean_num(v: Any) -> float | None:
    """Return float or None if not numeric / placeholder."""
    if _is_placeholder(v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        # Try removing currency symbols and commas
        try:
            cleaned = re.sub(r'[^\d.\-]', '', str(v))
            return float(cleaned) if cleaned else None
        except (TypeError, ValueError):
            return None


def split_tags(raw_tag: Any) -> list[str]:
    """
    Split a raw tag cell value into individual tag strings.

    Examples:
        "1/2/3"         → ["1", "2", "3"]
        "1,2,3"         → ["1", "2", "3"]
        "TAG-001"        → ["TAG-001"]
        "30-GV-146, 171, 169" → ["30-GV-146", "30-GV-171", "30-GV-169"]
        "A/B"           → ["A", "B"]
    """
    if _is_placeholder(raw_tag):
        return []

    raw = str(raw_tag).strip()

    # Check if splitting is needed
    if not _TAG_SEPARATORS.search(raw):
        return [raw] if raw else []

    # Split on separators
    parts = [p.strip() for p in _TAG_SEPARATORS.split(raw)]
    parts = [p for p in parts if p and not _is_placeholder(p)]

    if not parts:
        return [raw]

    # If parts after the first look like suffixes (short, no letters before dash),
    # inherit the prefix from the first part.
    # e.g. "30-GV-146, 171, 169" → first="30-GV-146", rest=["171","169"]
    # Apply prefix if rest-parts don't look like standalone tags
    first = parts[0]
    if len(parts) > 1:
        # Extract prefix: everything up to the last numeric segment
        prefix_match = re.match(r'^(.*?)(\d+)$', first)
        if prefix_match:
            prefix = prefix_match.group(1)
            result = [first]
            for p in parts[1:]:
                # If it's a plain number or short suffix, prepend prefix
                if re.match(r'^\d+$', p) or (not re.search(r'[A-Za-z]', p) and '-' not in p):
                    result.append(prefix + p)
                else:
                    result.append(p)
            return result

    return parts


class BaseParser(ABC):
    """Abstract base for all SPIR format parsers."""

    FORMAT_NAME: str = "BASE"

    @classmethod
    @abstractmethod
    def detect(cls, wb) -> bool:
        """Return True if this workbook matches this format."""
        ...

    @classmethod
    @abstractmethod
    def _extract_raw(cls, wb) -> list[dict]:
        """
        Extract raw data from workbook. Return list of dicts using
        field names from output_schema.OUTPUT_COLUMNS.
        Tags may still be multi-value strings — splitting happens in parse().
        """
        ...

    @classmethod
    def parse(cls, wb) -> list[dict]:
        """
        Full parse: extract raw rows, split tags, normalize.

        Returns:
            list[dict] — one dict per output row, using output_schema field names.
        """
        try:
            raw_rows = cls._extract_raw(wb)
        except Exception as exc:
            log.error("%s._extract_raw() failed: %s", cls.FORMAT_NAME, exc, exc_info=True)
            return []

        if not raw_rows:
            log.warning("%s returned 0 raw rows", cls.FORMAT_NAME)
            return []

        result: list[dict] = []
        for raw in raw_rows:
            tag_val = raw.get("tag")
            tags    = split_tags(tag_val)

            if not tags:
                # No tag — still emit the row with empty tag (don't drop data)
                tags = [None]

            for tag in tags:
                row = dict(raw)
                row["tag"]           = tag
                row["format_source"] = cls.FORMAT_NAME
                # Compute total_price if missing
                if row.get("total_price") is None:
                    qty   = clean_num(row.get("quantity"))
                    price = clean_num(row.get("unit_price"))
                    if qty is not None and price is not None:
                        row["total_price"] = round(qty * price, 4)
                result.append(row)

        log.info("%s: %d raw rows → %d output rows (after tag split)",
                 cls.FORMAT_NAME, len(raw_rows), len(result))
        return result

    @classmethod
    def _cv(cls, ws, row: int, col: int) -> str:
        """Read a cell as string, strip whitespace."""
        v = ws.cell(row, col).value
        return str(v).strip() if v is not None else ""

    @classmethod
    def _cn(cls, ws, row: int, col: int) -> float | None:
        """Read a cell as number."""
        v = ws.cell(row, col).value
        return clean_num(v)

    @classmethod
    def _find_header_row(cls, ws, keywords: list[str], scan_rows: int = 20) -> int | None:
        """
        Scan the first `scan_rows` rows for the one that best matches `keywords`.
        Returns 1-based row index or None.
        """
        best_row   = None
        best_score = 0

        for r in range(1, min(scan_rows + 1, ws.max_row + 1)):
            score = 0
            for c in range(1, min(ws.max_column + 1, 50)):
                cell_val = ws.cell(r, c).value
                if cell_val is None:
                    continue
                cell_lower = str(cell_val).lower()
                for kw in keywords:
                    if kw.lower() in cell_lower:
                        score += 1
                        break
            if score > best_score:
                best_score = score
                best_row   = r

        return best_row if best_score >= min(2, len(keywords)) else None
