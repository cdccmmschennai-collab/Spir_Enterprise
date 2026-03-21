"""
services/duplicate_checker.py
───────────────────────────────
Duplicate detection and flagging.

Spec requirement:
  Remove duplicate rows based on (tag + description).

Implementation:
  1. Build a seen-set keyed on (tag.upper(), description.upper()).
  2. First occurrence → kept, flag = "".
  3. Subsequent occurrences → flag = "DUPLICATE" (row kept for audit trail).

Returns:
  Modified rows list (in-place mutation) + summary counts.

The 'duplicate_flag' column in the output schema holds the flag value.
"""
from __future__ import annotations
import logging

log = logging.getLogger(__name__)


def _norm(v) -> str:
    """Normalise a value to uppercase string for comparison."""
    return str(v or "").strip().upper()


def deduplicate_rows(rows: list[list], CI: dict) -> list[list]:
    """
    Flag duplicate rows in-place based on (tag, description) key.

    Args:
        rows: List of output-schema rows (lists).
        CI:   Column-index dict from output_schema.

    Returns:
        Same list with duplicate_flag set on duplicate rows.
    """
    tag_col  = CI.get("tag", 0)
    desc_col = CI.get("description", 1)
    flag_col = CI.get("duplicate_flag")

    if flag_col is None:
        log.warning("duplicate_flag column not found in CI — skipping dedup")
        return rows

    seen: set[tuple[str, str]] = set()

    for row in rows:
        tag  = _norm(row[tag_col]  if tag_col  < len(row) else None)
        desc = _norm(row[desc_col] if desc_col < len(row) else None)
        key  = (tag, desc)

        if key in seen:
            row[flag_col] = "DUPLICATE"
        else:
            seen.add(key)
            row[flag_col] = ""

    return rows


def analyse_duplicates(rows: list[list]) -> dict:
    """
    Scan rows and return duplicate summary counts.

    Returns:
        {
            "dup1_count": int,      # rows flagged DUPLICATE
            "sap_count":  int,      # rows flagged SAP NUMBER MISMATCH (legacy)
            "dup_items":  list[dict]
        }
    """
    from extraction.output_schema import CI

    flag_col = CI.get("duplicate_flag")
    tag_col  = CI.get("tag", 0)
    desc_col = CI.get("description", 1)

    dup1_count = 0
    sap_count  = 0
    dup_items: list[dict] = []

    for row in rows:
        flag = str(row[flag_col] if flag_col is not None else "").strip()
        if not flag:
            continue

        if flag == "DUPLICATE":
            dup1_count += 1
            dup_items.append({
                "type":        "DUPLICATE",
                "label":       flag,
                "tag":         row[tag_col]  if tag_col  < len(row) else None,
                "description": row[desc_col] if desc_col < len(row) else None,
            })
        elif flag == "SAP NUMBER MISMATCH":
            sap_count += 1
            dup_items.append({
                "type":        "SAP_MISMATCH",
                "label":       flag,
                "tag":         row[tag_col]  if tag_col  < len(row) else None,
                "description": row[desc_col] if desc_col < len(row) else None,
            })

    return {
        "dup1_count": dup1_count,
        "sap_count":  sap_count,
        "dup_items":  dup_items,
    }


def remove_duplicates(rows: list[list]) -> list[list]:
    """
    Hard-remove duplicate rows (don't just flag, actually drop them).
    Returns a new list with only the first occurrence of each (tag, description) key.
    Use this when you want clean output without any duplicate rows at all.
    """
    from extraction.output_schema import CI

    tag_col  = CI.get("tag", 0)
    desc_col = CI.get("description", 1)

    seen:   set[tuple]  = set()
    result: list[list]  = []

    for row in rows:
        tag  = _norm(row[tag_col]  if tag_col  < len(row) else None)
        desc = _norm(row[desc_col] if desc_col < len(row) else None)
        key  = (tag, desc)
        if key not in seen:
            seen.add(key)
            result.append(row)

    removed = len(rows) - len(result)
    if removed:
        log.info("Duplicate removal: dropped %d duplicate rows", removed)

    return result
