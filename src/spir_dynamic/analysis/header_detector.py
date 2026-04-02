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
]

# Keywords that indicate metadata in the header area (above the data header)
METADATA_KEYWORDS: dict[str, list[str]] = {
    "spir_no": ["spir number", "spir no", "spir ref", "spir #"],
    "equipment": ["equipment:"],
    "manufacturer": ["manufacturer:"],
    "supplier": ["supplier:"],
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

    Returns 1-based row index or None if no row scores >= 2.
    """
    max_col = min(ws.max_column or 50, 60)
    best_score, best_row = 0, None

    for r in range(1, min(scan_rows + 1, (ws.max_row or 0) + 1)):
        s = _score_row(ws, r, max_col)
        if s > best_score:
            best_score, best_row = s, r

    if best_score >= 2:
        log.debug("Header found at row %d (score=%d) in '%s'", best_row, best_score, ws.title)
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
    "spare parts for normal operation": "Normal Operating Spares",
    "normal operating spares": "Normal Operating Spares",
    "normal operating": "Normal Operating Spares",
    "initial spare parts": "Initial Spare Parts",
    "initial spare": "Initial Spare Parts",
    "commissioning spare parts": "Commissioning Spare Parts",
    "commissioning spare": "Commissioning Spare Parts",
    "life cycle spare parts": "Life Cycle Spare Parts",
    "life cycle spare": "Life Cycle Spare Parts",
}


def _detect_spir_type(ws, scan_rows: int, max_col: int) -> str | None:
    """
    Detect which of the 4 SPIR types is ticked/selected.

    The 4 types are:
      1. Initial Spare Parts
      2. Normal Operating Spares (label: "Spare Parts for Normal Operation")
      3. Commissioning Spare Parts
      4. Life Cycle Spare Parts

    Detection mechanism: type labels appear in the header area (rows 3-7).
    The "tick" is a numeric value > 0 in the data rows BELOW the label,
    in the SAME column. Whichever type column has a positive value is selected.

    Falls back to "Normal Operating Spares" if no tick is found.
    """
    # Phase 1: Find ALL type labels with their (row, col) positions
    type_cells: list[tuple[int, int, str]] = []
    for r in range(1, min(scan_rows + 2, 12)):
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
    cell_val = str(ws.cell(row, label_col).value or "")
    if ":" in cell_val:
        after_colon = cell_val.split(":", 1)[1].strip()
        if after_colon:
            return _sanitize_metadata(after_colon)

    # Check the next several columns for a non-empty value (up to 8 cols right)
    for c in range(label_col + 1, min(label_col + 9, max_col + 1)):
        v = ws.cell(row, c).value
        if v is not None:
            s = str(v).strip()
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
                    return s

            # Check if cell itself looks like "SPIR NO: XXX-YYY-ZZZ"
            if "spir" in s.lower() and ":" in s:
                after = s.split(":", 1)[1].strip()
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
