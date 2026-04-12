"""
services/preprocessing.py
--------------------------
Pre-extraction preprocessing layer.

Runs AFTER row_from_dict (output_rows available) and BEFORE annexure_resolver
while the workbook is still open.  Handles group-level multi-tag splitting:

  For each (TAG header, [spare rows]) group:
    • If TAG NO contains N tags → expand to N groups, each with its own header
      and a full copy of the spare rows.
    • If TAG NO is single (or an annexure ref) → pass through unchanged.

SAFETY GUARANTEE
  For single-tag files (all known production files) every group has exactly
  one tag → the function returns the input list unchanged.

STRICT RULES
  • No hardcoded column positions — all access via ci dict.
  • Row classification uses TWO fields (ITEM NUMBER primary, TAG NO fallback)
    to handle blank separator rows robustly.
  • Serial alignment is ONLY applied when len(serials) == len(tags); otherwise
    serial is kept unchanged (None assigned — not forced to an incorrect index).
  • Orphan spare rows (before any header) are dropped with a warning.
  • list(row) copies are made only when splitting actually occurs.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex constants — duplicated from annexure_resolver for self-containment
# ---------------------------------------------------------------------------

_ANNEXURE_ID_RE = re.compile(
    r"(?i)annexure[\s\-_]*(?:\([^)]*\)[\s\-_]*)?([A-Z0-9]+)",
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def preprocess_rows(
    output_rows: list[list],
    ci: dict[str, int],
) -> list[list]:
    """
    Group rows into (header, [spares]) units and expand multi-tag headers.

    Returns the same list reference if no splitting is needed (single-tag files
    are returned unchanged — zero copies made).
    """
    t0 = time.monotonic()

    tag_col    = ci.get("TAG NO")
    item_col   = ci.get("ITEM NUMBER")
    serial_col = ci.get("EQPT SR NO")

    if tag_col is None:
        # No TAG NO column in schema — nothing to do
        log.info("[preprocessing] no TAG NO column — skipped")
        return output_rows

    groups = _group_rows(output_rows, item_col, tag_col)
    log.info("[preprocessing] %d input rows → %d groups", len(output_rows), len(groups))

    result: list[list] = []
    any_split = False

    for header, spares in groups:
        expanded = _expand_group(header, spares, tag_col, serial_col)
        result.extend(expanded)
        # Detect whether a split actually happened
        if header is not None and len(expanded) > 1 + len(spares):
            any_split = True

    elapsed = time.monotonic() - t0
    log.info(
        "[preprocessing] done: %d → %d rows in %.3fs",
        len(output_rows), len(result), elapsed,
    )

    # If nothing changed, return original list to avoid memory churn
    if not any_split and len(result) == len(output_rows):
        return output_rows
    return result


# ---------------------------------------------------------------------------
# Group detection
# ---------------------------------------------------------------------------

def _group_rows(
    output_rows: list[list],
    item_col: int | None,
    tag_col: int,
) -> list[tuple[Any, list]]:
    """
    Walk output_rows once and produce [(header | None, [spare_rows])].

    Classification (two-field fallback):
      1. ITEM NUMBER populated  → SPARE
      2. TAG NO populated       → HEADER
      3. Neither                → inherit previous state
    """
    groups: list[tuple[Any, list]] = []
    current_header: Any = None
    current_spares: list = []
    prev_is_spare = False
    orphan_count = 0

    for row in output_rows:
        is_spare = _classify_row(row, item_col, tag_col, prev_is_spare)
        prev_is_spare = is_spare

        if not is_spare:
            # Flush previous group (if any)
            if current_header is not None or current_spares:
                groups.append((current_header, current_spares))
            current_header = row
            current_spares = []
        else:
            if current_header is None:
                # Spare before any header → orphan
                orphan_count += 1
            else:
                current_spares.append(row)

    # Flush final group
    if current_header is not None or current_spares:
        groups.append((current_header, current_spares))

    if orphan_count:
        log.warning(
            "[preprocessing] %d orphan spare row(s) before first header — dropped",
            orphan_count,
        )

    return groups


def _classify_row(
    row: list,
    item_col: int | None,
    tag_col: int,
    prev_is_spare: bool,
) -> bool:
    """
    Return True (spare) / False (header).

    Priority:
      1. ITEM NUMBER populated  → spare
      2. TAG NO populated       → header
      3. Neither                → inherit prev_is_spare
    """
    # Rule 1: ITEM NUMBER populated → spare
    if item_col is not None and item_col < len(row):
        item_val = row[item_col]
        if item_val is not None and str(item_val).strip() not in ("", "None"):
            return True

    # Rule 2: TAG NO populated → header
    if tag_col < len(row):
        tag_val = row[tag_col]
        if tag_val is not None and str(tag_val).strip():
            return False

    # Rule 3: inherit
    return prev_is_spare


# ---------------------------------------------------------------------------
# Group expansion
# ---------------------------------------------------------------------------

def _expand_group(
    header: Any,
    spares: list,
    tag_col: int,
    serial_col: int | None,
) -> list:
    """
    Return the flat list of rows for this group.

    • header is None  → drop (orphans already excluded by caller)
    • annexure ref    → pass through unchanged
    • single tag      → pass through unchanged (strict no-op, zero copies)
    • multi-tag       → expand: N × (1 header + len(spares) spare rows)
    """
    if header is None:
        return []   # orphans dropped

    tag_val = _str(header[tag_col] if tag_col < len(header) else None)

    # Annexure refs handled by annexure_resolver after this step
    if _is_annexure_ref(tag_val):
        return [header] + spares

    tags = _split_multi_tag(tag_val)

    # Strict no-op: single tag → return originals, no copies
    if len(tags) == 1:
        return [header] + spares

    # Multi-tag expansion
    orig_tag = tag_val
    n_tags   = len(tags)
    n_spares = len(spares)

    # Serial safety: only align when count matches exactly
    serials: list
    if serial_col is not None and serial_col < len(header):
        raw_serial = _str(header[serial_col])
        split_serials = _split_multi_tag(raw_serial) if raw_serial else []
        if len(split_serials) == n_tags:
            serials = split_serials
        else:
            # Counts mismatch — do NOT force incorrect mapping
            serials = [None] * n_tags
    else:
        serials = [None] * n_tags

    log.info(
        "[preprocessing] split TAG='%s' → %d tags × %d spares = %d rows",
        orig_tag, n_tags, n_spares, n_tags * (1 + n_spares),
    )

    result: list = []
    for tag, serial in zip(tags, serials):
        # Header copy (one per tag — unavoidable)
        new_header = list(header)
        new_header[tag_col] = tag
        if serial_col is not None and serial is not None and serial_col < len(new_header):
            new_header[serial_col] = serial
        result.append(new_header)

        # Spare copies (one per spare per tag)
        for spare in spares:
            new_spare = list(spare)
            new_spare[tag_col] = tag
            if serial_col is not None and serial is not None and serial_col < len(new_spare):
                new_spare[serial_col] = serial
            result.append(new_spare)

    return result


# ---------------------------------------------------------------------------
# Helpers — duplicated from annexure_resolver for self-containment
# ---------------------------------------------------------------------------

def _is_annexure_ref(value: str) -> bool:
    """Return True when value contains an annexure reference with a numeric/roman identifier."""
    if not value:
        return False
    m = _ANNEXURE_ID_RE.search(value)
    if not m or not m.group(1):
        return False
    ident = m.group(1).upper()
    if re.search(r"\d", ident):
        return True
    if re.match(r"^[IVXLCDM]+$", ident):
        return True
    return False


def _split_multi_tag(value: str) -> list[str]:
    """
    Split a multi-tag cell value into individual tags.

    Handles: comma, numeric range, A/B suffix, plain slash.
    Returns [value] unchanged if no split pattern found.
    """
    if not value:
        return [value]

    # 1. Comma separation
    if "," in value:
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if len(parts) > 1:
            return parts

    # 2. Numeric range  "PREFIX-N to PREFIX-M"
    range_m = re.match(
        r"^(.+?[\-_])(\d+)\s+to\s+(.+?[\-_])?(\d+)$",
        value.strip(), re.IGNORECASE,
    )
    if range_m:
        prefix = range_m.group(1)
        start  = int(range_m.group(2))
        end    = int(range_m.group(4))
        if end > start and (end - start) < 500:
            return [f"{prefix}{i}" for i in range(start, end + 1)]

    # 3. A/B suffix expansion  "TAG-A/B" → ["TAG-A", "TAG-B"]
    ab_m = re.match(r"^(.*?[\-_])([A-Z])(/[A-Z])+$", value.strip())
    if ab_m:
        base    = ab_m.group(1)
        letters = re.findall(r"[A-Z]", value[len(ab_m.group(1)):])
        if letters:
            return [f"{base}{l}" for l in letters]

    # 4. Plain slash — only if neither part is an annexure ref
    if "/" in value:
        parts = [p.strip() for p in value.split("/") if p.strip()]
        if len(parts) > 1 and not any(_is_annexure_ref(p) for p in parts):
            return parts

    return [value]


def _str(v: Any) -> str:
    """Convert any cell value to str, returning "" for None."""
    return "" if v is None else str(v).strip()


def _align_lists(tags: list[str], serials: list[str]) -> tuple[list[str], list[str]]:
    """Pad the shorter list with "" to equal length."""
    n = max(len(tags), len(serials))
    tags    = tags    + [""] * (n - len(tags))
    serials = serials + [""] * (n - len(serials))
    return tags, serials
