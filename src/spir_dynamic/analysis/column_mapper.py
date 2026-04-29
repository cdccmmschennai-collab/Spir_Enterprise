"""
Dynamic column mapping — maps header cells to logical field names.

Scans the header row and matches cell text against keyword lists to determine
which column contains which data field. Handles merged cells and multi-row headers.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable

from spir_dynamic.utils.cell_utils import clean_num, is_placeholder

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Field keywords: logical field name -> list of header text patterns
# ---------------------------------------------------------------------------
# The canonical defaults below are used as fallback when keywords.yaml is absent.
# At runtime, _get_field_keywords() loads from config/keywords.yaml instead.
FIELD_KEYWORDS: dict[str, list[str]] = {
    "tag": [
        "equipment tag", "equip tag", "tag no", "tag number",
        "tag #", "equip't", "tag",
    ],
    "description": [
        "description of parts", "description of part",
        "description", "desc of part", "part description",
        "item description", "nomenclature", "part name",
        # Continuation sheets use "REMARKS" for description
        "remarks",
    ],
    "quantity": [
        "total no. of identical", "identical parts fitted",
        "no. of identical", "qty identical", "quantity",
        "qty", "no of parts", "nos", "number",
    ],
    "item_number": [
        "item number", "item no", "item #", "s/n", "sl no",
        "item", "sr no", "seq no",
    ],
    "unit_price": [
        "unit price", "price per unit", "unit cost", "price",
    ],
    "total_price": [
        "total price", "total cost", "extended price",
    ],
    "currency": [
        "currency",
    ],
    "part_number": [
        "manufacturer part", "mfr part", "part number", "part no",
        "part#", "vendor part", "supplier part",
    ],
    "manufacturer": [
        "manufacturer", "mfr",
    ],
    "supplier": [
        "supplier/ocm", "supplier ocm", "supplier name",
        "ocm name", "vendor", "vendor name", "ocm",
    ],
    "uom": [
        "unit of measure", "uom", "unit", "ea", "each",
    ],
    "delivery_weeks": [
        "delivery time", "lead time", "delivery",
    ],
    "sap_number": [
        "sap number", "sap no", "sap #", "material number",
    ],
    "classification": [
        "classification", "class of part", "spare type",
    ],
    "dwg_no": [
        "drawing no", "dwg no", "drawing number",
    ],
    "material_spec": [
        "material spec", "material specification",
    ],
    "model": [
        "model", "eqpt model",
    ],
    "serial": [
        "serial", "sr no", "serial no", "mfr ser", "mfr ser no",
    ],
    "eqpt_qty": [
        "eqpt qty", "equipment qty", "no of eqpt",
    ],
    "min_max": [
        "min max", "min/max", "stock level",
    ],
}


def _get_field_keywords() -> dict[str, list[str]]:
    """Return field keywords from config/keywords.yaml, falling back to defaults."""
    try:
        from spir_dynamic.app.config import load_keywords
        kw = load_keywords().get("field_keywords")
        if kw:
            return kw
    except Exception:
        pass
    return FIELD_KEYWORDS


def map_headers(
    ws,
    header_row: int,
    *,
    min_score: int = 30,
    sample_rows: int = 10,
) -> dict[str, int]:
    """
    Map header row cells to logical field names.

    Returns {field_name: 1-based_column_index} for all recognized columns.
    """
    max_col = min(ws.max_column or 50, 80)

    header_rows_to_consider: list[int] = [header_row]
    if header_row > 1:
        # PHASE 5 FIX: Only include header_row - 1 if it looks like a real header,
        # not a reminder/banner row or a commercial sub-header row.
        # Reminder rows contain keywords like "vendor", "supplier", "contractor"
        # in a long banner text. Commercial sub-headers are rows that only have
        # unit_price/delivery/qty keywords but no item_number/description.
        prev_row = header_row - 1
        if _row_looks_like_header(ws, prev_row, max_col):
            # Check if prev_row is a commercial-only sub-header (no item/desc keywords)
            has_core_header = False
            core_keywords = {"item_number", "description", "part_number", "dwg_no"}
            for c in range(1, max_col + 1):
                v = ws.cell(prev_row, c).value
                if v:
                    s = str(v).lower().strip()
                    for field in core_keywords:
                        for kw in _get_field_keywords().get(field, []):
                            if kw in s:
                                has_core_header = True
                                break
            if has_core_header:
                header_rows_to_consider.append(prev_row)
    # PHASE 4 FIX: Some SPIR files have sub-headers BELOW the main header row.
    # E.g., row 11 has "ITEM NUMBER", row 12 has "DESCRIPTION OF PARTS",
    # "DRAWING Number", "PART NUMBER". Scan up to 3 rows below to catch these.
    # Allow skipping transitional rows (0-1 keyword hits) between header rows.
    max_row = ws.max_row or 0
    skip_count = 0
    for offset in range(1, 4):
        r = header_row + offset
        if r > max_row:
            break
        if _row_looks_like_header(ws, r, max_col):
            header_rows_to_consider.append(r)
            skip_count = 0  # Reset skip counter after finding a header
        elif skip_count < 1:
            # Allow one transitional row between headers
            skip_count += 1
        else:
            break  # Two non-header rows in a row = stop

    # Precompute header text variants per column so multi-row headers work
    # even if `find_header_row()` is off by 1.
    header_text_by_col: dict[int, list[str]] = {}
    for c in range(1, max_col + 1):
        texts: list[str] = []
        for r in header_rows_to_consider:
            t = _get_header_text(ws, r, c)
            if t:
                texts.append(t)
        if texts:
            header_text_by_col[c] = texts

    # For each column, score each field using the best-matching header text.
    candidates: list[tuple[int, str, int]] = []  # (score, field, col)
    best_by_col: dict[int, tuple[int, str]] = {}  # col -> (score, field)

    field_keywords = _get_field_keywords()
    for c, texts in header_text_by_col.items():
        col_best_score = 0
        col_best_field: str | None = None
        for field, keywords in field_keywords.items():
            score = _score_field_against_header_texts(texts, keywords)
            if score > col_best_score:
                col_best_score = score
                col_best_field = field

        if col_best_field is not None and col_best_score >= min_score:
            best_by_col[c] = (col_best_score, col_best_field)

        # Keep top candidates above threshold (for validation/backoff later)
        scored_fields = [
            (field, _score_field_against_header_texts(texts, keywords))
            for field, keywords in field_keywords.items()
        ]
        scored_fields.sort(key=lambda x: x[1], reverse=True)
        for field, score in scored_fields[:3]:
            if score >= min_score:
                candidates.append((score, field, c))

    if not candidates:
        return {}

    # Validation: reject mappings that are clearly incompatible with value types.
    # This reduces wrong numeric/text assignments even when header scoring is noisy.
    data_row_start = header_row + 1
    data_row_end = min((ws.max_row or 0), header_row + sample_rows)
    sample_range = range(data_row_start, data_row_end + 1)

    numeric_like_fields = {"quantity", "unit_price", "total_price", "eqpt_qty", "min_max"}
    text_heavy_fields = {"description"}

    col_stats: dict[int, dict[str, float]] = {}
    for _, _, c in candidates:
        if c in col_stats:
            continue
        col_stats[c] = _compute_column_stats(ws, c, sample_range)

    # PHASE 5 FIX: Reject columns whose header text is a recommendation/approval
    # label rather than a data column. E.g., "RECOMMENDED BY .MANUFACTURER" is
    # not a data column — it's a signature/recommendation field.
    _REJECT_PATTERNS = [
        "RECOMMENDED BY", "APPROVED BY", "CHECKED BY", "PREPARED BY",
    ]
    def _is_rejection_header(col: int) -> bool:
        texts = header_text_by_col.get(col, [])
        combined = " ".join(texts).upper()
        return any(pat in combined for pat in _REJECT_PATTERNS)

    def _candidate_passes_validation(field: str, col: int) -> bool:
        # Always reject recommendation/approval columns regardless of sample count
        if _is_rejection_header(col):
            return False

        stats = col_stats.get(col) or {}
        numeric_rate = stats.get("numeric_rate", 0.0)
        integer_rate = stats.get("integer_rate", 0.0)
        alpha_rate = stats.get("alpha_rate", 0.0)

        # If we don't have enough samples, be lenient (layout may have sparse data).
        sample_count = int(stats.get("sample_count", 0))
        if sample_count < 3:
            return True

        if field in numeric_like_fields:
            # Ensure the column is mostly numeric-like.
            if numeric_rate < 0.55:
                return False
            # Quantity-like fields are typically integers in SPIR.
            if field in {"quantity", "eqpt_qty"} and integer_rate < 0.45:
                return False
            return True

        if field in text_heavy_fields:
            # Descriptions are expected to have alphabetic content.
            return alpha_rate >= 0.45

        return True

    # Conflict resolution:
    # - If multiple fields match the same column, keep the highest-scoring match.
    # - Enforce uniqueness: one field -> one column.
    # Greedy works well because we validate and candidates are top-ranked.
    candidates = [
        (score, field, col)
        for (score, field, col) in candidates
        if _candidate_passes_validation(field, col)
    ]

    # PHASE 4 FIX: When header text contains both "manufacturer" and "serial",
    # prefer "serial" mapping (e.g., "Manufacturer Serial No" -> serial, not manufacturer).
    # When header contains both "manufacturer" and "model", prefer "model" mapping
    # (e.g., "Manufacturer Model no" -> model, not manufacturer).
    # Use a large enough bonus to overcome the base score difference.
    def _apply_tiebreakers(score: int, field: str, col: int) -> int:
        texts = header_text_by_col.get(col, [])
        combined = " ".join(texts).lower()
        if field == "serial" and "manufacturer" in combined and "serial" in combined:
            return score + 50  # Prefer serial over manufacturer for "Manufacturer Serial No"
        if field == "model" and "manufacturer" in combined and "model" in combined:
            return score + 50  # Prefer model over manufacturer for "Manufacturer Model no"
        return score

    candidates = [
        (_apply_tiebreakers(score, field, col), field, col)
        for (score, field, col) in candidates
    ]
    candidates.sort(key=lambda x: x[0], reverse=True)

    used_fields: set[str] = set()
    used_cols: set[int] = set()
    mapping: dict[str, int] = {}

    for score, field, col in candidates:
        if field in used_fields:
            continue
        if col in used_cols:
            continue
        if score < min_score:
            continue
        mapping[field] = col
        used_fields.add(field)
        used_cols.add(col)

    if mapping:
        debug_best = [
            (field, col, *best_by_col.get(col, (0, "")))
            for field, col in mapping.items()
        ]
        debug_best_sorted = sorted(debug_best, key=lambda x: x[1])
        log.debug(
            "Column scoring (best-per-mapped-col) for '%s' header_row=%d: %s",
            ws.title,
            header_row,
            [
                {"field": f, "col": col, "best_score": score}
                for f, col, score, _ in debug_best_sorted
            ],
        )
        log.debug(
            "Final column mapping for '%s' header_row=%d: %s",
            ws.title,
            header_row,
            {k: v for k, v in sorted(mapping.items(), key=lambda x: x[1])},
        )

    return mapping


def _get_header_text(ws, row: int, col: int) -> str | None:
    """
    Get header text from a cell, handling merged cells.
    For merged cells, returns the value from the top-left cell of the merge range.
    Safe for read-only worksheets (which don't have merged_cells).
    """
    cell = ws.cell(row, col)
    if cell.value is not None:
        return str(cell.value).strip()

    # Check if this cell is part of a merged range (not available in read-only mode)
    try:
        for merge_range in ws.merged_cells.ranges:
            if cell.coordinate in merge_range:
                top_left = ws.cell(merge_range.min_row, merge_range.min_col)
                if top_left.value is not None:
                    return str(top_left.value).strip()
                break
    except AttributeError:
        pass  # read-only worksheets don't have merged_cells

    return None


def _normalize_header_text(text: str) -> str:
    # Normalize whitespace: "UNIT  OF MEASURE" → "unit of measure"
    return re.sub(r"\s+", " ", str(text).lower().strip())


def _keyword_score_for_cell(cell_lower: str, keyword: str) -> int:
    """
    Score how well a single keyword matches a header cell.

    Strong match: exact phrase hits (multi-word patterns).
    Weak match: short generic tokens (e.g. "price", "qty") inside the cell.
    """
    kw = _normalize_header_text(keyword)
    if not kw:
        return 0

    if cell_lower == kw:
        return 100

    if kw in cell_lower:
        # Multi-word phrases are typically more specific.
        if " " in kw or len(kw) >= 8:
            return 80
        # Short tokens can match multiple fields; keep them low/moderate.
        if re.search(rf"\b{re.escape(kw)}\b", cell_lower):
            return 35
        return 20

    # Word-boundary match for short tokens (helps with "EQPT QTY", "TOTAL PRICE").
    if " " not in kw and len(kw) <= 6:
        if re.search(rf"\b{re.escape(kw)}\b", cell_lower):
            return 35
    return 0


def _score_field_against_header_texts(header_texts: Iterable[str], keywords: list[str]) -> int:
    """Return the max score of any keyword against any provided header text."""
    best = 0
    for t in header_texts:
        cell_lower = _normalize_header_text(t)
        for kw in keywords:
            best = max(best, _keyword_score_for_cell(cell_lower, kw))
    return best


def _compute_column_stats(ws, col: int, sample_rows: range) -> dict[str, float]:
    """
    Compute lightweight value-type stats for a column (numeric-like vs text-heavy).
    Used to validate a proposed mapping.
    """
    sample_count = 0
    numeric_like_count = 0
    integer_like_count = 0
    alpha_count = 0

    for r in sample_rows:
        v = ws.cell(r, col).value
        if is_placeholder(v) or v is None:
            continue
        sample_count += 1

        s = str(v).strip()
        has_alpha = bool(re.search(r"[A-Za-z]", s))
        if has_alpha:
            alpha_count += 1

        if _value_is_numeric_like(v):
            numeric_like_count += 1
            num = clean_num(v)
            if num is not None:
                # integer-like: close to whole number
                if abs(num - round(num)) <= 1e-6:
                    integer_like_count += 1

    if sample_count == 0:
        return {"sample_count": 0.0, "numeric_rate": 0.0, "integer_rate": 0.0, "alpha_rate": 0.0}

    numeric_rate = numeric_like_count / sample_count
    integer_rate = (integer_like_count / numeric_like_count) if numeric_like_count else 0.0
    alpha_rate = alpha_count / sample_count

    return {
        "sample_count": float(sample_count),
        "numeric_rate": float(numeric_rate),
        "integer_rate": float(integer_rate),
        "alpha_rate": float(alpha_rate),
    }


def _value_is_numeric_like(v: Any) -> bool:
    """
    Numeric-like means:
      - parsable as number, AND
      - contains no alphabetic text except common currency tokens.
    """
    if is_placeholder(v) or v is None:
        return False

    s = str(v).strip()
    if not s:
        return False

    if clean_num(v) is None:
        return False

    # Allow optional trailing currency tokens/symbols.
    # If there are other letters, it's likely a text/description cell.
    currency_pat = r"(?:rs\.?|inr|usd|eur|gbp|aed|sar|sgd|₹|€|£)"
    return bool(
        re.match(
            rf"^\s*[\d\.,\-\(\)\s/]+(?:\s*{currency_pat})?\s*$",
            s,
            flags=re.IGNORECASE,
        )
    )


def _row_looks_like_header(ws, row: int, max_col: int) -> bool:
    """
    Check if a row looks like a header row (not a data row).
    Header rows have keyword-rich cells and no long description-like text.
    For multi-line header cells, only check the first line.
    Whitespace is normalized before length checks.
    """
    keyword_hits = 0
    has_long_text = False
    for c in range(1, min(max_col + 1, 40)):
        v = ws.cell(row, c).value
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        # For multi-line cells, check only the first line
        first_line = s.split("\n")[0].strip()
        # Normalize excessive whitespace for length check
        normalized = re.sub(r"\s+", " ", first_line)
        sl = normalized.lower()
        # Check for header keywords
        for field, keywords in _get_field_keywords().items():
            for kw in keywords:
                if kw in sl:
                    keyword_hits += 1
                    break
        # Long first line (60+ chars) usually means data, not header
        if len(normalized) > 60:
            has_long_text = True
    # A header row should have at least 2 keyword hits and no very long text
    return keyword_hits >= 2 and not has_long_text


def get_unmapped_columns(ws, header_row: int, mapped: dict[str, int]) -> dict[int, str]:
    """
    Return columns that weren't mapped to any known field.
    Useful for detecting extra tag columns or custom data fields.

    Returns {1-based_column_index: header_text}.
    """
    max_col = min(ws.max_column or 50, 80)
    mapped_cols = set(mapped.values())
    unmapped: dict[int, str] = {}

    for c in range(1, max_col + 1):
        if c in mapped_cols:
            continue
        text = _get_header_text(ws, header_row, c)
        if text:
            unmapped[c] = text

    return unmapped
