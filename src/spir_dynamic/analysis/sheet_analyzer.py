"""
Per-sheet content analysis orchestrator.

Combines header detection, column mapping, and tag location to build
a complete SheetProfile for a single worksheet.
"""
from __future__ import annotations

import logging

from spir_dynamic.models.sheet_profile import SheetProfile, SheetRole, TagLayout
from spir_dynamic.analysis.header_detector import (
    find_header_row,
    find_metadata,
    find_data_end,
)
from spir_dynamic.analysis.column_mapper import map_headers, get_unmapped_columns
from spir_dynamic.analysis.tag_locator import locate_tags
from spir_dynamic.utils.logging import timed

log = logging.getLogger(__name__)

# Sheet names that indicate non-data utility sheets (fallback defaults)
UTILITY_KEYWORDS = frozenset(
    {
        "validation",
        "lookup",
        "reference",
        "dropdown",
        "list",
        "instructions",
        "cover",
        "summary",
        "index",
        "contents",
        "change log",
        "revision",
        "spir review",
        "review sheet",
        "guidlines",
        "guidelines",
        "combined",
    }
)


def _get_utility_keywords() -> frozenset[str]:
    """Return utility sheet keywords from config, falling back to defaults."""
    try:
        from spir_dynamic.app.config import load_keywords
        kw = load_keywords().get("utility_sheet_keywords")
        if kw:
            return frozenset(kw)
    except Exception:
        pass
    return UTILITY_KEYWORDS


@timed
def analyze_sheet(ws, sheet_name: str) -> SheetProfile:
    """
    Analyze a single worksheet and build a complete SheetProfile.

    Steps:
      1. Quick-skip obvious utility sheets
      2. Find the header row via keyword scoring
      3. Map columns to logical field names
      4. Detect where tags are located
      5. Extract metadata from the header area
      6. Determine the data range
      7. Assign role and confidence
    """
    profile = SheetProfile(name=sheet_name)

    # Step 1: Quick utility check (name-based hint)
    name_lower = sheet_name.lower().strip()
    if any(kw in name_lower for kw in _get_utility_keywords()):
        profile.role = SheetRole.UTILITY
        profile.confidence = 0.8
        log.debug("Sheet '%s' classified as UTILITY by name", sheet_name)
        return profile

    # Step 2: Find header row
    profile.header_row = find_header_row(ws)

    if profile.header_row is None:
        # No recognizable header — might still be useful, try tag detection
        tag_result = locate_tags(ws, None, {})
        if tag_result.layout == TagLayout.NONE:
            # Check if sheet has minimal content
            if _is_empty_sheet(ws):
                profile.role = SheetRole.UTILITY
                profile.confidence = 0.5
                return profile
            # Has content but no headers — could be a simple list
            profile.role = SheetRole.UNKNOWN
            profile.confidence = 0.2
            return profile
        # Has tags but no standard header — still try to extract
        _apply_tag_result(profile, tag_result)
        profile.role = SheetRole.DATA
        profile.confidence = tag_result.confidence * 0.7
        return profile

    # Step 3: Map columns
    try:
        from spir_dynamic.app.config import get_settings
        min_score = get_settings().min_column_map_score
    except Exception:
        min_score = 30
    profile.column_map = map_headers(ws, profile.header_row, min_score=min_score)
    if profile.column_map:
        required = {"description", "item_number", "quantity", "unit_price", "part_number", "supplier", "currency"}
        hit_required = sorted(required.intersection(profile.column_map.keys()))
        log.debug(
            "Sheet '%s': mapping hit required=%s (%d/%d) in header_row=%d",
            sheet_name,
            hit_required,
            len(hit_required),
            len(required),
            profile.header_row,
        )

    # Capture extra columns (not mapped to standard fields)
    profile.extra_columns = get_unmapped_columns(ws, profile.header_row, profile.column_map)

    # Step 4: Locate tags
    tag_result = locate_tags(ws, profile.header_row, profile.column_map)
    _apply_tag_result(profile, tag_result)

    # Step 5: Extract metadata
    profile.metadata = find_metadata(ws, profile.header_row)

    # Step 6: Determine data range
    profile.data_start_row = profile.header_row + 1
    profile.data_end_row = find_data_end(ws, profile.header_row)
    profile.row_count = max(0, profile.data_end_row - profile.data_start_row + 1)

    # Step 7: Assign role
    if tag_result.layout == TagLayout.ROW_HEADERS:
        profile.role = SheetRole.ANNEXURE
    elif profile.column_map or tag_result.layout != TagLayout.NONE:
        profile.role = SheetRole.DATA
    else:
        profile.role = SheetRole.UNKNOWN

    # Boost role confidence based on content richness
    col_score = min(len(profile.column_map) / 5.0, 1.0)
    profile.confidence = (tag_result.confidence + col_score) / 2.0

    # Name-based hints (boost confidence, never override)
    # Promote DATA sheets whose name contains "annexure" to ANNEXURE,
    # but NOT if the sheet has COLUMN_HEADERS layout WITH a description column —
    # those are spare-data sheets that happen to be named "Annexure", not tag-list sheets.
    if "annexure" in name_lower and profile.role == SheetRole.DATA:
        is_spare_data_sheet = (
            tag_result.layout == TagLayout.COLUMN_HEADERS
            and bool(profile.column_map.get("description"))
        )
        if not is_spare_data_sheet:
            profile.role = SheetRole.ANNEXURE
            profile.confidence = min(profile.confidence + 0.1, 1.0)

    # Discovery mode: if confidence is very low and no tags found, re-run
    # column mapping with a lower threshold to pick up partial matches.
    if profile.confidence < 0.4 and tag_result.layout == TagLayout.NONE and not profile.column_map:
        try:
            from spir_dynamic.app.config import get_settings
            disc_score = get_settings().discovery_min_score
        except Exception:
            disc_score = 15
        disc_map = map_headers(ws, profile.header_row, min_score=disc_score)
        if disc_map:
            profile.column_map = disc_map
            profile.discovery_mode = True
            profile.role = SheetRole.DATA
            profile.confidence = min(len(disc_map) / 10.0, 0.35)
            log.warning(
                "Sheet '%s': discovery mode active — %d columns found at low threshold",
                sheet_name, len(disc_map),
            )

    log.info(
        "Sheet '%s': role=%s, tags=%s, header_row=%s, cols=%d, rows=%d, conf=%.2f%s",
        sheet_name, profile.role.value, profile.tag_layout.value,
        profile.header_row, len(profile.column_map), profile.row_count,
        profile.confidence,
        " [DISCOVERY]" if profile.discovery_mode else "",
    )
    return profile


def _apply_tag_result(profile: SheetProfile, tag_result) -> None:
    """Copy tag location results into the profile."""
    profile.tag_layout = tag_result.layout
    profile.tag_columns = tag_result.tag_columns
    profile.tag_rows = tag_result.tag_rows
    profile.tag_column_index = tag_result.tag_column_index
    profile.global_tag = tag_result.global_tag


def _is_empty_sheet(ws, check_rows: int = 10) -> bool:
    """Check if a sheet has virtually no content."""
    max_col = min(ws.max_column or 10, 20)
    non_empty = 0
    for r in range(1, min(check_rows + 1, (ws.max_row or 0) + 1)):
        for c in range(1, max_col + 1):
            if ws.cell(r, c).value is not None:
                non_empty += 1
                if non_empty > 3:
                    return False
    return True
