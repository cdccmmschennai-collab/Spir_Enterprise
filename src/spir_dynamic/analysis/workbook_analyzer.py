"""
Workbook-level analysis — analyzes all sheets and detects relationships.

After per-sheet analysis, this module:
  1. Detects continuation relationships (content overlap between sheets)
  2. Resolves global metadata (SPIR NO, manufacturer, etc.)
  3. Returns the final list of SheetProfiles ready for extraction
"""
from __future__ import annotations

import logging
import re
from typing import Any

from spir_dynamic.models.sheet_profile import SheetProfile, SheetRole, TagLayout
from spir_dynamic.analysis.sheet_analyzer import analyze_sheet
from spir_dynamic.utils.logging import timed

log = logging.getLogger(__name__)


@timed
def analyze_workbook(wb, filename: str = "") -> list[SheetProfile]:
    """
    Analyze all sheets in a workbook and return enriched SheetProfiles.

    Steps:
      1. Analyze each sheet independently (sequential)
      2. Detect continuation relationships (order-dependent)
      3. Exclude sheets from foreign embedded SPIR documents
      4. Propagate global metadata (order-dependent)

    _detect_continuations and _propagate_metadata are ORDER-DEPENDENT and
    must run after all sheets are analyzed in wb.sheetnames order.
    """
    profiles: list[SheetProfile] = []
    for sheet_name in wb.sheetnames:
        try:
            ws = wb[sheet_name]
            if getattr(ws, 'sheet_state', 'visible') != 'visible':
                log.info("Skipping hidden sheet '%s' (state=%s)", sheet_name, ws.sheet_state)
                continue
            profile = analyze_sheet(ws, sheet_name)
            profiles.append(profile)
        except Exception as exc:
            log.warning("Failed to analyze sheet '%s': %s", sheet_name, exc)
            profiles.append(SheetProfile(name=sheet_name, role=SheetRole.UNKNOWN))

    _detect_continuations(profiles)
    # Exclude sheets whose embedded SPIR number belongs to a different document.
    # Must run BEFORE _propagate_metadata so foreign metadata is never spread
    # to the valid sheets.
    _exclude_foreign_spir_sheets(profiles, filename)
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


def _exclude_foreign_spir_sheets(profiles: list[SheetProfile], filename: str) -> None:
    """
    Mark sheets that belong to a different embedded SPIR document as UTILITY.

    Some workbooks contain sheets from multiple SPIR documents (different
    equipment with different SPIR numbers). A sheet is foreign when it has an
    explicit SPIR number in its metadata that does NOT match the first 4
    hyphen-separated segments of the file's own SPIR number (derived from
    filename).  Sheets without an explicit embedded SPIR number are left alone.
    """
    if not filename:
        return

    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    m = re.search(r"([A-Z0-9]{2,}-[A-Z0-9]{2,}-[A-Z0-9][\w\-]*)", stem, re.IGNORECASE)
    if not m:
        return

    file_spir = m.group(1)

    for p in profiles:
        sheet_spir = p.metadata.get("spir_no")
        if not sheet_spir:
            continue  # No explicit SPIR number — do not exclude

        if _spir_differs(str(sheet_spir).strip(), file_spir):
            log.info(
                "Sheet '%s' excluded: embedded SPIR '%s' differs from file SPIR '%s'",
                p.name, sheet_spir, file_spir,
            )
            p.role = SheetRole.UTILITY
            p.confidence = 0.9


def _spir_differs(a: str, b: str) -> bool:
    """True if two SPIR numbers clearly belong to different documents.

    Compares the first 4 hyphen-separated segments (e.g. VEN-4460-DGTYP-4).
    A different 4th segment means a different equipment series within the same
    project (e.g. -4- vs -5-), which is a distinct document.
    """
    parts_a = a.lower().strip().split("-")
    parts_b = b.lower().strip().split("-")
    n = min(4, len(parts_a), len(parts_b))
    return parts_a[:n] != parts_b[:n]


def _propagate_metadata(profiles: list[SheetProfile]) -> None:
    """
    Propagate metadata from the first data sheet with rich metadata
    to other sheets that have less.
    """
    # Only use extractable sheets as metadata sources — UTILITY/foreign sheets
    # (e.g., sheets from an embedded different SPIR) must not pollute valid sheets.
    best_meta: dict[str, Any] = {}
    for p in profiles:
        if not p.is_extractable:
            continue
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
