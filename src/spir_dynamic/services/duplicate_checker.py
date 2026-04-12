"""
ERROR column logic — three co-existing rules, each with its own global counter.

Rules (all can fire on the same row, output in this fixed order):

  SAP DUPLICATE  — same SAP number linked to 2+ different MPNs.
                   Every row carrying that SAP is flagged.

  SAP MISMATCH   — same MPN linked to 2+ different SAP numbers.
                   Every row carrying that MPN is flagged.

  SPARE DUPLICATE — within the same Tag Number, the same MPN appears more
                    than once.  Rows in different tags with the same MPN are
                    NOT flagged — only within-tag repetition counts.

SAP-based rules (SAP DUPLICATE, SAP MISMATCH) are skipped for rows that
have no SAP value or a zero SAP value.  SPARE DUPLICATE always applies.

Numbering:
  Each rule keeps its own global counter, incremented once per unique
  violation *group* (not per row).  All rows in the same group share the
  same number.  Groups are numbered in first-appearance order.

Output format examples:
  "sap duplicate - 1"
  "sap mismatch - 2"
  "sap duplicate - 1, sap mismatch - 2, spare duplicate - 1"
  0   (integer, when no errors)
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _norm(v) -> str:
    return str(v or "").strip().upper()


def deduplicate_rows(rows: list[list], CI: dict) -> list[list]:
    """
    Apply ERROR rules in-place and return the modified rows.

    CI must contain integer column indices for the relevant fields.
    """
    tag_col  = CI["TAG NO"]
    part_col = CI["MANUFACTURER PART NUMBER"]
    sap_col  = CI.get("SAP NUMBER")
    flag_col = CI.get("ERROR")

    if flag_col is None:
        return rows

    # Default every row to 0
    for row in rows:
        if flag_col < len(row):
            row[flag_col] = 0

    def _get(row: list, col) -> str:
        if col is None or col >= len(row):
            return ""
        return _norm(row[col])

    # -----------------------------------------------------------------------
    # Phase 1: build violation mappings (single pass)
    # -----------------------------------------------------------------------
    mpn_to_saps:   dict[str, set[str]]           = {}  # mpn  → {sap, ...}
    sap_to_mpns:   dict[str, set[str]]           = {}  # sap  → {mpn, ...}
    tag_mpn_count: dict[tuple[str, str], int]    = {}  # (tag, mpn) → count

    for row in rows:
        tag  = _get(row, tag_col)
        part = _get(row, part_col)
        sap  = _get(row, sap_col)

        # SAP-based mappings: only when SAP is present and non-zero
        if part and sap and sap not in ("0", "0.0"):
            mpn_to_saps.setdefault(part, set()).add(sap)
            sap_to_mpns.setdefault(sap,  set()).add(part)

        # Spare duplicate: track (tag, mpn) occurrences
        if tag and part:
            k = (tag, part)
            tag_mpn_count[k] = tag_mpn_count.get(k, 0) + 1

    # -----------------------------------------------------------------------
    # Phase 2: assign group numbers (first-appearance order)
    # -----------------------------------------------------------------------
    mismatch_groups:  dict[str, int]           = {}  # mpn       → N
    duplicate_groups: dict[str, int]           = {}  # sap       → N
    spare_groups:     dict[tuple[str, str], int] = {}  # (tag,mpn) → N

    mismatch_counter  = 0
    duplicate_counter = 0
    spare_counter     = 0

    for mpn, saps in mpn_to_saps.items():
        if len(saps) >= 2:
            mismatch_counter += 1
            mismatch_groups[mpn] = mismatch_counter

    for sap, mpns in sap_to_mpns.items():
        if len(mpns) >= 2:
            duplicate_counter += 1
            duplicate_groups[sap] = duplicate_counter

    for (tag, mpn), cnt in tag_mpn_count.items():
        if cnt >= 2:
            spare_counter += 1
            spare_groups[(tag, mpn)] = spare_counter

    # -----------------------------------------------------------------------
    # Phase 3: write errors to rows (single pass)
    # -----------------------------------------------------------------------
    for row in rows:
        tag  = _get(row, tag_col)
        part = _get(row, part_col)
        sap  = _get(row, sap_col)

        errors: list[str] = []

        # Fixed order: sap duplicate → sap mismatch → spare duplicate

        if sap and sap not in ("0", "0.0"):
            n = duplicate_groups.get(sap)
            if n is not None:
                errors.append(f"sap duplicate - {n}")

            n = mismatch_groups.get(part) if part else None
            if n is not None:
                errors.append(f"sap mismatch - {n}")

        if tag and part:
            n = spare_groups.get((tag, part))
            if n is not None:
                errors.append(f"spare duplicate - {n}")

        if flag_col < len(row):
            row[flag_col] = ", ".join(errors) if errors else 0

    log.info(
        "ERROR detection: %d sap duplicate group(s), %d sap mismatch group(s), "
        "%d spare duplicate group(s)",
        duplicate_counter, mismatch_counter, spare_counter,
    )
    return rows


def analyse_duplicates(rows: list[list]) -> dict:
    """Return counts of each error type across all rows."""
    from spir_dynamic.extraction.output_schema import CI

    flag_col = CI.get("ERROR")
    tag_col  = CI["TAG NO"]
    desc_col = CI["DESCRIPTION OF PARTS"]

    sap_dup_count    = 0
    sap_mismatch_count = 0
    spare_dup_count  = 0
    dup_items: list[dict] = []

    for row in rows:
        if flag_col is None or flag_col >= len(row):
            continue
        flag = str(row[flag_col]).strip()
        if not flag or flag == "0":
            continue

        tag  = row[tag_col]  if tag_col  < len(row) else None
        desc = row[desc_col] if desc_col < len(row) else None

        if "sap duplicate" in flag:
            sap_dup_count += 1
            dup_items.append({"type": "sap duplicate", "tag": tag, "description": desc})
        if "sap mismatch" in flag:
            sap_mismatch_count += 1
            dup_items.append({"type": "sap mismatch", "tag": tag, "description": desc})
        if "spare duplicate" in flag:
            spare_dup_count += 1
            dup_items.append({"type": "spare duplicate", "tag": tag, "description": desc})

    return {
        # Kept for API backwards-compatibility
        "dup1_count":           spare_dup_count,
        "sap_count":            sap_mismatch_count,
        # New counts
        "sap_duplicate_count":  sap_dup_count,
        "spare_duplicate_count": spare_dup_count,
        "dup_items":            dup_items,
    }
