"""
Unified extractor — single entry point for all Excel workbook extraction.

Replaces the 8-format-parser + dispatcher system with content-based analysis.
Analyzes each sheet independently, selects the right extraction strategy
based on where tags are found, and merges results.

Special handling for COLUMN_HEADERS layout:
  - Identifies the "item source" sheet (has descriptions/prices)
  - Reads items once from it
  - Passes items_dict to continuation sheets for cross-referencing

Equipment enrichment:
  - Annexure sheets may contain tag→model/serial/make mappings
  - After extraction, equipment data is merged into spare rows
  - Annexure-reference tags ("Annexure 1") are resolved to real tags
"""
from __future__ import annotations

import logging
import re
from typing import Any

from spir_dynamic.models.sheet_profile import SheetProfile, SheetRole, TagLayout
from spir_dynamic.analysis.workbook_analyzer import analyze_workbook
from spir_dynamic.extraction.strategies.tabular import TabularStrategy
from spir_dynamic.extraction.strategies.columnar import ColumnarStrategy
from spir_dynamic.extraction.strategies.transposed import TransposedStrategy
from spir_dynamic.extraction.output_schema import row_from_dict
from spir_dynamic.utils.cell_utils import clean_str, clean_num, split_tags
from spir_dynamic.utils.logging import timed

log = logging.getLogger(__name__)

_tabular = TabularStrategy()
_columnar = ColumnarStrategy()
_transposed = TransposedStrategy()

# Pattern to detect annexure-reference tag values like "Annexure 1", "ANNEXURE-2",
# "Refer Annexure 3", "ANNEXURE (P1)-1", "ANNEXURE (P2)-3", "REFER TO ANNEX 4",
# "ANNEXURES-1" (plural form used in some SPIR files), etc.
# Matches "annexures?" (with or without plural S), bare "annex", and handles
# "refer to" as well as plain "refer".
_ANNEXURE_REF_RE = re.compile(
    r"(?:refer(?:\s+to)?\s+)?annex(?:ures?)?[\s\-_]*(?:\(([^)]*)\)[\s\-_]*)?(\d+|[IVX]+)\b",
    re.IGNORECASE,
)

# Unicode Roman numeral characters (U+2160–U+217B) → ASCII equivalents.
# Excel files sometimes use these Unicode glyphs instead of plain I/V/X letters,
# so "Annexure Ⅵ" (U+2165) would never match the [IVX]+ regex without this map.
_UNICODE_ROMAN_MAP: dict[str, str] = {
    'Ⅰ': 'I',   'Ⅱ': 'II',   'Ⅲ': 'III',  'Ⅳ': 'IV',
    'Ⅴ': 'V',   'Ⅵ': 'VI',   'Ⅶ': 'VII',  'Ⅷ': 'VIII',
    'Ⅸ': 'IX',  'Ⅹ': 'X',    'Ⅺ': 'XI',   'Ⅻ': 'XII',
    'ⅰ': 'i',   'ⅱ': 'ii',   'ⅲ': 'iii',  'ⅳ': 'iv',
    'ⅴ': 'v',   'ⅵ': 'vi',   'ⅶ': 'vii',  'ⅷ': 'viii',
    'ⅸ': 'ix',  'ⅹ': 'x',    'ⅺ': 'xi',   'ⅻ': 'xii',
}


def _roman_to_int(s: str) -> int | None:
    """Convert a roman numeral string to int. Returns None if not valid."""
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    s = s.upper().strip()
    if not s or not all(c in vals for c in s):
        return None
    result = 0
    prev = 0
    for ch in reversed(s):
        curr = vals[ch]
        result += curr if curr >= prev else -curr
        prev = curr
    return result if result > 0 else None


@timed
def extract_workbook(wb, filename: str = "") -> dict[str, Any]:
    """
    Extract all SPIR data from a workbook using dynamic content analysis.
    """
    # Step 1: Analyze all sheets
    profiles = analyze_workbook(wb)

    # Step 2: Resolve global SPIR number
    spir_no = _resolve_spir_no(profiles, filename)

    # Step 3: Handle COLUMN_HEADERS sheets with cross-sheet coordination
    all_rows: list[dict[str, Any]] = []
    format_parts: list[str] = []

    # Separate columnar sheets from other layouts
    # Exclude ANNEXURE-role sheets even if they happen to have COLUMN_HEADERS layout —
    # they belong only in the annexure registry, not the extraction pipeline.
    columnar_profiles = [
        p for p in profiles
        if p.is_extractable
        and p.tag_layout == TagLayout.COLUMN_HEADERS
        and p.role != SheetRole.ANNEXURE
    ]
    other_profiles = [
        p for p in profiles
        if p.is_extractable
        and p.tag_layout != TagLayout.COLUMN_HEADERS
        and p.role != SheetRole.ANNEXURE
    ]
    annexure_count = sum(1 for p in profiles if p.role == SheetRole.ANNEXURE)

    # Process COLUMN_HEADERS sheets with cross-sheet item sharing
    if columnar_profiles:
        rows = _extract_columnar_group(wb, columnar_profiles, spir_no, profiles)
        all_rows.extend(rows)
        for p in columnar_profiles:
            format_parts.append(f"{p.name}(column_headers)")

    # Process other layouts normally
    for profile in other_profiles:
        strategy = _get_strategy(profile)
        if strategy is None:
            continue

        try:
            ws = wb[profile.name]
            sheet_rows = strategy.extract(ws, profile, spir_no)
            all_rows.extend(sheet_rows)
            format_parts.append(f"{profile.name}({profile.tag_layout.value})")

        except Exception as exc:
            log.error("Extraction failed for '%s': %s", profile.name, exc, exc_info=True)

    # Step 4: Equipment enrichment — resolve annexure references + merge equipment data
    all_rows = _enrich_equipment_data(wb, all_rows, profiles)

    # Step 5: Collect metadata
    metadata = _collect_metadata(profiles)

    # Step 6: Compute statistics
    unique_tags = set()
    for row in all_rows:
        tag = row.get("tag_no") or row.get("tag")
        if tag:
            unique_tags.add(str(tag).strip().upper())

    spare_items = sum(1 for r in all_rows if r.get("item_num"))

    from spir_dynamic.extraction.output_schema import OUTPUT_COLS

    result = {
        "format": " + ".join(format_parts) if format_parts else "UNKNOWN",
        "spir_no": spir_no,
        "equipment": metadata.get("equipment", ""),
        "manufacturer": metadata.get("manufacturer", ""),
        "supplier": metadata.get("supplier", ""),
        "spir_type": metadata.get("spir_type"),
        "eqpt_qty": len(unique_tags),
        "spare_items": spare_items,
        "total_tags": len(unique_tags),
        "annexure_count": annexure_count,
        "rows": all_rows,
        "output_cols": OUTPUT_COLS,
        "sheet_profiles": [
            {
                "name": p.name,
                "role": p.role.value,
                "tag_layout": p.tag_layout.value,
                "header_row": p.header_row,
                "row_count": p.row_count,
                "confidence": p.confidence,
                "columns_mapped": list(p.column_map.keys()),
                "column_map": p.column_map,
                "extra_columns": p.extra_columns,
                "discovery_mode": p.discovery_mode,
            }
            for p in profiles
        ],
    }

    log.info(
        "Extraction complete: %d rows, %d tags, %d sheets, format=%s",
        len(all_rows), len(unique_tags), len(format_parts), result["format"],
    )
    return result


def _extract_columnar_group(
    wb,
    columnar_profiles: list[SheetProfile],
    spir_no: str,
    all_profiles: list[SheetProfile],
) -> list[dict[str, Any]]:
    """
    Handle a group of COLUMN_HEADERS sheets with cross-sheet item sharing.

    When multiple independent main sheets exist (each with its own item list),
    routes each main sheet + its continuation sheets as a separate extraction
    group so their items_dict never cross-contaminate.
    """
    # A "primary main" sheet owns its own item list: it has item_number, description,
    # and at least 4 mapped columns (price, part_no, etc.).
    def _is_primary_main(p: SheetProfile) -> bool:
        return (
            "item_number" in p.column_map
            and "description" in p.column_map
            and len(p.column_map) >= 4
        )

    primary_mains = [p for p in columnar_profiles if _is_primary_main(p)]
    non_primaries = [p for p in columnar_profiles if not _is_primary_main(p)]

    # Single main (or no primaries): use the original single-group logic unchanged.
    if len(primary_mains) <= 1:
        return _extract_single_group(wb, columnar_profiles, spir_no)

    # Multiple independent main sheets: partition non-primaries by parent main.
    groups = _group_by_main(primary_mains, non_primaries, all_profiles)

    all_rows: list[dict[str, Any]] = []
    for main_profile, group_conts in groups:
        group_sheets = [main_profile] + group_conts
        group_rows = _extract_single_group(wb, group_sheets, spir_no)
        all_rows.extend(group_rows)

    return all_rows


def _group_by_main(
    primary_mains: list[SheetProfile],
    non_primaries: list[SheetProfile],
    all_profiles: list[SheetProfile],
) -> list[tuple[SheetProfile, list[SheetProfile]]]:
    """
    Pair each continuation/reference sheet with its parent primary main sheet.

    Matching priority:
      1. Number extracted from sheet name: "Conti Sheet- 4" → 4 matches "Main Sheet-4"
      2. Positional fallback: assign to the preceding primary main in workbook order.
    """
    # Build workbook order index for positional fallback
    sheet_order = {p.name: i for i, p in enumerate(all_profiles)}

    def _sheet_numbers(name: str) -> set[int]:
        """Extract all digit sequences from a sheet name as integers."""
        return {int(m) for m in re.findall(r"\d+", name)}

    # Map each primary main to its set of name numbers
    main_numbers = {p.name: _sheet_numbers(p.name) for p in primary_mains}

    # Sort primary mains by workbook order
    sorted_mains = sorted(primary_mains, key=lambda p: sheet_order.get(p.name, 0))

    groups: dict[str, list[SheetProfile]] = {p.name: [] for p in sorted_mains}

    for cont in non_primaries:
        cont_nums = _sheet_numbers(cont.name)
        best_main = None
        best_overlap = 0

        for main in sorted_mains:
            overlap = len(cont_nums & main_numbers[main.name])
            if overlap > best_overlap:
                best_overlap = overlap
                best_main = main

        if best_main is None or best_overlap == 0:
            # Positional fallback: assign to the last primary main that appears
            # before this sheet in the workbook.
            cont_pos = sheet_order.get(cont.name, 0)
            for main in reversed(sorted_mains):
                if sheet_order.get(main.name, 0) < cont_pos:
                    best_main = main
                    break

        if best_main is None:
            best_main = sorted_mains[0]

        groups[best_main.name].append(cont)

    return [(main, groups[main.name]) for main in sorted_mains]


def _extract_single_group(
    wb,
    columnar_profiles: list[SheetProfile],
    spir_no: str,
) -> list[dict[str, Any]]:
    """
    Extract one cohesive group of COLUMN_HEADERS sheets sharing a single items_dict.

    1. Find the "item source" sheet (has description/price columns)
    2. Read items from it
    3. Merge items from other sheets in this group that fill gaps (PHASE 2)
    4. Extract all sheets in this group using the shared items_dict
    """
    all_rows: list[dict[str, Any]] = []

    # Find the item source: the sheet with the richest column_map
    # (has description, unit_price, part_number etc.)
    item_source = _find_item_source(columnar_profiles)

    # Read items from the source sheet
    items_dict: dict[int, dict[str, Any]] = {}
    item_source_name = item_source.name if item_source else ""
    if item_source:
        ws = wb[item_source_name]
        items_dict = _columnar.read_items(ws, item_source)
        log.info(
            "Item source '%s': %d items read",
            item_source_name, len(items_dict),
        )

    # Fix B: capture boundary BEFORE PHASE 2 loop corrupts _last_item_source_row
    item_source_last_row = getattr(_columnar, "_last_item_source_row", None)

    # PHASE 2 FIX: Merge items from other sheets in THIS GROUP that have
    # description data missing from the item source.
    # Handles files where one main sheet splits items across two sub-sheets
    # (e.g., items 1-18 in MAIN SHEET, items 19-24 in MAIN SHEET (3)).
    for profile in columnar_profiles:
        if profile.name == item_source_name:
            continue
        ws = wb[profile.name]
        other_items = _columnar.read_items(ws, profile)
        for item_num, item_data in other_items.items():
            if item_num not in items_dict:
                items_dict[item_num] = item_data
            elif not items_dict[item_num].get("desc") and item_data.get("desc"):
                items_dict[item_num].update(item_data)

    # Fix C: ensure item source is extracted first so its metadata_field_rows
    # are available for continuation sheets
    ordered_profiles = sorted(
        columnar_profiles,
        key=lambda p: 0 if p.name == item_source_name else 1,
    )

    # Extract from each sheet in this group
    item_source_metadata_rows: dict[str, int] | None = None
    for profile in ordered_profiles:
        try:
            ws = wb[profile.name]
            # Fix B: restore boundary so _read_tag_item_mapping uses correct end_row
            _columnar._last_item_source_row = item_source_last_row
            is_item_source_sheet = profile.name == item_source_name
            rows = _columnar.extract(
                ws,
                profile,
                spir_no,
                items_dict=items_dict,
                metadata_field_rows=None if is_item_source_sheet else item_source_metadata_rows,
            )
            # Fix C: capture field rows from main sheet for continuation sheets
            if is_item_source_sheet:
                item_source_metadata_rows = getattr(_columnar, "_last_metadata_field_rows", None)
            all_rows.extend(rows)
        except Exception as exc:
            log.error(
                "Columnar extraction failed for '%s': %s",
                profile.name, exc, exc_info=True,
            )

    return all_rows


def _find_item_source(profiles: list[SheetProfile]) -> SheetProfile | None:
    """
    Find the sheet with the most data columns (description, price, etc.).
    This is the "main" sheet that has actual item information.
    """
    data_fields = {"description", "unit_price", "part_number", "supplier", "currency"}

    best = None
    best_score = 0

    for p in profiles:
        score = sum(1 for f in data_fields if f in p.column_map)
        if score > best_score:
            best_score = score
            best = p

    if best and best_score >= 2:
        return best

    # Fallback: return the first profile (often the main sheet)
    return profiles[0] if profiles else None


def _get_strategy(profile: SheetProfile):
    """Get the right strategy for a profile's tag layout."""
    mapping = {
        TagLayout.TAG_COLUMN: _tabular,
        TagLayout.GLOBAL_TAG: _tabular,
        TagLayout.ROW_HEADERS: _transposed,
    }
    strategy = mapping.get(profile.tag_layout)
    if strategy is None and profile.column_map:
        return _tabular
    return strategy


def _resolve_spir_no(profiles: list[SheetProfile], filename: str) -> str:
    """Resolve the SPIR number from sheet metadata or filename."""
    for p in profiles:
        spir = p.metadata.get("spir_no")
        if spir:
            spir_clean = str(spir).strip()
            # Accept any value that is at least 5 chars and contains alphanumerics
            if len(spir_clean) >= 5 and re.search(r'[A-Z0-9]', spir_clean, re.I):
                return spir_clean

    if filename:
        patterns = [
            r"([A-Z0-9]{2,}-[A-Z0-9]{2,}-[A-Z0-9][\w\-]*)",
            r"(\d{4,}[\-_]\w+[\-_]\w+)",
        ]
        name = filename.rsplit(".", 1)[0]
        for pat in patterns:
            m = re.search(pat, name, re.IGNORECASE)
            if m:
                return m.group(1)

    return ""


def _collect_metadata(profiles: list[SheetProfile]) -> dict[str, Any]:
    """Collect the richest metadata from all profiles."""
    combined: dict[str, Any] = {}
    for p in profiles:
        if not p.is_extractable:
            continue
        for key, val in p.metadata.items():
            if key not in combined and val:
                combined[key] = val
    return combined


# ---------------------------------------------------------------------------
# Equipment enrichment — annexure / continuation → main sheet mapping
# ---------------------------------------------------------------------------


def _resolve_subgroup_key(
    annex_key: str,
    registry: dict[str, list],
    eqpt_qty,
    prefer_prefix: str | None = None,
) -> str | None:
    """Find the correct subgroup registry key (e.g. 'ANNEXURE1-1') for a bare annexure
    reference (e.g. 'ANNEXURE1') using the numeric suffix and eqpt_qty disambiguation.

    prefer_prefix: when set (e.g. 'ANNEXURE2'), restricts candidates to keys with that
    exact prefix — used once a sheet's parent annexure has been identified, preventing
    false matches due to identical eqpt_qty across different annexure sheets.
    """
    num_match = re.search(r"(\d+)$", annex_key)
    if not num_match:
        return None
    suffix = f"-{num_match.group(1)}"
    candidates = [k for k in registry if k.endswith(suffix)]
    if not candidates:
        return None
    # Narrow to preferred prefix if we already know which annexure this sheet uses.
    # Use prefix + "-" to avoid "ANNEXURE2" falsely matching "ANNEXURE20-1".
    if prefer_prefix:
        prefixed = [k for k in candidates if k.startswith(prefer_prefix + "-")]
        if prefixed:
            candidates = prefixed
    if len(candidates) == 1:
        return candidates[0]
    # Multiple candidates — disambiguate by entry count matching eqpt_qty
    if eqpt_qty is not None:
        try:
            qty = int(float(eqpt_qty))
            best = [k for k in candidates if len(registry[k]) == qty]
            if len(best) == 1:
                return best[0]
        except (TypeError, ValueError):
            pass
    return None  # still ambiguous — caller decides


def _get_annexure_key(tag: str, registry: dict) -> str | None:
    """Return the registry key matching an annexure tag, or None if not found.

    Handles two cases:
      1. Raw tag ("REFER TO ANNEX 1") → normalises to "ANNEXURE1" → looks up in registry
      2. Pre-resolved subgroup key ("ANNEXURE1-1" set by Step 1c) → direct registry lookup,
         because _normalize_annexure_ref strips the subgroup suffix ("-1") and returns "ANNEXURE1"
         which is not a valid registry key when subgroups exist.
    """
    if not tag:
        return None
    annex_key = _normalize_annexure_ref(tag)
    if annex_key and annex_key in registry:
        return annex_key
    # Direct lookup for pre-resolved subgroup keys (e.g. "ANNEXURE1-1")
    tag_upper = tag.strip().upper()
    if tag_upper in registry:
        return tag_upper
    return None


_EQUIPMENT_FIELDS = ("manufacturer", "model", "serial")


def _enrich_equipment_data(
    wb,
    all_rows: list[dict[str, Any]],
    profiles: list[SheetProfile],
) -> list[dict[str, Any]]:
    """
    Enrich spare rows with equipment data from annexure & continuation sheets.

    Two modes:
      A) Annexure-reference resolution: main sheet tag is "Annexure 1" etc.
         → fan-out rows to actual tags from annexure sheet, with model/serial.
      B) Direct enrichment: main sheet has real tags but missing equipment fields
         → look up from annexure/continuation data and fill in.
    """
    # Step 1: Extract equipment registry from annexure sheets
    annexure_registry = _build_annexure_registry(wb, profiles)
    # annexure_registry: {normalized_annexure_key: [{"tag":..,"model":..,"serial":..,"manufacturer":..}, ...]}

    # Step 1b: Resolve bare "ANNEXURE_ANY" references (e.g. "Refer Annexure" without a number).
    # If exactly one annexure sheet exists, remap all ANNEXURE_ANY tags to its key.
    _real_keys = {k for k in annexure_registry if k != "ANNEXURE_ANY"}
    if len(_real_keys) == 1:
        _single_key = next(iter(_real_keys))
        for _row in all_rows:
            _tag = str(_row.get("tag_no") or "").strip()
            if _tag and _normalize_annexure_ref(_tag) == "ANNEXURE_ANY":
                _row["tag_no"] = _single_key
    elif len(_real_keys) > 1:
        # Multiple annexure sheets — can't auto-resolve bare references unambiguously.
        # Leave as-is; log so the operator knows.
        _any_rows = [r for r in all_rows if _normalize_annexure_ref(str(r.get("tag_no") or "")) == "ANNEXURE_ANY"]
        if _any_rows:
            log.warning(
                "Bare annexure reference found (%d rows) but %d annexure sheets exist — "
                "cannot auto-resolve; tags will be left as-is",
                len(_any_rows), len(_real_keys),
            )

    # Step 1c: Pre-resolve "REFER TO ANNEX N" / subgroup references.
    #
    # When a tag normalises to e.g. "ANNEXURE1" but the registry only has subgroup
    # keys like "ANNEXURE1-1" (grouped annexure sheets), we must find the right
    # subgroup before the fan-out loop in Step 4.
    #
    # Two-pass approach: header rows carry eqpt_qty; item rows do not.
    # Pass 1 resolves header rows using eqpt_qty + prefix learning, building a
    # (sheet, original_tag) → resolved_key map.  Pass 2 applies that map to item
    # rows so they all point to the same subgroup key as their header row.
    #
    # Prefix learning: once we've resolved a sheet's first reference unambiguously,
    # we record which parent annexure it belongs to (e.g. MAIN SHEET-4 → "ANNEXURE2")
    # and restrict all future candidates for that sheet to that prefix — preventing
    # false eqpt_qty matches across different annexure sheets.
    _header_resolved: dict[tuple[str, str], str] = {}  # (sheet, tag_no) → resolved_key
    _sheet_to_prefix: dict[str, str] = {}              # sheet name → "ANNEXURE1" / "ANNEXURE2" / …

    # Pass 1: header rows (item_num is None) — eqpt_qty is available here
    for _row in all_rows:
        if _row.get("item_num") is not None:
            continue
        _tag = str(_row.get("tag_no") or "").strip()
        if not _tag:
            continue
        _key = _normalize_annexure_ref(_tag)
        if _key and _key != "ANNEXURE_ANY" and _key not in annexure_registry:
            _sheet_name = _row.get("sheet") or ""
            _prefer = _sheet_to_prefix.get(_sheet_name)
            _resolved = _resolve_subgroup_key(
                _key, annexure_registry, _row.get("eqpt_qty"), prefer_prefix=_prefer
            )
            if _resolved:
                _header_resolved[(_sheet_name, _tag)] = _resolved
                _row["tag_no"] = _resolved
                # Learn which annexure prefix this sheet uses for future rows
                if _sheet_name and _sheet_name not in _sheet_to_prefix:
                    _pfx_match = re.match(r"([A-Z]+\d+)-", _resolved)
                    if _pfx_match:
                        _sheet_to_prefix[_sheet_name] = _pfx_match.group(1)

    # Pass 1.5: Re-attempt rows that couldn't be resolved in Pass 1 because the sheet's
    # prefix hadn't been learned yet (e.g. "REFER TO ANNEX 5" appeared before
    # "REFER TO ANNEX 1" in the column order, so prefix was unknown).
    # By now, Pass 1 has learned the prefix for most sheets — try again.
    for _row in all_rows:
        if _row.get("item_num") is not None:
            continue
        _tag = str(_row.get("tag_no") or "").strip()
        if not _tag:
            continue
        # Skip rows already resolved in Pass 1 (their tag_no is now a direct registry key).
        # Without this guard, _normalize_annexure_ref("ANNEXURE1-2") returns "ANNEXURE1"
        # (not in registry), causing Pass 1.5 to overwrite the correct value with ANNEXURE1-1.
        if _tag in annexure_registry:
            continue
        _key = _normalize_annexure_ref(_tag)
        if not (_key and _key != "ANNEXURE_ANY" and _key not in annexure_registry):
            continue
        _sheet_name = _row.get("sheet") or ""
        _prefer = _sheet_to_prefix.get(_sheet_name)
        if not _prefer:
            continue  # still no prefix learned for this sheet — skip
        _resolved = _resolve_subgroup_key(
            _key, annexure_registry, _row.get("eqpt_qty"), prefer_prefix=_prefer
        )
        if _resolved:
            _header_resolved[(_sheet_name, _tag)] = _resolved
            _row["tag_no"] = _resolved

    # Pass 2: item rows (item_num is not None) — apply the map built from header rows
    for _row in all_rows:
        if _row.get("item_num") is None:
            continue
        _tag = str(_row.get("tag_no") or "").strip()
        if not _tag:
            continue
        _resolved = _header_resolved.get((_row.get("sheet"), _tag))
        if _resolved:
            _row["tag_no"] = _resolved

    # Step 2: Build tag→equipment lookup from annexure + continuation rows
    tag_equip = _build_tag_equipment_lookup(all_rows, profiles, annexure_registry)

    # Step 3: Determine which annexure sheets are being resolved via fan-out
    resolved_annexure_keys: set[str] = set()
    for row in all_rows:
        tag = row.get("tag_no") or ""
        annex_key = _get_annexure_key(tag, annexure_registry)
        if annex_key:
            resolved_annexure_keys.add(annex_key)

    resolved_annexure_sheets: set[str] = set()
    for profile in profiles:
        if profile.role == SheetRole.ANNEXURE:
            key = _normalize_annexure_ref(profile.name)
            if not key:
                key = profile.name.strip().upper()
            if key in resolved_annexure_keys:
                resolved_annexure_sheets.add(profile.name.upper())

    # Step 4: Group annexure-reference rows by key, then fan-out with
    #         correct interleaving: group header → group spare → per-tag pairs
    #
    # Collect annexure groups: {annex_key: {"headers": [...], "details": [...]}}
    annex_groups: dict[str, dict[str, list]] = {}
    non_annex_rows: list[tuple[int, dict[str, Any]]] = []  # (original_idx, row)

    for idx, row in enumerate(all_rows):
        tag = row.get("tag_no") or ""
        sheet = (row.get("sheet") or "").upper()

        # Skip pure-metadata rows from resolved annexure sheets
        if sheet in resolved_annexure_sheets and not row.get("item_num"):
            continue

        annex_key = _get_annexure_key(tag, annexure_registry)
        if annex_key:
            if annex_key not in annex_groups:
                annex_groups[annex_key] = {"headers": [], "details": [], "order": idx}
            if row.get("item_num"):
                annex_groups[annex_key]["details"].append(row)
            else:
                # PHASE 5 FIX: Deduplicate header rows by tag_no to avoid
                # duplicates when multiple sheets reference the same annexure.
                # E.g., MAIN SHEET and CONTINUATION SHEET-1 both have "Annexure I".
                existing_tags = {h.get("tag_no") for h in annex_groups[annex_key]["headers"]}
                if tag not in existing_tags:
                    annex_groups[annex_key]["headers"].append(row)
        else:
            # Drop rows whose tag is an annexure reference that was never resolved.
            # This happens when a main sheet references "Annexure-9" as a column header
            # but that sheet is classified as DATA (not ANNEXURE) and thus has no
            # registry entry — its real tags are extracted independently.
            _unresolved_key = _normalize_annexure_ref(tag)
            if _unresolved_key and _unresolved_key != "ANNEXURE_ANY" and _unresolved_key not in annexure_registry:
                continue
            non_annex_rows.append((idx, row))

    # Step 5: Build enriched output with correct row ordering
    enriched: list[dict[str, Any]] = []

    # Process non-annexure rows and insert annexure groups at their original position
    annex_insert_points: dict[int, str] = {}
    for key, grp in annex_groups.items():
        annex_insert_points[grp["order"]] = key

    next_non_annex = 0
    processed_annex: set[str] = set()

    # Merge: walk through original indices and emit in order
    all_indices = sorted(
        [(idx, "row", row) for idx, row in non_annex_rows]
        + [(grp["order"], "annex", key) for key, grp in annex_groups.items()],
        key=lambda x: x[0],
    )

    for _, kind, payload in all_indices:
        if kind == "row":
            row = payload
            # Direct enrichment for non-annexure rows
            tag = row.get("tag_no") or ""
            tag_key = str(tag).strip().upper() if tag else ""
            if tag_key and tag_key in tag_equip:
                for field in _EQUIPMENT_FIELDS:
                    if not row.get(field) and tag_equip[tag_key].get(field):
                        row[field] = tag_equip[tag_key][field]
            enriched.append(row)
        else:
            annex_key = payload
            if annex_key in processed_annex:
                continue
            processed_annex.add(annex_key)
            annex_tags = annexure_registry[annex_key]
            grp = annex_groups[annex_key]

            # Deduplicate detail rows by item_num.
            # When multiple main sheets reference the same annexure, the same spare items
            # appear in each sheet's continuation and are collected into grp["details"]
            # multiple times. Keep only the first occurrence per item_num.
            seen_item_nums: set = set()
            deduped_details: list[dict[str, Any]] = []
            for _dtl in grp["details"]:
                _inum = _dtl.get("item_num")
                if _inum is not None:
                    if _inum not in seen_item_nums:
                        seen_item_nums.add(_inum)
                        deduped_details.append(_dtl)
                else:
                    deduped_details.append(_dtl)

            # Per-tag pairs: for each real tag, emit header + spares
            # Only enrich model/serial from annexure; manufacturer stays
            # from global metadata (top-right of SPIR = EQPT MAKE)
            _TAG_ENRICH = ("model", "serial")
            N = len(annex_tags)
            for tdata in annex_tags:
                # Skip tags that have no associated spare items to avoid header-only rows
                if not deduped_details:
                    continue
                # Tag header
                for hdr in grp["headers"]:
                    tag_hdr = dict(hdr)
                    tag_hdr["tag_no"] = tdata["tag"]
                    for field in _TAG_ENRICH:
                        if tdata.get(field):
                            tag_hdr[field] = tdata[field]
                    enriched.append(tag_hdr)
                # Tag spare rows — divide total qty by N annexure tags
                for dtl in deduped_details:
                    tag_dtl = dict(dtl)
                    tag_dtl["tag_no"] = tdata["tag"]
                    for field in _TAG_ENRICH:
                        if tdata.get(field):
                            tag_dtl[field] = tdata[field]
                    if N > 1:
                        raw_qty = tag_dtl.get("quantity")
                        try:
                            q = float(raw_qty) if raw_qty is not None else None
                            if q and q > 0:
                                per_tag = q / N
                                tag_dtl["quantity"] = int(per_tag) if per_tag == int(per_tag) else per_tag
                        except (TypeError, ValueError):
                            pass
                    enriched.append(tag_dtl)

    log.info(
        "Equipment enrichment: %d → %d rows, %d annexure groups, %d unique tags",
        len(all_rows), len(enriched),
        len(annexure_registry), len(tag_equip),
    )
    return enriched


def _try_read_annexure_list_sheet(ws) -> dict[str, list[dict[str, Any]]]:
    """
    Parse an "Annexure List" sheet that groups equipment by section headers.

    Format: the sheet has rows whose first cell matches an annexure reference
    (e.g. "Annexure 1", "REFER TO ANNEX 2") acting as section dividers, followed
    by data rows with comma/newline-separated tag numbers and a model number column.
    Example (VEN-4460-DGTYP-5-43-0851-6):
        Row 1: "Annexure 1"        ← section header
        Row 2: "TAG NO" | "MODEL"  ← column headers (optional)
        Row 3: "T-101, T-102"  | "Model-A"
        Row 4: "Annexure 2"        ← next section header
        ...

    Returns {normalized_key: [{tag, model, serial}, ...]} or {} if the sheet
    does not match this format.
    """
    result: dict[str, list[dict[str, Any]]] = {}

    max_row = ws.max_row or 0
    max_col = min(ws.max_column or 5, 20)
    if max_row < 2:
        return result

    # Scan the first ~5 columns of all rows to find section headers and data rows.
    # A section header is a row whose first non-blank cell is an annexure reference.
    # After a section header, rows with non-blank first cell are data rows.

    # First, detect column positions by looking for a header row with keyword cells.
    tag_col: int | None = None
    model_col: int | None = None

    tag_kws = ["tag no", "tag number", "tag", "equipment tag"]
    model_kws = ["model number", "model no", "model"]

    for r in range(1, min(10, max_row + 1)):
        for c in range(1, max_col + 1):
            v = ws.cell(r, c).value
            if v is None:
                continue
            cl = str(v).lower().strip()
            if tag_col is None and any(kw in cl for kw in tag_kws):
                tag_col = c
            if model_col is None and any(kw in cl for kw in model_kws):
                model_col = c

    # Verify this looks like an annexure-list sheet: at least one section header row.
    section_header_found = False
    for r in range(1, min(15, max_row + 1)):
        first_val = None
        for c in range(1, min(5, max_col + 1)):
            v = ws.cell(r, c).value
            if v is not None:
                first_val = str(v).strip()
                break
        if first_val and _normalize_annexure_ref(first_val) not in (None, "ANNEXURE_ANY"):
            section_header_found = True
            break

    if not section_header_found:
        return result

    # Use col 1 for tags and col 2 for model if no header row was found
    if tag_col is None:
        tag_col = 1
    if model_col is None:
        model_col = 2 if max_col >= 2 else None

    current_key: str | None = None

    for r in range(1, max_row + 1):
        # Look for a section header in the first few columns
        first_val = None
        first_col = None
        for c in range(1, min(5, max_col + 1)):
            v = ws.cell(r, c).value
            if v is not None:
                first_val = str(v).strip()
                first_col = c
                break

        if not first_val:
            continue

        # Check if this row is a section header
        ref_key = _normalize_annexure_ref(first_val)
        if ref_key and ref_key != "ANNEXURE_ANY":
            current_key = ref_key
            result.setdefault(current_key, [])
            continue

        # Skip column-header rows using EXACT matching so we don't accidentally skip
        # data rows like "TAG NO. N/A" (which contains "tag no" as a substring).
        _hdr_exact: frozenset[str] = frozenset({
            "model / type", "model no", "model no.", "model number",
            "tag no", "tag no.", "tag number", "tag number(s)",
            "sl no", "sl no.", "s.no", "s no",
        })
        cl = first_val.lower().strip()
        if cl in _hdr_exact:
            continue
        if tag_col:
            _tag_cell_str = str(ws.cell(r, tag_col).value or "").strip().lower()
            if _tag_cell_str in _hdr_exact:
                continue

        # Data row — parse tag(s) and model
        if current_key is None:
            continue

        tag_val = clean_str(ws.cell(r, tag_col).value) if tag_col else None
        model_val = clean_str(ws.cell(r, model_col).value) if model_col else None

        if not tag_val:
            continue

        # Skip rows whose tag column says "N/A" or similar (no real tag assigned yet)
        if re.search(r"(?i)\bn/?a\b", tag_val) and len(tag_val.replace(" ", "")) <= 10:
            continue

        # Normalise "&" and newline separators to commas before splitting so that
        # patterns like "TAG-A & TAG-B" or multi-line cells are split correctly.
        tag_val_split = re.sub(r"\s*&\s*|\n", ", ", tag_val)

        # Tags may be comma-, slash-, or newline-separated within a single cell
        for tag in split_tags(tag_val_split):
            if tag:
                entry: dict[str, Any] = {"tag": tag}
                if model_val:
                    entry["model"] = model_val
                result[current_key].append(entry)

    # Drop empty sections
    result = {k: v for k, v in result.items() if v}

    if result:
        log.info(
            "Annexure list sheet: %d sections, %d total entries",
            len(result), sum(len(v) for v in result.values()),
        )

    return result


def _build_annexure_registry(
    wb, profiles: list[SheetProfile]
) -> dict[str, list[dict[str, Any]]]:
    """
    Read equipment data directly from annexure-classified sheets.

    Returns {normalized_key: [{tag, model, serial, manufacturer}, ...]}.

    Handles three patterns:
      A) Simple: sheet "Annexure 1" → key "ANNEXURE1", all rows are one group
      B) Grouped: sheet "ANNEXURE-P1" has a group number column (e.g.
         "ANNEXURE-P1 NUMBER" with values 1,2,3,4) → creates keys
         "ANNEXUREP1-1", "ANNEXUREP1-2", etc.
      C) Annexure List: one sheet with section headers ("Annexure 1 / 2 / …") and
         comma-separated tag rows — parsed by _try_read_annexure_list_sheet().
    """
    registry: dict[str, list[dict[str, Any]]] = {}

    for profile in profiles:
        annex_key = _normalize_annexure_ref(profile.name)

        # Broaden key derivation for abbreviated sheet names like "Anx-1(New-Skd)"
        # that don't match _ANNEXURE_REF_RE (which requires the full "annex" spelling).
        # Only runs when: standard regex returned None AND the sheet already has ANNEXURE role
        # (meaning Fix 1 in sheet_analyzer promoted it via the broader name pattern).
        if annex_key is None and profile.role == SheetRole.ANNEXURE:
            m = re.search(r"(?i)\b(?:annex(?:ure)?|ann?x)[\s\-_]*(\d+)", profile.name)
            if m:
                annex_key = f"ANNEXURE{m.group(1)}"

        is_annexure_like_sheet = (
            profile.role == SheetRole.ANNEXURE or annex_key is not None
        )
        if not is_annexure_like_sheet:
            continue

        if not annex_key:
            annex_key = profile.name.strip().upper()

        ws = wb[profile.name]

        # Pattern C: unnumbered "list" sheets (annex_key = ANNEXURE_ANY) may contain
        # multiple sections separated by section-header rows.  Try the list parser first
        # so we get correctly keyed subgroups rather than one big ANNEXURE_ANY blob.
        if annex_key == "ANNEXURE_ANY":
            list_result = _try_read_annexure_list_sheet(ws)
            if list_result:
                for lk, lv in list_result.items():
                    registry[lk] = lv
                    log.info(
                        "Annexure '%s' list-format section '%s': %d entries",
                        profile.name, lk, len(lv),
                    )
                continue
            # Fall through to standard parser if list format not detected

        # Check for a group number column (e.g. "ANNEXURE-P1 NUMBER")
        group_col = _find_annexure_group_col(ws, profile)

        entries = _read_annexure_equipment(ws, profile, group_col=group_col)

        # Fallback for COLUMN_HEADERS annexure sheets (tags are column headers, not row data).
        # _read_annexure_equipment expects ROW_HEADERS style; use columnar tag-header reader instead.
        if not entries and profile.tag_layout == TagLayout.COLUMN_HEADERS:
            tag_info = _columnar._read_tag_headers(ws, profile)
            meta = _columnar._read_tag_metadata(ws, profile, tag_info)
            for _col_idx, col_tags in tag_info.items():
                for tag in col_tags:
                    if tag and not re.search(r"(?i)annex", tag):
                        entry: dict[str, Any] = {"tag": tag}
                        entry.update(meta.get(tag, {}))
                        entries.append(entry)

        if not entries:
            continue

        if group_col:
            # Split entries into subgroups by their _group field
            groups: dict[str, list[dict[str, Any]]] = {}
            for entry in entries:
                gnum = entry.pop("_group", None)
                if gnum is not None:
                    sub_key = f"{annex_key}-{gnum}"
                else:
                    sub_key = annex_key
                groups.setdefault(sub_key, []).append(entry)

            for sub_key, sub_entries in groups.items():
                registry[sub_key] = sub_entries
                log.info(
                    "Annexure '%s' subgroup '%s': %d entries",
                    profile.name, sub_key, len(sub_entries),
                )
        else:
            # Simple: all entries under one key
            registry[annex_key] = entries
            log.info(
                "Annexure '%s' (key=%s): %d entries",
                profile.name, annex_key, len(entries),
            )

    return registry


def _find_annexure_group_col(ws, profile: SheetProfile) -> int | None:
    """
    Find a column that contains annexure group numbers.
    Looks for headers like "ANNEXURE-P1 NUMBER", "ANNEXURE NUMBER", etc.
    """
    header_row = profile.header_row or 2
    max_col = min(ws.max_column or 10, 20)

    for c in range(1, max_col + 1):
        v = ws.cell(header_row, c).value
        if v is None:
            continue
        s = str(v).lower().strip()
        # Match plain "ANNEXURE" / "ANNEX" header (group number column in some files)
        # or qualified "ANNEXURE NUMBER", "ANNEXURE NO", etc.
        if s in ("annexure", "annex") or (
            "annexure" in s and any(x in s for x in ("number", "num", "#", "no"))
        ):
            return c

    return None


def _read_annexure_equipment(
    ws, profile: SheetProfile, group_col: int | None = None,
) -> list[dict[str, Any]]:
    """
    Read tag/model/serial/manufacturer from an annexure sheet.
    Uses the profile's column_map when available, otherwise scans headers.
    If group_col is provided, each entry gets a '_group' field for subgrouping.
    """
    entries: list[dict[str, Any]] = []

    col_map = profile.column_map
    scanner_header_row = None
    if not col_map:
        col_map, scanner_header_row = _scan_annexure_headers(ws, sheet_name=profile.name)

    tag_col = col_map.get("tag")
    model_col = col_map.get("model") or col_map.get("manufacturer_model")
    serial_col = col_map.get("serial")
    mfr_col = col_map.get("manufacturer")

    # If manufacturer and model point to the same column (e.g. "Manufacturer Model No"),
    # treat it as model-only — the real manufacturer comes from sheet metadata.
    if mfr_col and model_col and mfr_col == model_col:
        mfr_col = None

    # For sheets where the profile has no header_row (e.g. simplified annexure reference
    # tables like "Anx-9", "Anx-10"), use the row from the scanner so we don't skip
    # the first data row or include a label row in the data scan.
    effective_header_row = profile.header_row or scanner_header_row
    start_row = profile.data_start_row or (
        (effective_header_row + 1) if effective_header_row else 2
    )
    end_row = profile.data_end_row or (ws.max_row or 0)

    if not tag_col:
        # Fallback: scan data rows to find the column with the most tag-like values.
        # Handles annexure sheets whose tag column has an unrecognized header (or no header).
        from spir_dynamic.utils.cell_utils import looks_like_tag as _llt
        best_col, best_count = None, 0
        scan_end = min(start_row + 20, end_row + 1)
        max_col = min((ws.max_column or 5) + 1, 20)
        for c in range(1, max_col):
            count = sum(
                1 for r in range(start_row, scan_end)
                if _llt(str(ws.cell(r, c).value or ""))
            )
            if count > best_count:
                best_count, best_col = count, c
        if best_col and best_count >= 1:
            tag_col = best_col
            log.info(
                "Annexure '%s': tag column not found by header; using col %d "
                "(%d tag-like values via data scan)",
                getattr(profile, "name", "?"), tag_col, best_count,
            )
        else:
            return entries  # truly can't find tags

    # group_val is initialised here (outside the loop) so blank group cells
    # automatically carry the previous row's group number forward.
    group_val = None

    for r in range(start_row, end_row + 1):
        tag_val = clean_str(ws.cell(r, tag_col).value)

        # Keep rows even when tag is blank — the tag may be missing/pending
        # but other data (serial, model, etc.) should still be extracted
        if not tag_val:
            # Check if the row has any other data worth keeping
            has_other = any(
                ws.cell(r, c).value is not None
                for c in [serial_col, model_col, mfr_col]
                if c is not None
            )
            if not has_other:
                continue
            tags = [None]  # blank tag — will output as empty TAG NO
        else:
            tags = split_tags(tag_val)
        model_val = clean_str(ws.cell(r, model_col).value) if model_col else None
        serial_val = clean_str(ws.cell(r, serial_col).value) if serial_col else None
        mfr_val = clean_str(ws.cell(r, mfr_col).value) if mfr_col else None

        # PHASE 5 FIX: Carry forward missing model/manufacturer from previous row.
        # Some SPIR annexure sheets only have model/mfr on the first row,
        # with subsequent rows having only tag + serial.
        if not model_val and entries:
            model_val = entries[-1].get("model")
        if not mfr_val and entries:
            mfr_val = entries[-1].get("manufacturer")

        # Handle serial ranges for multi-tag cells
        serials = _split_serial_range(serial_val, len(tags)) if serial_val else [None] * len(tags)

        # Read group number if group_col is provided.
        # group_val is declared before the loop and only updated when the cell has a value,
        # so blank cells (continuation rows of the same group) carry the previous value forward.
        if group_col:
            gv = ws.cell(r, group_col).value
            if gv is not None:
                try:
                    group_val = str(int(float(gv)))
                except (ValueError, TypeError):
                    group_val = str(gv).strip()

        for i, tag in enumerate(tags):
            entry: dict[str, Any] = {"tag": tag}
            if model_val:
                entry["model"] = model_val
            if i < len(serials) and serials[i]:
                entry["serial"] = serials[i]
            elif serial_val:
                entry["serial"] = serial_val
            if mfr_val:
                entry["manufacturer"] = mfr_val
            if group_val is not None:
                entry["_group"] = group_val
            entries.append(entry)

    return entries


def _scan_annexure_headers(ws, sheet_name: str = None) -> tuple[dict[str, int], int]:
    """
    Scan first rows of an annexure sheet to find tag/model/serial columns.
    Returns (col_map, header_row) where header_row is the last row that contained
    a recognized field header (used as data_start = header_row + 1).

    Uses longest-keyword-wins so that "pump motor tag" (len 14) beats "pump tag"
    (len 8) when both appear in different columns of the same sheet.

    If sheet_name is provided and a shorter-keyword tag column better matches the
    sheet's theme words (derived from the name), that column is preferred instead.
    This handles sheets like "Annx-11 (Isolater)" where the tag column of interest
    is "Isolater Tag No" rather than the longer-matching "Pump Motor Tag No".
    """
    keywords = {
        "tag": ["tag no", "tag number", "tag number(s)", "valve tag", "equipment tag",
                "pump motor tag", "motor tag", "pump tag", "equip", "tag"],
        "model": ["model number", "model no", "model", "mfr type", "manufacturer model"],
        "serial": ["serial number", "serial no", "serial", "ser no", "sr no"],
        "manufacturer": ["manufacturer", "make", "mfr name"],
    }

    max_col = min(ws.max_column or 10, 20)
    scan_rows = min(6, (ws.max_row or 0) + 1)

    # field → (col_index, matched_keyword_length, row)
    best_match: dict[str, tuple[int, int, int]] = {}

    for r in range(1, scan_rows):
        for c in range(1, max_col + 1):
            v = ws.cell(r, c).value
            if v is None:
                continue
            cell_lower = str(v).lower().strip()
            for field, kws in keywords.items():
                for kw in kws:
                    if kw in cell_lower:
                        kw_len = len(kw)
                        current_len = best_match.get(field, (None, -1, -1))[1]
                        if kw_len > current_len:
                            best_match[field] = (c, kw_len, r)
                        break  # one keyword per cell per field

    # Theme-based tag column override: if the sheet name contains words not present
    # in the best-matched tag column header, look for an alternative "tag" column
    # whose header shares more words with the sheet name.  This disambiguates sheets
    # that have multiple tag-like columns (e.g. "Pump Motor Tag No" vs "Isolater Tag No").
    if sheet_name and "tag" in best_match:
        current_tag_col = best_match["tag"][0]
        theme_col = _find_theme_tag_col(
            ws, sheet_name, current_tag_col, max_col, scan_rows
        )
        if theme_col is not None:
            best_match["tag"] = (theme_col, best_match["tag"][1], best_match["tag"][2])

    col_map = {field: col for field, (col, _, _) in best_match.items()}
    header_row_found = max((row for _, _, row in best_match.values()), default=1) if best_match else 1
    return col_map, header_row_found


def _find_theme_tag_col(ws, sheet_name: str, current_col: int, max_col: int, scan_rows: int):
    """
    Return an alternative tag column whose header better matches the sheet name's
    theme words, or None if the current column is already the best fit.

    Theme words are extracted by stripping the leading annexure identifier
    (e.g. "Annx-11") and then collecting distinct alphabetic words of 3+ chars.
    """
    _STOP_WORDS = frozenset({"the", "and", "for", "with", "new", "old", "tab", "page", "sheet"})
    # Strip leading "Annx-N / Anx-N / Annexure-N" prefix then extract words
    cleaned = re.sub(r"(?i)^ann?(?:ex(?:ure)?)?[\s\-_]*\d+[\s\-_]*", "", sheet_name)
    theme_words = {
        w for w in re.findall(r"[a-z]{3,}", cleaned.lower())
        if w not in _STOP_WORDS
    }
    if not theme_words:
        return None

    # Score every column that contains "tag" in its header by theme-word overlap
    best_col = None
    best_score = 0
    for r in range(1, scan_rows):
        for c in range(1, max_col + 1):
            v = ws.cell(r, c).value
            if v is None:
                continue
            cell_lower = str(v).lower()
            if "tag" not in cell_lower:
                continue
            score = sum(1 for w in theme_words if w in cell_lower)
            if score > best_score or (score == best_score and c == current_col):
                best_score = score
                best_col = c

    # Only override when a *different* column wins with a positive score
    if best_col is not None and best_col != current_col and best_score > 0:
        return best_col
    return None


def _split_serial_range(serial_str: str, expected_count: int) -> list[str]:
    """
    Split a serial number range into individual values.
    "100 to 101" → ["100", "101"]
    "100/101/102" → ["100", "101", "102"]
    "SNY20061532" → ["SNY20061532"]
    """
    if not serial_str:
        return []

    s = str(serial_str).strip()

    # "X to Y" pattern
    parts = re.split(r"\s+to\s+", s, flags=re.IGNORECASE)
    if len(parts) == 2:
        return [p.strip() for p in parts]

    # "/" separator
    if "/" in s:
        parts = [p.strip() for p in s.split("/") if p.strip()]
        if len(parts) >= 2:
            return parts

    # "," separator
    if "," in s:
        parts = [p.strip() for p in s.split(",") if p.strip()]
        if len(parts) >= 2:
            return parts

    return [s]


def _build_tag_equipment_lookup(
    all_rows: list[dict[str, Any]],
    profiles: list[SheetProfile],
    annexure_registry: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """
    Build tag→{model, serial, manufacturer} lookup from:
      1. Annexure registry (highest priority)
      2. Continuation sheet rows
      3. Any row that has equipment data
    """
    tag_equip: dict[str, dict[str, Any]] = {}

    annexure_sheets = {p.name.upper() for p in profiles if p.role == SheetRole.ANNEXURE}
    continuation_sheets = {p.name.upper() for p in profiles if p.role == SheetRole.CONTINUATION}

    # Pass 1: data/continuation rows (lower priority)
    for row in all_rows:
        sheet = (row.get("sheet") or "").upper()
        tag = row.get("tag_no") or ""
        tag_key = str(tag).strip().upper()
        if not tag_key or _ANNEXURE_REF_RE.match(tag_key):
            continue

        if tag_key not in tag_equip:
            tag_equip[tag_key] = {}
        for field in _EQUIPMENT_FIELDS:
            val = row.get(field)
            if val and field not in tag_equip[tag_key]:
                tag_equip[tag_key][field] = val

    # Pass 2: annexure registry (overrides)
    for entries in annexure_registry.values():
        for entry in entries:
            tag_key = str(entry.get("tag", "")).strip().upper()
            if not tag_key:
                continue
            if tag_key not in tag_equip:
                tag_equip[tag_key] = {}
            for field in _EQUIPMENT_FIELDS:
                val = entry.get(field)
                if val:
                    tag_equip[tag_key][field] = val  # Override

    return tag_equip


def _normalize_annexure_ref(value: str) -> str | None:
    """
    Extract normalized annexure key from a value.
    "Annexure 1"          → "ANNEXURE1"
    "ANNEXURE-2"          → "ANNEXURE2"
    "Refer Annexure 3"    → "ANNEXURE3"
    "REFER TO ANNEX 4"    → "ANNEXURE4"
    "ANNEXURE (P1)-1"     → "ANNEXUREP1-1"
    "ANNEXURE (P2)-3"     → "ANNEXUREP2-3"
    "Annexure I"          → "ANNEXURE1"
    "Annexure Ⅵ"          → "ANNEXURE6"   (Unicode Roman → ASCII before regex)
    Returns None if value is not an annexure reference.
    """
    if not value:
        return None
    # Replace Unicode Roman numeral glyphs with their ASCII equivalents so
    # the regex [IVX]+ can match them (e.g. "Ⅵ" → "VI", "Ⅻ" → "XII").
    text = str(value).strip()
    for uni_char, ascii_str in _UNICODE_ROMAN_MAP.items():
        text = text.replace(uni_char, ascii_str)
    m = _ANNEXURE_REF_RE.search(text)
    if m:
        group_id = m.group(1)  # e.g. "P1" from "(P1)", or None
        number = m.group(2)    # e.g. "1" or "I"
        # Convert Roman numeral to integer if needed
        roman_val = _roman_to_int(number)
        if roman_val is not None:
            number = str(roman_val)
        if group_id:
            return f"ANNEXURE{group_id.upper()}-{number}"
        return f"ANNEXURE{number}"
    # Also match exact sheet names like "Annexure 1" without the regex
    cleaned = re.sub(r"[\s\-_]+", "", text.upper())
    if cleaned.startswith("ANNEXURE") and any(c.isdigit() for c in cleaned):
        return cleaned
    # Bare "Refer Annexure" / "Annexure" without a number — sentinel for single-sheet resolution
    if re.search(r"(?i)annex", text):
        return "ANNEXURE_ANY"
    return None


def _is_annexure_sheet(sheet_name: str, profiles: list[SheetProfile]) -> bool:
    """Check if a sheet name belongs to an annexure-classified sheet."""
    for p in profiles:
        if p.name.upper() == sheet_name and p.role == SheetRole.ANNEXURE:
            return True
    return False
