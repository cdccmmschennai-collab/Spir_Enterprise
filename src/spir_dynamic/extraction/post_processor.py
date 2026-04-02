"""
extraction/post_processor.py
-----------------------------
Post-processing: adds POSITION NUMBER and OLD MATERIAL NUMBER/SPF NUMBER.

POSITION NUMBER
  - Always "0010" for all rows (header and detail)

OLD MATERIAL NUMBER / SPF NUMBER (OMN)
  - Exactly 18 characters, always
  - Only assigned to spare/detail rows (rows WITH ITEM NUMBER)
  - Format: BASE + "-" + SUFFIX

  Step 1: Clean SPIR Number -> Base
    - Remove ALL letters
    - Remove brackets and content inside (REV.2)
    - Keep only digits and hyphens
    - Collapse multiple hyphens
    Example: "VEN-4391-MTY-5-43-0006" -> "4391-5-43-0006"

  Step 2: Suffix = sheet-prefix + line number
    - Sheet 1 (main):   L01 through L24 (zero-padded 2 digits)
    - Sheet 2:          1L1 through 1L24
    - Sheet 3:          2L1 through 2L24
    - Max L24 per sheet
    - Line number increments per spare row per sheet (not per tag)

  Step 3: Assemble BASE + "-" + SUFFIX

  Step 4: Enforce exactly 18 chars
    Trim order (if too long):
      1. Remove leading zeros from segments (0610->610, NOT 6100)
      2. Remove hyphens from middle
      3. NEVER remove the first 4-digit project number
    Pad with zeros if too short.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

TARGET_LEN = 18
MAX_LINE_PER_SHEET = 24


# ---------------------------------------------------------------------------
# OMN helpers
# ---------------------------------------------------------------------------

def _clean_spir_base(spir_no: str) -> str:
    """
    Clean SPIR number to numeric-only base.
    Step 1: Remove brackets and ALL content inside  (REV.2) → gone
    Step 2: Remove all letters
    Step 3: Remove unnecessary leading zeros (0006→6, 0610→610, 6000→keep)
    Step 4: Keep numeric structure with hyphens

    "VEN-4391-MTY-5-43-0006" -> "4391-5-43-6"
    "4400-VP-30-00-10-053 (REV.2)" -> "4400-30-0-10-53"
    "VEN-4460-DGTYP-4-43-0004 (Normal) Rev. A" -> "4460-4-43-4"
    """
    s = spir_no or ""

    # Step 1: Remove bracketed content entirely — (REV.2), (Normal), etc.
    s = re.sub(r"\([^)]*\)", "", s)

    # Also remove "Rev. X" / "Rev X" style suffixes (letters will go anyway,
    # but stripping early avoids stray hyphens)
    s = re.sub(r"(?i)\brev\.?\s*[a-z0-9]*", "", s)

    # Step 2: Replace all non-digit, non-hyphen characters with hyphens
    s = re.sub(r"[^0-9\-]", "-", s)

    # Collapse multiple hyphens and strip edges
    s = re.sub(r"-{2,}", "-", s).strip("-")

    # Step 3: Split into segments, drop empties, strip leading zeros
    segs = [seg for seg in s.split("-") if seg]
    cleaned = []
    for seg in segs:
        # Strip leading zeros but keep at least one character
        stripped = seg.lstrip("0") or "0"
        cleaned.append(stripped)

    return "-".join(cleaned)


def _build_suffix(sheet_idx: int, line_idx: int) -> str:
    """
    Build the OMN suffix.
    sheet_idx: 1-based (1 = first/main sheet)
    line_idx: 1-based within that sheet
    """
    if sheet_idx == 1:
        return f"L{line_idx:02d}" if line_idx < 100 else f"L{line_idx}"
    return f"{sheet_idx - 1}L{line_idx}"


def _trim_to_budget(base: str, budget: int) -> str:
    """
    Trim base to at most `budget` characters.

    Order:
      1. Remove leading zeros from segments (rightmost first)
         - "0610" -> "610" (leading zero removed, value preserved)
         - "6100" -> unchanged (no leading zero)
      2. Remove hyphens by fusing the smallest adjacent pair first
         - This preserves readable structure (e.g. 4460-543-10022)
      3. NEVER remove the first 4-digit segment (project number)
    """
    if len(base) <= budget:
        return base

    segs = base.split("-")

    # Pass 1: Remove leading zeros from segments, rightmost first
    # Skip the first segment (project number)
    changed = True
    while changed and len("-".join(segs)) > budget:
        changed = False
        for i in range(len(segs) - 1, 0, -1):
            if len(segs[i]) >= 2 and segs[i][0] == "0":
                segs[i] = segs[i].lstrip("0") or "0"
                changed = True
                if len("-".join(segs)) <= budget:
                    break

    base = "-".join(segs)
    if len(base) <= budget:
        return base

    # Pass 2: Remove hyphens by fusing adjacent segments.
    # At each step, pick the pair (skip index 0) whose fusion produces
    # the shortest combined segment — this keeps the result readable.
    segs = base.split("-")
    while len(segs) > 1 and len("-".join(segs)) > budget:
        best_i = None
        best_combined_len = float("inf")
        for i in range(1, len(segs) - 1):
            combined_len = len(segs[i]) + len(segs[i + 1])
            if combined_len < best_combined_len:
                best_combined_len = combined_len
                best_i = i
        # Fallback: fuse last two if nothing found (only 2 segments left)
        if best_i is None:
            if len(segs) >= 2:
                best_i = len(segs) - 2
            else:
                break
        segs = segs[:best_i] + [segs[best_i] + segs[best_i + 1]] + segs[best_i + 2:]

    base = "-".join(segs)

    # Hard truncate if still too long (preserve first segment)
    if len(base) > budget:
        base = base[:budget]

    return base


def _pad_base(base: str, budget: int) -> str:
    """Pad base with trailing zeros to reach exactly budget length."""
    if len(base) < budget:
        base = base + "0" * (budget - len(base))
    return base


def build_omn(spir_no: str, sheet_idx: int, line_idx: int) -> str:
    """
    Build an exactly-18-character OLD MATERIAL NUMBER.

    Args:
        spir_no: Raw SPIR document number
        sheet_idx: 1-based sheet index (encounter order)
        line_idx: 1-based line index within that sheet
    """
    base = _clean_spir_base(spir_no)
    suffix = _build_suffix(sheet_idx, line_idx)

    print("OMN INPUT:", spir_no)
    print("OMN CLEANED:", base)

    # Budget = 18 - 1 (hyphen) - len(suffix)
    budget = TARGET_LEN - 1 - len(suffix)

    trimmed = _trim_to_budget(base, budget)
    padded = _pad_base(trimmed, budget)

    result = f"{padded}-{suffix}"

    # Hard guarantee: exactly 18 chars
    if len(result) > TARGET_LEN:
        result = result[:TARGET_LEN]
    elif len(result) < TARGET_LEN:
        # Should not happen, but pad just in case
        result = result + "0" * (TARGET_LEN - len(result))

    print("OMN FINAL:", result)
    return result


# ---------------------------------------------------------------------------
# Column index helpers
# ---------------------------------------------------------------------------

def _get_ci():
    from spir_dynamic.extraction.output_schema import CI
    return CI


def _col(ci: dict, *names) -> int | None:
    """Return the first column index that exists in CI."""
    for name in names:
        idx = ci.get(name)
        if idx is not None:
            return idx
    return None


# ---------------------------------------------------------------------------
# Sheet index tracker
# ---------------------------------------------------------------------------

_NON_MAIN_PATTERNS = re.compile(
    r"(?:continuation|cont\.?\s|continued|overflow|annexure|annex\b)",
    re.IGNORECASE,
)


class SheetTracker:
    """
    Maps sheet names to main-sheet indices for OMN suffix generation.

    Only MAIN sheets increment the index.
    Continuation and annexure sheets inherit the index of the most recent
    main sheet — they are part of the same logical block.

    Detection uses TWO sources:
      1. main_sheet_names (from profile roles) — if a sheet is NOT in this
         set and NOT utility/unknown, it's non-main.
      2. Name patterns — "Continuation", "Annexure" in the name.
    """

    def __init__(self, main_sheet_names: set[str] | None = None):
        self._main_names: set[str] = main_sheet_names or set()
        self._sheet_to_idx: dict[str, int] = {}
        self._sheet_line: dict[str, int] = {}
        self._main_counter = 0        # number of main sheets seen so far
        self._current_main_idx = 1    # 1-based index of current main block

    def get_sheet_idx(self, sheet: str | None) -> int:
        key = (sheet or "MAIN").strip()
        if key in self._sheet_to_idx:
            return self._sheet_to_idx[key]

        is_main = self._is_main(key)

        if is_main:
            # New main sheet → increment
            self._main_counter += 1
            self._current_main_idx = self._main_counter
            print(f"Sheet: {key}")
            print(f"Type: MAIN")
            print(f"Main Index: {self._current_main_idx - 1}")
        else:
            # Continuation / annexure → reuse current main index
            print(f"Sheet: {key}")
            print(f"Type: CONTINUATION/ANNEXURE")
            print(f"Main Index: {self._current_main_idx - 1}")

        self._sheet_to_idx[key] = self._current_main_idx
        return self._current_main_idx

    def next_line(self, sheet: str | None) -> int:
        key = (sheet or "MAIN").strip()
        self._sheet_line[key] = self._sheet_line.get(key, 0) + 1
        return self._sheet_line[key]

    def _is_main(self, key: str) -> bool:
        """
        Determine if a sheet is a main (data) sheet.

        A sheet is NOT main if its name matches continuation/annexure patterns.
        This catches cases where the analyzer classifies continuations as 'data'
        because they share the same column_headers layout as the main sheet.
        """
        if _NON_MAIN_PATTERNS.search(key):
            return False
        return True


# ---------------------------------------------------------------------------
# Main post-processor
# ---------------------------------------------------------------------------

def post_process_rows(
    rows: list[list],
    spir_no: str,
    main_sheet_names: set[str] | None = None,
) -> list[list]:
    """
    Assign POSITION NUMBER and OLD MATERIAL NUMBER/SPF NUMBER to every row.
    Modifies rows in-place and returns the same list.

    main_sheet_names: names of sheets classified as DATA (main).
        Only these sheets cause the OMN line-number prefix to increment.
        Continuation/annexure sheets inherit the prefix of the preceding main sheet.
    """
    if not rows:
        return rows

    ci = _get_ci()

    tag_col = _col(ci, "TAG NO")
    item_col = _col(ci, "ITEM NUMBER")
    pos_col = _col(ci, "POSITION NUMBER")
    spf_col = _col(ci, "OLD MATERIAL NUMBER/SPF NUMBER")
    sheet_col = _col(ci, "SHEET")

    if pos_col is None and spf_col is None:
        return rows

    sheet_tracker = SheetTracker(main_sheet_names)
    spir_no_clean = (spir_no or "").strip()

    # Per-tag position counter: resets for each new tag
    pos_counter: dict[str, int] = {}  # tag_key -> next position value

    for row in rows:
        ncols = len(row)

        tag = row[tag_col] if tag_col is not None and tag_col < ncols else None
        item = row[item_col] if item_col is not None and item_col < ncols else None
        sheet = row[sheet_col] if sheet_col is not None and sheet_col < ncols else None

        tag_key = str(tag or "__NONE__").strip().upper()
        is_spare = item is not None and str(item).strip() not in ("", "None")

        # POSITION NUMBER: per-tag, resets for each new tag
        if pos_col is not None and pos_col < ncols:
            if not is_spare:
                # Header/equipment row → always "0010", initialize counter
                row[pos_col] = "0010"
                if tag_key not in pos_counter:
                    pos_counter[tag_key] = 10  # ready for first spare
            else:
                # Spare/detail row → use counter, increment by 10
                if tag_key not in pos_counter:
                    pos_counter[tag_key] = 10
                row[pos_col] = str(pos_counter[tag_key]).zfill(4)
                pos_counter[tag_key] += 10

        # OLD MATERIAL NUMBER: only for spare/detail rows
        if spf_col is not None and spf_col < ncols and is_spare and spir_no_clean:
            sheet_idx = sheet_tracker.get_sheet_idx(sheet)
            line_idx = sheet_tracker.next_line(sheet)
            row[spf_col] = build_omn(spir_no_clean, sheet_idx, line_idx)

    return rows
