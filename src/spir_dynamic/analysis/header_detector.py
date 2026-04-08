"""
Dynamic header row detection via keyword scoring.

Scans the first N rows of a worksheet, scores each row based on how many
recognized SPIR-related keywords it contains, and returns the best match.
Also extracts metadata from rows above the header and detects footer rows.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from spir_dynamic.utils.cell_utils import clean_str, looks_like_tag

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keywords that appear in SPIR data headers
# ---------------------------------------------------------------------------
HEADER_KEYWORDS: list[str] = [
    "description of parts",
    "description of part",
    "description",
    "part description",
    "item number",
    "item no",
    "item #",
    "equipment tag",
    "tag no",
    "tag number",
    "tag #",
    "total no. of identical",
    "identical parts fitted",
    "no. of identical",
    "qty identical",
    "quantity",
    "qty",
    "unit price",
    "price per unit",
    "unit cost",
    "total price",
    "currency",
    "manufacturer part",
    "mfr part",
    "part number",
    "part no",
    "part#",
    "manufacturer name",
    "manufacturer",
    "make",
    "supplier",
    "vendor",
    "unit of measure",
    "uom",
    "delivery time",
    "lead time",
    "sap number",
    "sap no",
    "classification",
    "drawing no",
    "dwg no",
    "material spec",
    # PHASE 3 FIX: Continuation sheet keywords
    "remarks",
    "mfr ser",
    "mfr ser'l",
    "serial number",
    "no. of units",
    "no of units",
    "parts per unit",
    "equip't",
]

# Keywords that indicate metadata in the header area (above the data header)
METADATA_KEYWORDS: dict[str, list[str]] = {
    "spir_no": ["spir number", "spir no", "spir ref", "spir #"],
    "equipment": ["equipment:"],
    # PHASE 4 FIX: Accept "manufacturer", "manufacturer:", "MANUFACTURER : VALUE" etc.
    "manufacturer": ["manufacturer"],
    # PHASE 4 FIX: Accept "supplier", "supplier:", "SUPPLIER : VALUE" etc.
    "supplier": ["supplier"],
    "project": ["project", "contract"],
}

# Footers that indicate end of data section
FOOTER_STARTS = (
    "project",
    "company",
    "engineering by",
    "reminder",
    "technical data",
    "note:",
    "notes:",
    "end of",
    "signature",
    "requisition",
    "prepared by",
    "checked by",
    "approved by",
    "revision",
)


def _score_row(ws, row_idx: int, max_col: int) -> int:
    """Score a row based on how many header keywords it contains."""
    score = 0
    for c in range(1, max_col + 1):
        v = ws.cell(row_idx, c).value
        if v is None:
            continue
        cell = str(v).lower().strip()
        for kw in HEADER_KEYWORDS:
            if kw in cell:
                score += 1
                break
    return score


def find_header_row(ws, scan_rows: int = 30) -> int | None:
    """
    Find the most keyword-rich row in the first `scan_rows` rows.

    Returns 1-based row index or None if no suitable row is found.
    """
    max_col = min(ws.max_column or 50, 60)
    best_score, best_row = 0, None
    best_signals: dict[str, Any] | None = None

    CRITICAL_PATTERNS = (
        "description",
        "item number",
        "item no",
        "quantity",
        "qty",
        "unit price",
        "price per unit",
        "unit cost",
        "total price",
        "total cost",
        "currency",
        "part number",
        "part no",
    )

    def _row_signals(row_idx: int) -> dict[str, Any]:
        keyword_cols: set[int] = set()
        keyword_count = 0
        critical_hits = 0
        non_empty = 0

        keyword_min_col = None
        keyword_max_col = None

        for c in range(1, max_col + 1):
            v = ws.cell(row_idx, c).value
            if v is None:
                continue
            s = str(v).lower().strip()
            if not s:
                continue
            non_empty += 1

            hit_kw = None
            for kw in HEADER_KEYWORDS:
                if kw in s:
                    hit_kw = kw
                    break
            if hit_kw:
                keyword_count += 1
                keyword_cols.add(c)
                keyword_min_col = c if keyword_min_col is None else min(keyword_min_col, c)
                keyword_max_col = c if keyword_max_col is None else max(keyword_max_col, c)

            for pat in CRITICAL_PATTERNS:
                if pat in s:
                    critical_hits += 1
                    break

        span_width = 0
        if keyword_min_col is not None and keyword_max_col is not None:
            span_width = keyword_max_col - keyword_min_col

        return {
            "keyword_count": keyword_count,
            "keyword_cols": len(keyword_cols),
            "critical_hits": critical_hits,
            "non_empty": non_empty,
            "span_width": span_width,
        }

    scan_limit = min(scan_rows + 1, (ws.max_row or 0) + 1)
    for r in range(1, scan_limit):
        signals = _row_signals(r)
        keyword_cols = int(signals["keyword_cols"])
        span_width = int(signals["span_width"])
        critical_hits = int(signals["critical_hits"])

        # Reject rows that look like mostly metadata:
        # - too few keyword columns
        # - keyword columns are too concentrated (small span)
        if keyword_cols < 2:
            continue
        if span_width < 3 and keyword_cols < 4:
            continue

        # PHASE 4 FIX: Check for ITEM NUMBER before candidate_ok filter
        # Rows with ITEM NUMBER should be accepted even with fewer keyword columns
        has_item_number = False
        for c in range(1, max_col + 1):
            v = ws.cell(r, c).value
            if v and "item" in str(v).lower() and "number" in str(v).lower():
                has_item_number = True
                break

        # Tabular header rows usually contain multiple critical patterns
        # spread across columns. Be tolerant but avoid picking rows like "Model" / "Serial".
        candidate_ok = False
        if critical_hits >= 2 and keyword_cols >= 3:
            candidate_ok = True
        elif critical_hits >= 1 and keyword_cols >= 4:
            candidate_ok = True
        # PHASE 4 FIX: Accept rows with ITEM NUMBER even with fewer keywords
        elif has_item_number and critical_hits >= 1 and keyword_cols >= 2:
            candidate_ok = True

        if not candidate_ok:
            continue

        # Weighted score:
        # - prefer more keyword columns
        # - prefer higher critical hits
        # - prefer wider span (tabular spread)
        # - small boost for non-empty cells
        score = (
            keyword_cols * 20
            + critical_hits * 10
            + span_width * 2
            + min(signals["non_empty"], 30) * 0.2
        )

        # PHASE 4 FIX: Reject rows that are clearly metadata/commercial rows
        # rather than data header rows. These rows have keywords like
        # "required on site date", "authority block", etc. but lack
        # "ITEM NUMBER" which is the definitive marker of a SPIR data header.
        _METADATA_ONLY_PATTERNS = (
            "required on site",
            "authority block",
            "ref indicator",
            "purchase by",
        )
        is_metadata_only = any(
            pat in str(ws.cell(r, c).value or "").lower()
            for c in range(1, max_col + 1)
            for pat in _METADATA_ONLY_PATTERNS
        )

        # Skip rows that are purely metadata (no ITEM NUMBER)
        if is_metadata_only and not has_item_number:
            continue

        # PHASE 4 FIX: Strong boost for ITEM NUMBER rows
        if has_item_number:
            score += 100

        if score > best_score:
            best_score, best_row = score, r
            best_signals = signals

    if best_row is not None:
        log.debug(
            "Header found at row %d (score=%.1f, signals=%s) in '%s'",
            best_row,
            best_score,
            best_signals,
            ws.title,
        )
        return best_row

    # PHASE 3 FIX: Fallback for continuation sheets with sparse headers.
    # Continuation sheets may only have "ITEM NUMBER" + "MFR SER NO." + "REMARKS"
    # in the header row. If we find a row with "ITEM NUMBER" and at least 2
    # other keyword columns, accept it as the header row.
    for r in range(1, scan_limit):
        signals = _row_signals(r)
        keyword_cols = int(signals["keyword_cols"])
        critical_hits = int(signals["critical_hits"])

        # Check if this row has "item number" specifically
        has_item_number = False
        for c in range(1, max_col + 1):
            v = ws.cell(r, c).value
            if v and "item" in str(v).lower() and "number" in str(v).lower():
                has_item_number = True
                break

        if has_item_number and keyword_cols >= 2 and critical_hits >= 1:
            log.debug(
                "Header found at row %d (continuation fallback, keyword_cols=%d, critical=%d) in '%s'",
                r, keyword_cols, critical_hits, ws.title,
            )
            return r

    # Fallback to old simplistic scoring if nothing passes filters
    for r in range(1, scan_limit):
        s = _score_row(ws, r, max_col)
        if s > best_score:
            best_score, best_row = s, r

    if best_score >= 2:
        log.debug("Header fallback found at row %d (score=%d) in '%s'", best_row, best_score, ws.title)
        return best_row

    log.debug("No header found in '%s' (best score=%d)", ws.title, best_score)
    return None


def find_metadata(ws, header_row: int | None) -> dict[str, Any]:
    """
    Extract metadata (SPIR NO, manufacturer, etc.) from the sheet.

    Scans all rows up to header_row (or first 10 rows) looking for
    label-value pairs. Labels are identified by METADATA_KEYWORDS.
    The value is in the next column(s) after the label.

    Scans ALL columns (up to 30) to find labels like "MANUFACTURER:" or
    "SPIR NUMBER:" which may appear in any column.
    """
    metadata: dict[str, Any] = {}
    scan_to = header_row if header_row else 10
    scan_to = min(scan_to, 15)
    max_col = min(ws.max_column or 30, 50)

    for r in range(1, scan_to + 1):
        for c in range(1, max_col + 1):
            v = ws.cell(r, c).value
            if v is None:
                continue
            cell = str(v).lower().strip()

            # Only match if the cell looks like a label (contains keyword
            # but is relatively short — not a data cell with coincidental match)
            if len(cell) > 80:
                continue

            # PHASE 5 FIX: Reject column header text that happens to contain
            # metadata keywords. E.g., "SUPPLIERS PART NUMBER (SEE NOTE 3)"
            # or "SUPPLIER/OCM NAME" are column headers, not metadata labels.
            _HEADER_PATTERNS = [
                "see note", "part number", "recommended by", "approved by",
                "checked by", "prepared by", "drawing", "material spec",
                "classification", "sap number", "unit price", "delivery time",
                "min/max", "stock level", "unit of measure",
                # Column headers that look like metadata labels
                "supplier/ocm", "ocm name", "supporting documents",
                "attachments only", "material certification",
            ]
            if any(pat in cell for pat in _HEADER_PATTERNS):
                continue

            for field, keywords in METADATA_KEYWORDS.items():
                if field in metadata:
                    continue
                for kw in keywords:
                    if kw in cell:
                        val = _extract_metadata_value(ws, r, c, max_col)
                        if val:
                            metadata[field] = val
                        break

    # Also try to detect SPIR number from cell patterns
    if "spir_no" not in metadata:
        spir_no = _detect_spir_number(ws, scan_to, max_col)
        if spir_no:
            metadata["spir_no"] = spir_no

    # Detect SPIR type by scanning for known type strings as VALUES
    if "spir_type" not in metadata:
        spir_type = _detect_spir_type(ws, scan_to, max_col)
        if spir_type:
            metadata["spir_type"] = spir_type

    return metadata


_SPIR_TYPE_MAP = {
    "spare parts for normal operation": "normal operating spares",
    "normal operating spares": "normal operating spares",
    "normal operating": "normal operating spares",
    "initial spare parts": "initial spares",
    "initial spare": "initial spares",
    "commissioning spare parts": "commissioning spare",
    "commissioning spare": "commissioning spare",
    "life cycle spare parts": "life cycle spares",
    "life cycle spare": "life cycle spares",
}


def _detect_spir_type(ws, scan_rows: int, max_col: int) -> str | None:
    """
    Detect which of the 4 SPIR types is ticked/selected.

    The 4 types are:
      1. Initial Spare Parts
      2. Normal Operating Spares (label: "Spare Parts for Normal Operation")
      3. Commissioning Spare Parts
      4. Life Cycle Spare Parts

    Detection mechanism: type labels appear in the header area.
    The "tick" is a numeric value > 0 in the rows immediately BELOW the label
    (rows 2-3 below), in the SAME column. Whichever type column has a positive
    value is selected.

    Returns None if no tick is found (e.g., files without the checkbox grid).
    """
    # Phase 1: Find ALL type labels with their (row, col) positions
    type_cells: list[tuple[int, int, str]] = []
    for r in range(1, min(scan_rows + 2, 15)):
        for c in range(1, max_col + 1):
            v = ws.cell(r, c).value
            if v is None:
                continue
            cl = str(v).lower().strip()
            for pattern, normalized in _SPIR_TYPE_MAP.items():
                if pattern in cl:
                    type_cells.append((r, c, normalized))
                    break

    if not type_cells:
        return None

    type_row = type_cells[0][0]  # all labels are in the same row
    type_cols = [(c, normalized) for _, c, normalized in type_cells]

    # Phase 2: Scan ONLY rows 2-3 below the labels for tick values.
    # Tick values appear immediately below the type labels in the header area.
    # Do NOT scan further down — data rows may have 0/1 values that are
    # NOT SPIR type ticks.
    for offset in range(2, 4):
        check_row = type_row + offset
        if check_row > (ws.max_row or 0):
            break

        col_vals: list[tuple[float, str]] = []
        all_numeric = True
        for c, normalized in type_cols:
            cv = ws.cell(check_row, c).value
            if cv is True:
                return normalized
            try:
                num_val = float(cv) if cv is not None else 0
                col_vals.append((num_val, normalized))
            except (ValueError, TypeError):
                all_numeric = False
                break

        if not all_numeric or not col_vals:
            continue

        # Check if this row has a tick pattern: exactly ONE > 0, rest = 0
        positive = [(v, n) for v, n in col_vals if v > 0]
        zeros = [v for v, _ in col_vals if v == 0]

        if len(positive) == 1 and len(zeros) >= 1:
            return positive[0][1]

    # Phase 3: No data-row tick found. Check for boolean True in same row
    for r, c, normalized in type_cells:
        for check_c in range(c + 1, min(c + 12, max_col + 1)):
            tv = ws.cell(r, check_c).value
            if tv is True:
                return normalized

    # No type ticked — return None (blank). Do NOT guess or default.
    return None

    # PHASE 4 FIX: If only ONE type label is found, use it directly.
    # Some SPIR files write the type as plain text (e.g., "Initial Spare Parts")
    # without a checkbox grid. In this case, there's only one type label present.
    unique_types = set(n for _, _, n in type_cells)
    if len(unique_types) == 1:
        return unique_types.pop()

    # PHASE 4 FIX: If multiple type labels are found in the same row as standalone
    # text (not checkbox labels), this is a text-only format. Detect this by checking
    # if ALL type cells contain ONLY the type text (not part of a larger label).
    # In text-only formats, the first type found is the selected one.
    all_standalone = True
    for r, c, normalized in type_cells:
        cell_val = str(ws.cell(r, c).value or "").strip()
        cell_lower = cell_val.lower()
        is_standalone = cell_lower in _SPIR_TYPE_MAP
        if not is_standalone:
            all_standalone = False
            break
    
    if all_standalone:
        # Text-only format — return the first type found
        return type_cells[0][2]

    # Phase 2: Find the tick row — the row below the type labels where
    # EXACTLY ONE type column has a value > 0 and the others are 0.
    # This distinguishes the tick row from column-numbering rows (where
    # ALL columns have positive values).
    type_row = type_cells[0][0]  # all labels are in the same row
    type_cols = [(c, normalized) for _, c, normalized in type_cells]

    for offset in range(2, 7):
        check_row = type_row + offset
        if check_row > (ws.max_row or 0):
            continue

        # Read values from ALL type columns at this row
        col_vals: list[tuple[float, str]] = []
        all_numeric = True
        for c, normalized in type_cols:
            cv = ws.cell(check_row, c).value
            if cv is True:
                return normalized
            try:
                num_val = float(cv) if cv is not None else 0
                col_vals.append((num_val, normalized))
            except (ValueError, TypeError):
                all_numeric = False
                break

        if not all_numeric or not col_vals:
            continue

        # Tick row: exactly ONE has value > 0, others are 0
        positive = [(v, n) for v, n in col_vals if v > 0]
        zeros = [v for v, _ in col_vals if v == 0]

        if len(positive) == 1 and len(zeros) >= 1:
            return positive[0][1]

    # PHASE 4 FIX: No tick row found. Check if this is a text-only format
    # (not a checkbox grid). In text-only formats, the type labels appear as
    # standalone text values without a grid structure. We detect this by checking
    # if the type cells have NO tick values (0/1) in the immediate rows below them.
    # Only check offset 2-3 (where checkbox ticks would be), not further down
    # where data rows with 0/1 values might exist.
    has_tick_below = False
    for c, _ in type_cols:
        for offset in range(2, 4):  # Only check immediate rows below
            check_row = type_row + offset
            if check_row > (ws.max_row or 0):
                continue
            cv = ws.cell(check_row, c).value
            try:
                num = float(cv) if cv is not None else -1
                # Tick values are 0 or 1, not column numbers (which are typically > 1)
                if num in (0, 1):
                    has_tick_below = True
                    break
            except (ValueError, TypeError):
                pass
        if has_tick_below:
            break

    # If no tick values below type labels, this is a text-only format.
    # The selected type is the one that appears as a standalone value.
    if not has_tick_below:
        for r, c, normalized in type_cells:
            cell_val = str(ws.cell(r, c).value or "").strip()
            cell_lower = cell_val.lower()
            # Use _SPIR_TYPE_MAP directly for proper capitalization
            for pattern, norm in _SPIR_TYPE_MAP.items():
                if cell_lower == pattern:
                    return norm

    # Phase 3: No data-row tick found. Check for boolean True in same row
    # (fallback for non-standard formats — some files use checkboxes)
    for r, c, normalized in type_cells:
        for check_c in range(c + 1, min(c + 12, max_col + 1)):
            tv = ws.cell(r, check_c).value
            if tv is True:
                return normalized

    # Phase 4: No type ticked — return None (no default; the SPIR type
    # must be explicitly ticked in the input file)
    return None


def _extract_metadata_value(ws, row: int, label_col: int, max_col: int) -> str | None:
    """Extract the value associated with a metadata label."""
    # Check if value is in the same cell after a colon
    # PHASE 4 FIX: Handle "MANUFACTURER : ALLEN BRADLY" format
    cell_val = str(ws.cell(row, label_col).value or "")
    if ":" in cell_val:
        after_colon = cell_val.split(":", 1)[1].strip()
        # Strip leading colon if present (e.g., ": VEN-4142-...")
        after_colon = after_colon.lstrip(":").strip()
        if after_colon:
            return _sanitize_metadata(after_colon)

    # Check the next several columns for a non-empty value (up to 8 cols right)
    for c in range(label_col + 1, min(label_col + 9, max_col + 1)):
        v = ws.cell(row, c).value
        if v is not None:
            s = str(v).strip()
            # Strip leading colon if present (e.g., ": LARSEN & TOUBRO")
            s = s.lstrip(":").strip()
            if s and s.lower() not in ("", "-", "n/a"):
                return _sanitize_metadata(s)

    return None


def _sanitize_metadata(val: str) -> str:
    """Remove embedded newlines and excess whitespace from metadata values."""
    import re
    # Excel cells can have embedded newlines (Alt+Enter); collapse to single space
    return re.sub(r"[\r\n]+", " ", val).strip()


def _detect_spir_number(ws, scan_rows: int, max_col: int) -> str | None:
    """Try to find a SPIR number by pattern matching cell values."""
    # Common SPIR number pattern: alphanumeric with hyphens, at least 3 segments
    spir_pattern = re.compile(r"[A-Z0-9]{2,}-[A-Z0-9]{2,}-[A-Z0-9]", re.IGNORECASE)

    for r in range(1, scan_rows + 1):
        for c in range(1, max_col + 1):
            v = ws.cell(r, c).value
            if v is None:
                continue
            s = str(v).strip()

            # Check if cell to the left contains "spir"
            if c > 1:
                left = str(ws.cell(r, c - 1).value or "").lower()
                if "spir" in left and spir_pattern.search(s):
                    # PHASE 4 FIX: Strip leading colon if present (e.g., ": VEN-4142-...")
                    return s.lstrip(":").strip()

            # Check if cell itself looks like "SPIR NO: XXX-YYY-ZZZ"
            if "spir" in s.lower() and ":" in s:
                after = s.split(":", 1)[1].strip()
                # PHASE 4 FIX: Strip leading colon if present (e.g., ": VEN-4142-...")
                after = after.lstrip(":").strip()
                if spir_pattern.search(after):
                    return after

    return None


def find_data_end(ws, header_row: int) -> int:
    """
    Scan from header_row downward to find where data ends (footer rows).
    Returns the 1-based row index of the last data row.
    """
    max_row = ws.max_row or 0
    max_col = min(ws.max_column or 20, 30)

    for r in range(header_row + 1, max_row + 1):
        # Check for footer markers
        for c in range(1, min(6, max_col + 1)):
            v = ws.cell(r, c).value
            if v is None:
                continue
            dl = str(v).lower().strip()
            if any(dl.startswith(f) for f in FOOTER_STARTS):
                return r - 1

        # Check for completely blank row (5+ consecutive blank rows = end)
        row_vals = [ws.cell(r, c).value for c in range(1, min(max_col + 1, 30))]
        if all(v is None or str(v).strip() == "" for v in row_vals):
            # Look ahead: if next 4 rows are also blank, stop here
            all_blank = True
            for check_r in range(r + 1, min(r + 5, max_row + 1)):
                check_vals = [ws.cell(check_r, c).value for c in range(1, min(max_col + 1, 15))]
                if not all(v is None or str(v).strip() == "" for v in check_vals):
                    all_blank = False
                    break
            if all_blank:
                return r - 1

    return max_row


def is_footer_row(desc: str) -> bool:
    """Check if a description value looks like a footer marker."""
    dl = (desc or "").lower().strip()
    return any(dl.startswith(f) for f in FOOTER_STARTS)
