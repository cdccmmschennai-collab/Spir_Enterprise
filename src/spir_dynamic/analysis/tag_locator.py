"""
Dynamic tag location detection.

Determines WHERE tags live within a sheet by analyzing actual cell content,
not sheet names or format assumptions. This is the core innovation replacing
the 8-format parser system.

Detection priority:
  1. TAG_COLUMN   — A dedicated column with multiple distinct tag values
  2. COLUMN_HEADERS — Multiple tag-like values in the same row (top area)
  3. ROW_HEADERS  — Tag-like values as row labels in column A
  4. GLOBAL_TAG   — Single tag-like value in the metadata area
  5. NONE         — No tags detected
"""
from __future__ import annotations

import logging
import re
from typing import Any

from spir_dynamic.utils.cell_utils import looks_like_tag, TAG_PATTERN, is_placeholder
from spir_dynamic.models.sheet_profile import TagLayout

log = logging.getLogger(__name__)

# Minimum distinct tag values to classify as TAG_COLUMN
_MIN_TAG_COLUMN_VALUES = 2

# Minimum columns with tag-like values to classify as COLUMN_HEADERS
_MIN_TAG_COLUMNS = 2

# Row range to scan for column-header tags
# PHASE 4 FIX: Increased from 8 to 15 to handle files where headers
# appear at row 10-11 (e.g., files 14, 15, 16 with different layouts)
_TAG_HEADER_SCAN_ROWS = 15

# Columns to scan for row-header tags
_TAG_ROW_SCAN_COL = 1  # Column A


class TagLocationResult:
    """Result of tag location detection."""

    def __init__(
        self,
        layout: TagLayout,
        tag_columns: list[int] | None = None,
        tag_rows: list[int] | None = None,
        tag_column_index: int | None = None,
        global_tag: str | None = None,
        confidence: float = 0.0,
    ):
        self.layout = layout
        self.tag_columns = tag_columns or []
        self.tag_rows = tag_rows or []
        self.tag_column_index = tag_column_index
        self.global_tag = global_tag
        self.confidence = confidence


def locate_tags(
    ws,
    header_row: int | None,
    column_map: dict[str, int],
) -> TagLocationResult:
    """
    Detect where tags are located in a worksheet.

    Tries each detection method in priority order and returns the
    first confident match.
    """
    # 1. Check for a dedicated tag column with multiple values
    result = _check_tag_column(ws, header_row, column_map)
    if result:
        return result

    # 2. Check for tags as column headers (matrix layout)
    result = _check_column_headers(ws, header_row)
    if result:
        return result

    # 3. Check for tags as row headers (annexure/transposed layout)
    result = _check_row_headers(ws, header_row)
    if result:
        return result

    # 4. Check for a single global tag in the metadata area
    result = _check_global_tag(ws, header_row)
    if result:
        return result

    # 5. No tags found
    return TagLocationResult(layout=TagLayout.NONE, confidence=0.0)


def _check_tag_column(
    ws,
    header_row: int | None,
    column_map: dict[str, int],
) -> TagLocationResult | None:
    """
    Check if there's a dedicated tag column with multiple distinct tag values.
    This is the most common layout: a "Tag No" column alongside item data.
    """
    tag_col = column_map.get("tag")
    if tag_col is None:
        return None

    start_row = (header_row + 1) if header_row else 2
    max_row = min((ws.max_row or 0), start_row + 200)  # sample first 200 data rows

    distinct_tags: set[str] = set()
    for r in range(start_row, max_row + 1):
        v = ws.cell(r, tag_col).value
        if v is not None:
            s = str(v).strip()
            if s and not is_placeholder(s):
                distinct_tags.add(s.upper())

    if len(distinct_tags) >= _MIN_TAG_COLUMN_VALUES:
        # High confidence if values match tag patterns
        tag_like_count = sum(1 for t in distinct_tags if looks_like_tag(t))
        confidence = 0.9 if tag_like_count >= 1 else 0.6

        log.debug(
            "TAG_COLUMN detected in '%s' col %d: %d distinct tags",
            ws.title, tag_col, len(distinct_tags),
        )
        return TagLocationResult(
            layout=TagLayout.TAG_COLUMN,
            tag_column_index=tag_col,
            confidence=confidence,
        )

    # Even a single tag value in the column counts if it looks like a real tag
    if len(distinct_tags) == 1:
        tag_val = next(iter(distinct_tags))
        if looks_like_tag(tag_val):
            return TagLocationResult(
                layout=TagLayout.TAG_COLUMN,
                tag_column_index=tag_col,
                confidence=0.5,
            )

    return None


def _check_column_headers(
    ws,
    header_row: int | None,
) -> TagLocationResult | None:
    """
    Check if tags appear as column headers in the top rows.
    This is the matrix layout where each tag has its own column with qty values.
    """
    max_col = min(ws.max_column or 50, 80)
    scan_rows = min(_TAG_HEADER_SCAN_ROWS, (ws.max_row or 0))

    # PRIORITY: Look for "EQUIPMENT TAG" label in rows 1-2.
    # Every SPIR file has this label — columns to its RIGHT are tag columns.
    # This works regardless of tag format (no TAG_PATTERN needed).
    _TAG_LABEL_KWS = ["tag no", "tag number", "equip"]
    _ANNEXURE_PAT = re.compile(
        r"(?i)(?:refer\s+)?annexure[\s\-_]*(?:\([^)]*\)[\s\-_]*)?(?:\d+|[IVX]+)\b"
    )
    for r in range(1, min(3, (ws.max_row or 0) + 1)):
        for c in range(1, min(5, max_col + 1)):
            v = ws.cell(r, c).value
            if v is None:
                continue
            vl = str(v).lower().strip()
            if any(kw in vl for kw in _TAG_LABEL_KWS):
                # Found the label — scan columns to the right for tag values.
                # Stop at section breaks or after 3+ consecutive empty columns.
                tag_cols: list[int] = []
                empty_streak = 0
                for tc in range(c + 1, max_col + 1):
                    tv = ws.cell(r, tc).value
                    if tv is None or is_placeholder(tv) or str(tv).strip() == "_":
                        empty_streak += 1
                        if empty_streak >= 3:
                            break  # too many empties = end of tag section
                        continue
                    empty_streak = 0
                    s = str(tv).strip()
                    # PHASE 2 FIX: Allow longer values if they look like
                    # comma/semicolon-separated tags (e.g., 10 tags in one cell).
                    # A long value with tag-like patterns and separators is valid.
                    looks_like_packed_tags = (
                        len(s) > 50
                        and re.search(r"[A-Z0-9]{2,}[-/][A-Z0-9]", s, re.IGNORECASE)
                        and re.search(r"[,;/|]", s)
                    )
                    # Stop at long text (SPIR title, notes) or section breaks
                    # but allow packed tag columns
                    if (len(s) > 50 and not looks_like_packed_tags
                        or "spare parts" in s.lower()
                        or "note" in s.lower()
                        or "interchangeability" in s.lower()
                        or "spir" in s.lower()):
                        break
                    tag_cols.append(tc)
                if len(tag_cols) >= _MIN_TAG_COLUMNS:
                    log.debug(
                        "COLUMN_HEADERS detected via label '%s' in row %d: %d tag columns %s",
                        str(v).strip(), r, len(tag_cols), tag_cols,
                    )
                    return TagLocationResult(
                        layout=TagLayout.COLUMN_HEADERS,
                        tag_columns=tag_cols,
                        confidence=0.95,
                    )
                # PHASE 2 FIX: Accept a single tag column if it contains
                # multiple comma/semicolon-separated tags (e.g., 10 tags in one cell).
                # This handles continuation sheets where all tags are packed into
                # a single column header cell.
                # PHASE 4 FIX: Also accept alphanumeric equipment identifiers
                # without separators (e.g., "18421LP0004", "3014LJ1") which are
                # valid single-tag SPIR files.
                # PHASE 5 FIX: Also accept annexure references (e.g., "Annexure I")
                # as valid single-column tag columns. The actual tags come from
                # the referenced annexure sheet.
                elif len(tag_cols) == 1:
                    single_val = ws.cell(r, tag_cols[0]).value
                    if single_val:
                        sv = str(single_val).strip()
                        has_multiple_tags = (
                            re.search(r"[,;/|]", sv)
                            and re.search(r"[A-Z0-9]{1,}[-/][A-Z0-9]", sv, re.IGNORECASE)
                        )
                        # PHASE 4 FIX: Accept alphanumeric equipment identifiers
                        # without separators (e.g., "18421LP0004", "3014LJ1")
                        is_equip_id = bool(re.match(r"^[A-Z0-9]{6,}$", sv, re.IGNORECASE))
                        # PHASE 5 FIX: Accept annexure references
                        is_annexure_ref = bool(_ANNEXURE_PAT.match(sv))
                        if has_multiple_tags or is_equip_id or is_annexure_ref:
                            log.debug(
                                "COLUMN_HEADERS detected via label '%s' in row %d: 1 tag column %s (packed=%s, equip_id=%s, annexure_ref=%s)",
                                str(v).strip(), r, tag_cols, has_multiple_tags, is_equip_id, is_annexure_ref,
                            )
                            return TagLocationResult(
                                layout=TagLayout.COLUMN_HEADERS,
                                tag_columns=tag_cols,
                                confidence=0.80,
                            )

    # Fallback: scan rows for tag-like column headers using TAG_PATTERN
    best_row = None
    best_cols: list[int] = []
    best_count = 0

    for r in range(1, scan_rows + 1):
        if r == header_row:
            continue

        tag_cols: list[int] = []
        for c in range(1, max_col + 1):
            v = ws.cell(r, c).value
            if v is None:
                continue
            sv = str(v).strip()
            if looks_like_tag(sv) or re.match(
                r"(?i)(?:refer\s+)?annexure[\s\-_]*(?:\([^)]*\)[\s\-_]*)?\d+", sv
            ):
                tag_cols.append(c)

        if len(tag_cols) > best_count:
            best_count = len(tag_cols)
            best_row = r
            best_cols = tag_cols

    if best_count >= _MIN_TAG_COLUMNS:
        # Filter out isolated columns that are far from the main cluster
        # Tags in SPIR matrices are in consecutive/near-consecutive columns
        best_cols = _filter_tag_cluster(best_cols)

        if len(best_cols) >= _MIN_TAG_COLUMNS:
            log.debug(
                "COLUMN_HEADERS detected in '%s' row %d: %d tag columns %s",
                ws.title, best_row, len(best_cols), best_cols,
            )
            return TagLocationResult(
                layout=TagLayout.COLUMN_HEADERS,
                tag_columns=best_cols,
                confidence=0.85,
            )

    return None


def _filter_tag_cluster(cols: list[int], max_gap: int = 4) -> list[int]:
    """
    Filter tag columns to keep only the main contiguous cluster.
    Isolated columns far from the cluster (like SPIR numbers in col 25)
    are filtered out.
    """
    if len(cols) <= 2:
        return cols

    sorted_cols = sorted(cols)

    # Find the largest cluster of columns with gaps <= max_gap
    clusters: list[list[int]] = [[sorted_cols[0]]]
    for c in sorted_cols[1:]:
        if c - clusters[-1][-1] <= max_gap:
            clusters[-1].append(c)
        else:
            clusters.append([c])

    # Return the largest cluster
    largest = max(clusters, key=len)
    return largest


def _check_row_headers(
    ws,
    header_row: int | None,
) -> TagLocationResult | None:
    """
    Check if tags appear as row labels (typically in column A).
    This is the annexure/transposed layout.
    """
    start_row = (header_row + 1) if header_row else 2
    max_row = min((ws.max_row or 0), start_row + 100)
    max_col = min(ws.max_column or 20, 30)

    tag_rows: list[int] = []
    for r in range(start_row, max_row + 1):
        v = ws.cell(r, _TAG_ROW_SCAN_COL).value
        if v is not None and looks_like_tag(v):
            # Verify there's data to the right
            has_data = False
            for c in range(2, max_col + 1):
                cv = ws.cell(r, c).value
                if cv is not None and str(cv).strip():
                    has_data = True
                    break
            if has_data:
                tag_rows.append(r)

    if len(tag_rows) >= _MIN_TAG_COLUMN_VALUES:
        log.debug(
            "ROW_HEADERS detected in '%s': %d tag rows",
            ws.title, len(tag_rows),
        )
        return TagLocationResult(
            layout=TagLayout.ROW_HEADERS,
            tag_rows=tag_rows,
            confidence=0.75,
        )

    return None


def _check_global_tag(
    ws,
    header_row: int | None,
) -> TagLocationResult | None:
    """
    Check if there's a single tag-like value in the metadata/header area.
    The entire sheet belongs to this one tag.
    """
    scan_to = (header_row - 1) if header_row else 8
    scan_to = min(scan_to, 10)
    max_col = min(ws.max_column or 20, 30)

    found_tags: list[str] = []
    for r in range(1, scan_to + 1):
        for c in range(1, max_col + 1):
            v = ws.cell(r, c).value
            if v is not None and looks_like_tag(v):
                tag_val = str(v).strip()
                # Don't count it if the cell also has other text
                if len(tag_val) < 30:
                    found_tags.append(tag_val)

    # We want exactly one unique tag (or very few tags that look like one thing)
    unique_tags = list(set(found_tags))
    if len(unique_tags) == 1:
        log.debug(
            "GLOBAL_TAG detected in '%s': %s",
            ws.title, unique_tags[0],
        )
        return TagLocationResult(
            layout=TagLayout.GLOBAL_TAG,
            global_tag=unique_tags[0],
            confidence=0.6,
        )

    return None
