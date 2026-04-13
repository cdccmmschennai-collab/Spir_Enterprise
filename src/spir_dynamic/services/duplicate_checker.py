"""
Output ERROR column logic.

Rules are applied independently and a row may carry multiple labels:

  sap mismatch -N   — same PART NUMBER appears with different SAP NUMBER values
  sap duplicate -N  — same SAP NUMBER appears with different PART NUMBER values
  spare duplicate -N — same TAG NO + PART NUMBER repeats

Each distinct issue group gets one stable counter value shared by every row in
that same group. Counters increment only when a new issue group appears.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from spir_dynamic.utils.logging import timed

log = logging.getLogger(__name__)


def _norm(v) -> str:
    return str(v or "").strip().upper()


@timed
def deduplicate_rows(rows: list[list], CI: dict) -> list[list]:
    """Apply ERROR rules in-place and return the modified rows."""
    tag_col = CI.get("TAG NO")
    part_col = CI.get("MANUFACTURER PART NUMBER")
    sap_col = CI.get("SAP NUMBER")
    flag_col = CI.get("ERROR")

    if flag_col is None:
        return rows

    for row in rows:
        if flag_col < len(row):
            row[flag_col] = 0

    def _get(row: list, col) -> str:
        if col is None or col >= len(row):
            return ""
        return _norm(row[col])

    row_labels: list[list[str]] = [[] for _ in rows]

    sap_mismatch_counter = 1
    sap_duplicate_counter = 1
    spare_duplicate_counter = 1

    # Rule 1 — same PART NUMBER, different SAP NUMBER
    part_to_saps: dict[str, set[str]] = defaultdict(set)
    part_to_rows: dict[str, list[int]] = defaultdict(list)
    if part_col is not None and sap_col is not None:
        for idx, row in enumerate(rows):
            part = _get(row, part_col)
            sap = _get(row, sap_col)
            if not part or not sap or sap in ("0", "0.0"):
                continue
            part_to_saps[part].add(sap)
            part_to_rows[part].append(idx)

        for part in sorted(part_to_saps):
            if len(part_to_saps[part]) <= 1:
                continue
            label = f"sap mismatch -{sap_mismatch_counter}"
            for row_idx in part_to_rows[part]:
                row_labels[row_idx].append(label)
            sap_mismatch_counter += 1

    # Rule 2 — same SAP NUMBER, different PART NUMBER
    sap_to_parts: dict[str, set[str]] = defaultdict(set)
    sap_to_rows: dict[str, list[int]] = defaultdict(list)
    if sap_col is not None and part_col is not None:
        for idx, row in enumerate(rows):
            sap = _get(row, sap_col)
            part = _get(row, part_col)
            if not sap or sap in ("0", "0.0") or not part:
                continue
            sap_to_parts[sap].add(part)
            sap_to_rows[sap].append(idx)

        for sap in sorted(sap_to_parts):
            if len(sap_to_parts[sap]) <= 1:
                continue
            label = f"sap duplicate -{sap_duplicate_counter}"
            for row_idx in sap_to_rows[sap]:
                row_labels[row_idx].append(label)
            sap_duplicate_counter += 1

    # Rule 3 — same TAG NO, same PART NUMBER repeated
    tag_part_to_rows: dict[tuple[str, str], list[int]] = defaultdict(list)
    if tag_col is not None and part_col is not None:
        for idx, row in enumerate(rows):
            tag = _get(row, tag_col)
            part = _get(row, part_col)
            if not tag or not part:
                continue
            tag_part_to_rows[(tag, part)].append(idx)

        for key in sorted(tag_part_to_rows):
            group_rows = tag_part_to_rows[key]
            if len(group_rows) <= 1:
                continue
            label = f"spare duplicate -{spare_duplicate_counter}"
            for row_idx in group_rows:
                row_labels[row_idx].append(label)
            spare_duplicate_counter += 1

    mismatch_count = 0
    sap_dup_count = 0
    spare_dup_count = 0
    for idx, row in enumerate(rows):
        labels = row_labels[idx]
        if not labels:
            if flag_col < len(row):
                row[flag_col] = 0
            continue
        mismatch_count += sum(1 for label in labels if label.startswith("sap mismatch"))
        sap_dup_count += sum(1 for label in labels if label.startswith("sap duplicate"))
        spare_dup_count += sum(1 for label in labels if label.startswith("spare duplicate"))
        if flag_col < len(row):
            row[flag_col] = ", ".join(labels)

    if mismatch_count or sap_dup_count or spare_dup_count:
        log.info(
            "ERROR labels applied: sap_mismatch=%d sap_duplicate=%d spare_duplicate=%d",
            mismatch_count,
            sap_dup_count,
            spare_dup_count,
        )

    return rows


def analyse_duplicates(rows: list[list]) -> dict:
    """Return counts of error labels across all rows."""
    from spir_dynamic.extraction.output_schema import CI

    flag_col = CI.get("ERROR")
    tag_col = CI.get("TAG NO", 0)
    desc_col = CI.get("DESCRIPTION OF PARTS", 1)

    dup1_count = 0
    sap_count = 0
    dup_items: list[dict] = []

    for row in rows:
        if flag_col is None or flag_col >= len(row):
            continue
        raw_flag = row[flag_col]
        if raw_flag in (None, "", 0):
            continue
        labels = [part.strip() for part in str(raw_flag).split(",") if part.strip()]
        for label in labels:
            lower_label = label.lower()
            item = {
                "type": label,
                "tag": row[tag_col] if tag_col < len(row) else None,
                "description": row[desc_col] if desc_col < len(row) else None,
            }
            dup_items.append(item)
            if lower_label.startswith("sap mismatch") or lower_label.startswith("sap duplicate"):
                sap_count += 1
            if lower_label.startswith("spare duplicate"):
                dup1_count += 1

    return {
        "dup1_count": dup1_count,
        "sap_count": sap_count,
        "dup_items": dup_items,
    }
