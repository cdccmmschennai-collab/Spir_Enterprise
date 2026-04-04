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
from spir_dynamic.analysis.column_mapper import map_headers
from spir_dynamic.analysis.tag_locator import locate_tags

log = logging.getLogger(__name__)

# Sheet names that indicate non-data utility sheets
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
    }
)


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
    if any(kw in name_lower for kw in UTILITY_KEYWORDS):
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
    profile.column_map = map_headers(ws, profile.header_row)
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
    if "annexure" in name_lower and profile.role == SheetRole.DATA:
        profile.role = SheetRole.ANNEXURE
        profile.confidence = min(profile.confidence + 0.1, 1.0)

    log.info(
        "Sheet '%s': role=%s, tags=%s, header_row=%s, cols=%d, rows=%d, conf=%.2f",
        sheet_name, profile.role.value, profile.tag_layout.value,
        profile.header_row, len(profile.column_map), profile.row_count,
        profile.confidence,
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
