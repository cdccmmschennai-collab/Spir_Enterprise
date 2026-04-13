"""
services/annexure_resolver.py
------------------------------
Post-extraction annexure reference resolver.

Runs AFTER unified_extractor (but BEFORE post_processor) while the
workbook is still open.  Handles two independent cases:

  1. MULTI-TAG SPLITTING
     When TAG NO (and/or EQPT SR NO) contains multiple tag values
     separated by commas, slashes, A/B suffixes, or numeric ranges,
     split them into one row per tag, keeping TAG and SERIAL aligned
     1:1 (shorter list padded with "").

  2. ANNEXURE EXPANSION
     When TAG NO / EQPT SR NO / EQPT MODEL contains an annexure
     reference (e.g. "ANNEXURE-1", "Refer Annexure 2"), read the
     actual values from the corresponding worksheet and replace the
     reference row(s) with one row per entry from the annexure sheet.

SAFETY GUARANTEE
  For working files where the extractor has already resolved all tags,
  neither case triggers → the function returns the input list unchanged.

STRICT RULES (enforced throughout)
  • No hardcoded column positions — all access via ci dict
  • No structural changes — row order is preserved; rows are only
    replaced by their expansion
  • Placeholder values (N/A, TBA, "will provide", etc.) are normalised
    to "" before any assignment
  • TAG and SERIAL are always kept 1:1 aligned
"""
from __future__ import annotations

import logging
import re
from typing import Any

from spir_dynamic.utils.logging import timed

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — keyword lists used for dynamic column detection
# ---------------------------------------------------------------------------

_TAG_KEYWORDS    = ["tag no", "tag number", "tag nos", "tag numbers",
                    "valve tag", "equipment tag", "equip tag"]
_SERIAL_KEYWORDS = ["serial no", "serial number", "ser no", "mfr ser",
                    "serial nos", "serial numbers", "mfr serial"]
_MODEL_KEYWORDS  = ["model no", "model number", "model", "mfr type",
                    "type or model"]

# Placeholder pattern — values that should be treated as empty
_PLACEHOLDER_RE = re.compile(
    r"^\s*(?:n/?a|tba|to\s+be\b|will\s+provide|will\s+be\b|na|--+)\s*$",
    re.IGNORECASE,
)

# Annexure reference pattern — must contain "annexure" + optional group + identifier
# Handles: "ANNEXURE-1", "Annexure (P1)-1", "Refer Annexure B1", "Annexure II"
_ANNEXURE_ID_RE = re.compile(
    r"(?i)annexure[\s\-_]*(?:\([^)]*\)[\s\-_]*)?([A-Z0-9]+)",
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

@timed
def resolve_annexure_refs(
    output_rows: list[list],
    wb,          # openpyxl Workbook (still open when called)
    ci: dict[str, int],
) -> list[list]:
    """
    Process output_rows and return the (potentially expanded) list.

    For each row:
      Step A — multi-tag split (TAG NO + EQPT SR NO aligned 1:1)
      Step B — annexure expansion (TAG NO / EQPT SR NO / EQPT MODEL)
      Step C — pass through unchanged

    Known files have no annexure refs and no unsplit multi-tags after
    extraction → every row hits Step C → list returned unchanged.
    """
    tag_col    = ci.get("TAG NO")
    serial_col = ci.get("EQPT SR NO")
    model_col  = ci.get("EQPT MODEL")

    # If columns are not in the schema, nothing to do
    if tag_col is None:
        return output_rows

    # Cache resolved annexure worksheets to avoid re-scanning per row
    _ws_cache: dict[str, Any] = {}   # key → ws or None sentinel

    new_rows: list[list] = []

    for row in output_rows:
        tag_val    = _str(row[tag_col]    if tag_col    < len(row) else None)
        serial_val = _str(row[serial_col] if serial_col is not None and serial_col < len(row) else None)
        model_val  = _str(row[model_col]  if model_col  is not None and model_col  < len(row) else None)

        # ----------------------------------------------------------------
        # STEP A: Multi-tag split
        # Only attempted when TAG NO is NOT itself an annexure reference.
        # ----------------------------------------------------------------
        if tag_val and not _is_annexure_ref(tag_val):
            tag_parts    = _split_multi_tag(tag_val)
            serial_parts = _split_multi_tag(serial_val) if serial_val else []

            if len(tag_parts) > 1:
                # Align tag and serial lists to equal length (pad with "")
                tag_parts, serial_parts = _align_lists(tag_parts, serial_parts)
                for t, s in zip(tag_parts, serial_parts):
                    new_row = list(row)
                    new_row[tag_col] = t
                    if serial_col is not None and serial_col < len(new_row):
                        new_row[serial_col] = s
                    new_rows.append(new_row)
                continue   # row fully handled

        # ----------------------------------------------------------------
        # STEP B: Annexure expansion
        # Check TAG NO first, then SERIAL, then MODEL.
        # ----------------------------------------------------------------
        annexure_src = None
        if   _is_annexure_ref(tag_val):    annexure_src = tag_val
        elif _is_annexure_ref(serial_val): annexure_src = serial_val
        elif _is_annexure_ref(model_val):  annexure_src = model_val

        if annexure_src:
            key = _normalize_annexure_key(annexure_src)

            # Resolve worksheet (cached)
            if key not in _ws_cache:
                _ws_cache[key] = _find_annexure_ws(wb, key)
                if _ws_cache[key] is None:
                    log.warning(
                        "[annexure_resolver] No worksheet found for key '%s' "
                        "(ref=%r) — row kept unchanged", key, annexure_src,
                    )

            ws = _ws_cache[key]
            if ws is None:
                new_rows.append(row)
                continue

            col_map = _detect_columns(ws)
            if not col_map.get("tag"):
                log.warning(
                    "[annexure_resolver] No tag column detected in sheet '%s' "
                    "— row kept unchanged", ws.title,
                )
                new_rows.append(row)
                continue

            entries = _read_annexure_entries(ws, col_map)
            if not entries:
                log.warning(
                    "[annexure_resolver] Zero valid entries in sheet '%s' "
                    "— row kept unchanged", ws.title,
                )
                new_rows.append(row)
                continue

            log.info(
                "[annexure_resolver] '%s': %d entries, serial=%s, model=%s",
                ws.title, len(entries),
                "yes" if col_map.get("serial") else "no",
                "yes" if col_map.get("model")  else "no",
            )

            for entry in entries:
                new_row = list(row)

                # TAG — always update when the tag field was the ref
                if _is_annexure_ref(tag_val):
                    new_row[tag_col] = entry["tag"]

                # SERIAL — always assign (entry serial or ""); keeps 1:1 with tag
                if serial_col is not None and serial_col < len(new_row):
                    new_row[serial_col] = entry["serial"]   # already "" if missing

                # MODEL — assign only if present in entry; do NOT split
                if model_col is not None and model_col < len(new_row):
                    if entry["model"]:
                        # Update if current value is empty, placeholder, or annexure ref
                        cur = _str(new_row[model_col])
                        if not cur or _is_placeholder_str(cur) or _is_annexure_ref(cur):
                            new_row[model_col] = entry["model"]

                new_rows.append(new_row)

            log.info(
                "[annexure_resolver] Expanded '%s': 1 row → %d rows",
                key, len(entries),
            )
            continue

        # ----------------------------------------------------------------
        # STEP C: No changes needed
        # ----------------------------------------------------------------
        new_rows.append(row)

    return new_rows


# ---------------------------------------------------------------------------
# Annexure reference detection & normalisation
# ---------------------------------------------------------------------------

def _is_annexure_ref(value: str) -> bool:
    """
    Return True only when value contains "annexure" followed by a
    non-empty identifier that is either:
      • contains at least one digit  (e.g. "1", "B1", "A2", "P1-3")
      • OR is composed purely of roman numeral chars (I, V, X, L, C, D, M)

    True:  "ANNEXURE-1", "Refer Annexure 2", "Annexure B1", "Annexure II"
    False: "See annexure", "Annexure note", "Refer annexure for details", None, ""
    """
    if not value:
        return False
    m = _ANNEXURE_ID_RE.search(value)
    if not m or not m.group(1):
        return False
    ident = m.group(1).upper()
    # Valid: has at least one digit
    if re.search(r"\d", ident):
        return True
    # Valid: pure roman numerals (I V X L C D M)
    if re.match(r"^[IVXLCDM]+$", ident):
        return True
    return False


def _normalize_annexure_key(value: str) -> str:
    """
    Normalise an annexure reference to a lookup key.
    "ANNEXURE-1" → "ANNEXURE1"
    "Refer Annexure 2" → "ANNEXURE2"
    "Annexure B1" → "ANNEXUREB1"
    """
    m = _ANNEXURE_ID_RE.search(value)
    if m:
        return "ANNEXURE" + m.group(1).upper()
    # Fallback: strip non-alphanumeric, uppercase
    cleaned = re.sub(r"[^A-Z0-9]", "", value.upper())
    return cleaned


def _find_annexure_ws(wb, key: str):
    """
    Search wb.sheetnames for a sheet whose normalized name matches key.
    Returns the worksheet or None.
    """
    for name in wb.sheetnames:
        if _is_annexure_ref(name) and _normalize_annexure_key(name) == key:
            return wb[name]
    # Second pass: looser match — strip all non-alphanumeric from sheet name
    key_clean = re.sub(r"[^A-Z0-9]", "", key.upper())
    for name in wb.sheetnames:
        name_clean = re.sub(r"[^A-Z0-9]", "", name.upper())
        if name_clean == key_clean:
            return wb[name]
    return None


# ---------------------------------------------------------------------------
# Column detection in annexure sheet
# ---------------------------------------------------------------------------

def _detect_columns(ws) -> dict:
    """
    Scan the first 8 rows of ws for header keywords.
    Returns:
        {
            "tag":        1-based col index or None,
            "serial":     1-based col index or None,
            "model":      1-based col index or None,
            "header_row": row index of detected header (or 1),
        }
    Detection is purely keyword-based — no hardcoded positions.
    """
    result = {"tag": None, "serial": None, "model": None, "header_row": 1}
    max_col = min(ws.max_column or 20, 30)
    max_row = min(ws.max_row    or 10,  8)

    for r in range(1, max_row + 1):
        for c in range(1, max_col + 1):
            raw = ws.cell(r, c).value
            if raw is None:
                continue
            cell_lower = str(raw).lower().strip()

            if result["tag"] is None:
                if any(kw in cell_lower for kw in _TAG_KEYWORDS):
                    result["tag"] = c
                    result["header_row"] = r
                    continue

            if result["serial"] is None:
                if any(kw in cell_lower for kw in _SERIAL_KEYWORDS):
                    result["serial"] = c
                    continue

            if result["model"] is None:
                if any(kw in cell_lower for kw in _MODEL_KEYWORDS):
                    result["model"] = c
                    continue

        if result["tag"] is not None:
            # Header row found; set data start and stop scanning for more headers
            break

    return result


# ---------------------------------------------------------------------------
# Reading entries from an annexure sheet
# ---------------------------------------------------------------------------

def _read_annexure_entries(ws, col_map: dict) -> list[dict]:
    """
    Read tag / serial / model rows from ws starting after the header row.
    Returns a list of {"tag": str, "serial": str, "model": str|None}.
    Placeholder values are normalised to "".  Rows with empty/placeholder
    tags are skipped entirely.
    """
    entries: list[dict] = []
    tag_col    = col_map["tag"]
    serial_col = col_map.get("serial")
    model_col  = col_map.get("model")
    start_row  = col_map["header_row"] + 1
    max_row    = ws.max_row or 0

    for r in range(start_row, max_row + 1):
        raw_tag = ws.cell(r, tag_col).value
        tag_str = _clean(raw_tag)
        if not tag_str:
            continue   # empty tag → skip row

        serial_str = _clean(ws.cell(r, serial_col).value) if serial_col else ""
        model_str  = _clean(ws.cell(r, model_col).value)  if model_col  else None

        entries.append({
            "tag":    tag_str,
            "serial": serial_str,    # "" when missing/placeholder
            "model":  model_str,     # None when column absent, "" when placeholder
        })

    return entries


# ---------------------------------------------------------------------------
# Multi-tag splitting
# ---------------------------------------------------------------------------

def _split_multi_tag(value: str) -> list[str]:
    """
    Split a single-cell multi-tag value into a list of individual tags.

    Handles:
      • Comma separation: "T001, T002" → ["T001", "T002"]
      • A/B suffix:       "23V01-A/B"  → ["23V01-A", "23V01-B"]
      • Plain slash:      "T001/T002"  → ["T001", "T002"]
      • Numeric range:    "30-GV-23 to 30-GV-25" → ["30-GV-23","30-GV-24","30-GV-25"]

    Returns [value] unchanged if no split pattern is found.
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
        if end > start and (end - start) < 500:   # sanity cap
            return [f"{prefix}{i}" for i in range(start, end + 1)]

    # 3. A/B (or A/B/C) suffix expansion  "TAG-A/B" → ["TAG-A", "TAG-B"]
    ab_m = re.match(r"^(.*?[\-_])([A-Z])(/[A-Z])+$", value.strip())
    if ab_m:
        base    = ab_m.group(1)
        letters = re.findall(r"[A-Z]", value[len(ab_m.group(1)):])
        if letters:
            return [f"{base}{l}" for l in letters]

    # 4. Plain slash — only if neither part contains "annexure"
    if "/" in value:
        parts = [p.strip() for p in value.split("/") if p.strip()]
        if len(parts) > 1 and not any(_is_annexure_ref(p) for p in parts):
            return parts

    return [value]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _str(v) -> str:
    """Convert any cell value to str, returning "" for None."""
    return "" if v is None else str(v).strip()


def _is_placeholder_str(value: str) -> bool:
    """Return True if value matches a placeholder pattern."""
    return bool(_PLACEHOLDER_RE.match(value))


def _clean(raw) -> str:
    """Convert raw cell value to clean string; placeholders → ""."""
    s = _str(raw)
    if _is_placeholder_str(s):
        return ""
    return s


def _align_lists(tags: list[str], serials: list[str]) -> tuple[list[str], list[str]]:
    """
    Ensure tags and serials have equal length.
    The shorter list is padded with "" to match the longer one.
    """
    n = max(len(tags), len(serials))
    tags    = tags    + [""] * (n - len(tags))
    serials = serials + [""] * (n - len(serials))
    return tags, serials
