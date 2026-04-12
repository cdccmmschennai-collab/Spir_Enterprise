"""
SPIR ERROR column logic.

Two mutually exclusive rules (checked in priority order):

  SAP MISMATCH  — file contains SAP numbers AND the same manufacturer part
                  number appears with two or more distinct SAP numbers.
                  Every row carrying that part number is flagged.
                  Takes priority over DUPLICATE.

  DUPLICATE     — file has NO SAP numbers AND the exact same
                  (TAG NO, DESCRIPTION OF PARTS, MANUFACTURER PART NUMBER)
                  triple appears more than once for the same tag.
                  Rows with different TAG NO values are never flagged,
                  even if part/description are identical.

  0             — default for every row that does not match either rule.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _norm(v) -> str:
    return str(v or "").strip().upper()


def deduplicate_rows(rows: list[list], CI: dict) -> list[list]:
    """
    Apply SPIR ERROR rules in-place and return the modified rows.

    CI must contain integer column indices for the relevant fields.
    """
    tag_col  = CI.get("TAG NO")
    desc_col = CI.get("DESCRIPTION OF PARTS")
    part_col = CI.get("MANUFACTURER PART NUMBER")
    sap_col  = CI.get("SAP NUMBER")
    flag_col = CI.get("SPIR ERROR")

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
    # Determine if the file contains real SAP numbers (non-empty, non-zero)
    # -----------------------------------------------------------------------
    has_sap = False
    if sap_col is not None:
        for row in rows:
            sap = _get(row, sap_col)
            if sap and sap not in ("0", "0.0"):
                has_sap = True
                break

    # -----------------------------------------------------------------------
    # Rule 1 — SAP MISMATCH (only when file has SAP numbers)
    # -----------------------------------------------------------------------
    if has_sap:
        # Build part_number → {set of distinct SAP values}
        part_sap: dict[str, set[str]] = {}
        for row in rows:
            part = _get(row, part_col)
            sap  = _get(row, sap_col)
            if part and sap and sap not in ("0", "0.0"):
                part_sap.setdefault(part, set()).add(sap)

        # Parts that appear with 2+ different SAP numbers
        mismatch_parts = {p for p, saps in part_sap.items() if len(saps) > 1}

        if mismatch_parts:
            for row in rows:
                part = _get(row, part_col)
                if part and part in mismatch_parts:
                    if flag_col < len(row):
                        row[flag_col] = "SAP MISMATCH"

            log.info(
                "SAP MISMATCH: %d part number(s) with conflicting SAP codes flagged",
                len(mismatch_parts),
            )
        return rows

    # -----------------------------------------------------------------------
    # Rule 2 — DUPLICATE (only when file has NO SAP numbers)
    # -----------------------------------------------------------------------
    seen: set[tuple[str, str, str]] = set()
    dup_count = 0

    for row in rows:
        tag  = _get(row, tag_col)
        desc = _get(row, desc_col)
        part = _get(row, part_col)

        # Skip rows that are missing any of the three key fields
        if not tag or not desc or not part:
            continue

        key = (tag, desc, part)
        if key in seen:
            if flag_col < len(row):
                row[flag_col] = "DUPLICATE"
            dup_count += 1
        else:
            seen.add(key)

    if dup_count:
        log.info("DUPLICATE: %d duplicate spare row(s) flagged", dup_count)

    return rows


def analyse_duplicates(rows: list[list]) -> dict:
    """Return counts of DUPLICATE and SAP MISMATCH flags across all rows."""
    from spir_dynamic.extraction.output_schema import CI

    flag_col = CI.get("SPIR ERROR")
    tag_col  = CI.get("TAG NO", 0)
    desc_col = CI.get("DESCRIPTION OF PARTS", 1)

    dup1_count = 0
    sap_count  = 0
    dup_items: list[dict] = []

    for row in rows:
        if flag_col is None or flag_col >= len(row):
            continue
        flag = str(row[flag_col]).strip()
        if flag == "DUPLICATE":
            dup1_count += 1
            dup_items.append({
                "type": "DUPLICATE",
                "tag": row[tag_col] if tag_col < len(row) else None,
                "description": row[desc_col] if desc_col < len(row) else None,
            })
        elif flag == "SAP MISMATCH":
            sap_count += 1
            dup_items.append({
                "type": "SAP MISMATCH",
                "tag": row[tag_col] if tag_col < len(row) else None,
                "description": row[desc_col] if desc_col < len(row) else None,
            })

    return {
        "dup1_count": dup1_count,
        "sap_count": sap_count,
        "dup_items": dup_items,
    }
