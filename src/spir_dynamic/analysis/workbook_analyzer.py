"""
Workbook-level analysis — analyzes all sheets and detects relationships.

After per-sheet analysis, this module:
  1. Detects continuation relationships (content overlap between sheets)
  2. Resolves global metadata (SPIR NO, manufacturer, etc.)
  3. Returns the final list of SheetProfiles ready for extraction
"""
from __future__ import annotations

import logging
from typing import Any

from spir_dynamic.models.sheet_profile import SheetProfile, SheetRole, TagLayout
from spir_dynamic.analysis.sheet_analyzer import analyze_sheet

log = logging.getLogger(__name__)


def analyze_workbook(wb) -> list[SheetProfile]:
    """
    Analyze all sheets in a workbook and return enriched SheetProfiles.

    Steps:
      1. Analyze each sheet independently
      2. Detect continuation relationships
      3. Propagate global metadata
    """
    profiles: list[SheetProfile] = []

    for sheet_name in wb.sheetnames:
        try:
            ws = wb[sheet_name]
            profile = analyze_sheet(ws, sheet_name)
            profiles.append(profile)
        except Exception as exc:
            log.warning("Failed to analyze sheet '%s': %s", sheet_name, exc)
            profiles.append(SheetProfile(name=sheet_name, role=SheetRole.UNKNOWN))

    # Detect continuation relationships
    _detect_continuations(profiles)

    # Propagate metadata from the first data sheet to others
    _propagate_metadata(profiles)

    extractable = [p for p in profiles if p.is_extractable]
    log.info(
        "Workbook analysis complete: %d sheets total, %d extractable",
        len(profiles), len(extractable),
    )

    return profiles


def _detect_continuations(profiles: list[SheetProfile]) -> None:
    """
    Detect continuation relationships between sheets.

    For COLUMN_HEADERS layout: do NOT mark as CONTINUATION — the unified
    extractor handles cross-sheet item sharing for all COLUMN_HEADERS sheets
    as a group. They all stay as DATA.

    For other layouts: detect continuations based on column overlap and
    name hints.
    """
    data_sheets = [p for p in profiles if p.role in (SheetRole.DATA, SheetRole.UNKNOWN)]
    if len(data_sheets) < 2:
        return

    for i, sheet in enumerate(data_sheets):
        if sheet.role == SheetRole.CONTINUATION:
            continue

        # Skip COLUMN_HEADERS sheets — they're handled as a group
        if sheet.tag_layout == TagLayout.COLUMN_HEADERS:
            continue

        name_lower = sheet.name.lower()
        has_cont_hint = any(
            kw in name_lower
            for kw in ("continuation", "cont ", "continued", "cont.", "overflow")
        )

        if has_cont_hint:
            parent = _find_parent_sheet(sheet, data_sheets[:i])
            if parent:
                sheet.role = SheetRole.CONTINUATION
                sheet.continuation_of = parent.name
                sheet.confidence = min(sheet.confidence + 0.15, 1.0)
                log.debug(
                    "Sheet '%s' is continuation of '%s' (name hint + structure match)",
                    sheet.name, parent.name,
                )
                continue

        for j in range(i):
            prev = data_sheets[j]
            if prev.role not in (SheetRole.DATA, SheetRole.ANNEXURE):
                continue

            col_overlap = _column_overlap(sheet.column_map, prev.column_map)
            if col_overlap >= 0.7:
                if sheet.tag_layout == prev.tag_layout and sheet.tag_layout != TagLayout.NONE:
                    sheet.role = SheetRole.CONTINUATION
                    sheet.continuation_of = prev.name
                    sheet.confidence = min(col_overlap * 0.8, 1.0)
                    log.debug(
                        "Sheet '%s' detected as continuation of '%s' (%.0f%% col overlap)",
                        sheet.name, prev.name, col_overlap * 100,
                    )
                    break


def _find_parent_sheet(
    candidate: SheetProfile,
    earlier_sheets: list[SheetProfile],
) -> SheetProfile | None:
    """Find the most likely parent sheet for a continuation candidate."""
    best_parent = None
    best_overlap = 0.0

    for sheet in reversed(earlier_sheets):
        if sheet.role == SheetRole.CONTINUATION:
            continue

        overlap = _column_overlap(candidate.column_map, sheet.column_map)
        if overlap > best_overlap:
            best_overlap = overlap
            best_parent = sheet

    # Accept if column overlap is decent (or if there's only one candidate)
    if best_parent and (best_overlap >= 0.5 or len(earlier_sheets) == 1):
        return best_parent

    # Fall back: return the immediately preceding data sheet
    for sheet in reversed(earlier_sheets):
        if sheet.role in (SheetRole.DATA, SheetRole.ANNEXURE):
            return sheet

    return None


def _column_overlap(map_a: dict[str, int], map_b: dict[str, int]) -> float:
    """Compute the overlap ratio between two column maps."""
    if not map_a or not map_b:
        return 0.0

    keys_a = set(map_a.keys())
    keys_b = set(map_b.keys())
    intersection = keys_a & keys_b
    union = keys_a | keys_b

    return len(intersection) / len(union) if union else 0.0


def _propagate_metadata(profiles: list[SheetProfile]) -> None:
    """
    Propagate metadata from the first data sheet with rich metadata
    to other sheets that have less.
    """
    # Find the best metadata source
    best_meta: dict[str, Any] = {}
    for p in profiles:
        if p.metadata and len(p.metadata) > len(best_meta):
            best_meta = p.metadata

    if not best_meta:
        return

    # Fill in missing metadata on other sheets
    for p in profiles:
        if not p.is_extractable:
            continue
        for key, val in best_meta.items():
            if key not in p.metadata:
                p.metadata[key] = val
