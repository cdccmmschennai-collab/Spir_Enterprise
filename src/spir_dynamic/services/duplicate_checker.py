"""
Duplicate detection and flagging based on (tag + description) key.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _norm(v) -> str:
    return str(v or "").strip().upper()


def deduplicate_rows(rows: list[list], CI: dict) -> list[list]:
    """Flag duplicate rows in-place based on (tag, description) key."""
    tag_col = CI.get("TAG NO", CI.get("tag", 0))
    desc_col = CI.get("DESCRIPTION OF PARTS", CI.get("description", 1))
    flag_col = CI.get("SPIR ERROR", CI.get("duplicate_flag"))

    if flag_col is None:
        return rows

    seen: set[tuple[str, str]] = set()

    for row in rows:
        tag = _norm(row[tag_col] if tag_col < len(row) else None)
        desc = _norm(row[desc_col] if desc_col < len(row) else None)
        key = (tag, desc)

        if key in seen:
            row[flag_col] = "DUPLICATE"
        else:
            seen.add(key)
            row[flag_col] = ""

    return rows


def analyse_duplicates(rows: list[list]) -> dict:
    """Scan rows and return duplicate summary counts."""
    from spir_dynamic.extraction.output_schema import CI

    flag_col = CI.get("SPIR ERROR", CI.get("duplicate_flag"))
    tag_col = CI.get("TAG NO", 0)
    desc_col = CI.get("DESCRIPTION OF PARTS", 1)

    dup1_count = 0
    sap_count = 0
    dup_items: list[dict] = []

    for row in rows:
        flag = str(row[flag_col] if flag_col is not None else "").strip()
        if not flag:
            continue
        if flag == "DUPLICATE":
            dup1_count += 1
            dup_items.append({
                "type": "DUPLICATE",
                "label": flag,
                "tag": row[tag_col] if tag_col < len(row) else None,
                "description": row[desc_col] if desc_col < len(row) else None,
            })

    return {
        "dup1_count": dup1_count,
        "sap_count": sap_count,
        "dup_items": dup_items,
    }
