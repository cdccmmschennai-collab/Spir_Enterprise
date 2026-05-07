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
import re
from collections import defaultdict

from spir_dynamic.utils.logging import timed

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers for SPARE DUPLICATE reference building
# ---------------------------------------------------------------------------

_NON_MAIN_NAME_RE = re.compile(
    r"continuation|conti\b|cont[.\-\s]|continued|overflow|annexure|annex\b",
    re.IGNORECASE,
)


def _nat_sort_key(s: str) -> list:
    parts = re.split(r"(\d+)", s)
    return [int(p) if p.isdigit() else p for p in parts]


def _build_sheet_idx_map(rows: list[list], sheet_col) -> dict[str, int]:
    """Map normalised (upper) sheet name → 1-based main-sheet index."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for row in rows:
        if sheet_col is None or sheet_col >= len(row):
            continue
        name = str(row[sheet_col] or "").strip().upper()
        if name and name not in seen_set:
            seen_set.add(name)
            seen.append(name)
    main_sheets = sorted(
        [n for n in seen if not _NON_MAIN_NAME_RE.search(n)],
        key=_nat_sort_key,
    )
    return {name: i + 1 for i, name in enumerate(main_sheets)}


_OMN_SUFFIX_RE = re.compile(r'(\d*)L(\d+)$', re.IGNORECASE)


def _ref_from_omn(omn: str) -> str | None:
    """Extract '01L04' reference from an OMN string."""
    if '-' in omn:
        # Strict 18-char format: suffix is always the last '-'-separated segment
        suffix = omn.rsplit('-', 1)[-1]
        m = re.fullmatch(r'(\d*)L(\d+)', suffix, re.IGNORECASE)
        if m:
            sheet = int(m.group(1)) if m.group(1) else 1
            line = int(m.group(2))
            return f"{sheet:02d}L{line:02d}"
    # Fallback: flexible/fused OMN — search at end of string
    m = _OMN_SUFFIX_RE.search(omn)
    if m:
        sheet = int(m.group(1)) if m.group(1) else 1
        line = int(m.group(2))
        return f"{sheet:02d}L{line:02d}"
    return None


def _ref_from_identity(identity: str, sheet_idx_map: dict[str, int]) -> str:
    """Build '01L04' reference from OMN string or 'SHEETNAME|ITEM' fallback key."""
    if '|' in identity:
        sname, item_str = identity.split('|', 1)
        sheet_idx = sheet_idx_map.get(sname, 1)
        try:
            item_num = int(float(item_str))
        except (ValueError, TypeError):
            item_num = 0
        return f"{sheet_idx:02d}L{item_num:02d}"
    ref = _ref_from_omn(identity)
    return ref if ref else identity


def _label_priority(label: str) -> int:
    """Sort order: spare duplicate (0) → sap number duplicate (1) → sap number mismatch (2)."""
    l = label.lower()
    if l.startswith("spare duplicate"):
        return 0
    if l.startswith("sap number duplicate"):
        return 1
    if l.startswith("sap number mismatch"):
        return 2
    return 99


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
            label = f"sap number mismatch-{sap_mismatch_counter}"
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
            label = f"sap number duplicate-{sap_duplicate_counter}"
            for row_idx in sap_to_rows[sap]:
                row_labels[row_idx].append(label)
            sap_duplicate_counter += 1

    # Rule 3 — SPARE DUPLICATE: same PART NUMBER, different OMN identity
    omn_col_idx = CI.get("OLD MATERIAL NUMBER/SPF NUMBER")
    item_col_idx = CI.get("ITEM NUMBER")
    sheet_col_idx = CI.get("SHEET")

    if part_col is not None:
        sheet_idx_map = _build_sheet_idx_map(rows, sheet_col_idx)

        # part → { identity_key: [row_indices] }
        # identity_key = OMN string (primary); fallback = "SHEETNAME|ITEM" when OMN absent
        part_to_identity: dict[str, dict] = defaultdict(lambda: defaultdict(list))
        for idx, row in enumerate(rows):
            part = _get(row, part_col)
            if not part:
                continue
            omn = _get(row, omn_col_idx) if omn_col_idx is not None else ""
            if omn:
                identity = omn
            else:
                item_str = _get(row, item_col_idx) if item_col_idx is not None else ""
                if not item_str:
                    continue  # no identity → header row, skip
                sname = _get(row, sheet_col_idx) if sheet_col_idx is not None else ""
                identity = f"{sname}|{item_str}"
            part_to_identity[part][identity].append(idx)

        for part in sorted(part_to_identity):
            identities = part_to_identity[part]
            if len(identities) <= 1:
                continue  # all same OMN identity → not a duplicate

            def _identity_sort_key(ident, _map=sheet_idx_map):
                ref = _ref_from_identity(ident, _map)
                m = re.search(r'(\d+)L(\d+)', ref, re.IGNORECASE)
                if m:
                    return (int(m.group(1)), int(m.group(2)))
                return (99, 99)

            sorted_ids = sorted(identities.keys(), key=_identity_sort_key)
            refs = [_ref_from_identity(ident, sheet_idx_map) for ident in sorted_ids]
            label = f"spare duplicate-{' & '.join(refs)}"

            for ident in sorted_ids:
                for row_idx in identities[ident]:
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
        mismatch_count += sum(1 for label in labels if label.startswith("sap number mismatch"))
        sap_dup_count += sum(1 for label in labels if label.startswith("sap number duplicate"))
        spare_dup_count += sum(1 for label in labels if label.startswith("spare duplicate"))
        if flag_col < len(row):
            unique = list(dict.fromkeys(labels))
            unique.sort(key=_label_priority)
            row[flag_col] = ",".join(unique)

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
            if lower_label.startswith("sap number mismatch") or lower_label.startswith("sap number duplicate"):
                sap_count += 1
            if lower_label.startswith("spare duplicate"):
                dup1_count += 1

    return {
        "dup1_count": dup1_count,
        "sap_count": sap_count,
        "dup_items": dup_items,
    }
