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
from spir_dynamic.utils.cell_utils import clean_str, clean_num, split_tags, is_placeholder
from spir_dynamic.utils.logging import timed

log = logging.getLogger(__name__)

# Pattern to detect annexure-reference tag values like "Annexure 1", "ANNEXURE-2",
# "Refer Annexure 3", "ANNEXURE (P1)-1", "ANNEXURE (P2)-3", "REFER TO ANNEX 4",
# "ANNEXURES-1" (plural form used in some SPIR files), etc.
# Matches "annexures?" (with or without plural S), bare "annex", and handles
# "refer to" as well as plain "refer".
_ANNEXURE_REF_RE = re.compile(
    r"(?:refer(?:\s+to)?\s+)?ann(?:ex|e)?(?:ures?)??[\s\-_]*(?:\(([^)]*)\)[\s\-_]*)?(\d+|[IVX]+)\b",
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
    profiles = analyze_workbook(wb, filename=filename)

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
            # Normal sheets are their own logical main group
            for r in sheet_rows:
                r["_group_main"] = profile.name
            all_rows.extend(sheet_rows)
            format_parts.append(f"{profile.name}({profile.tag_layout.value})")

        except Exception as exc:
            log.error("Extraction failed for '%s': %s", profile.name, exc, exc_info=True)

    # Step 4: Equipment enrichment — resolve annexure references + merge equipment data
    all_rows = _enrich_equipment_data(wb, all_rows, profiles)

    # Step 4b: Vendor contact extraction from MANUFACTURERS/SUPPLIERS FOCAL POINT cell
    _attach_vendor_info(wb, all_rows, profiles)

    # Restore physical sheet labels for continuation-sheet rows whose sheet was
    # temporarily overridden to the parent main sheet name for EXTEND/SPLIT
    # detection in _enrich_equipment_data.
    for row in all_rows:
        if "_orig_sheet" in row:
            row["sheet"] = row.pop("_orig_sheet")

    # PHASE 5 FIX: Clean up internal tracking fields
    for row in all_rows:
        row.pop("_group_main", None)
        row.pop("_annex_col", None)
        row.pop("_orig_sheet", None)  # safety in case not already restored

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
    # Exclude sheets that are clearly continuations by name to ensure they stay grouped.
    def _is_primary_main(p: SheetProfile) -> bool:
        name_lower = p.name.lower()
        if any(kw in name_lower for kw in ("conti", "continuation")):
            return False
        return (
            "item_number" in p.column_map
            and "description" in p.column_map
            and len(p.column_map) >= 4
        )

    primary_mains = [p for p in columnar_profiles if _is_primary_main(p)]
    non_primaries = [p for p in columnar_profiles if not _is_primary_main(p)]

    # Single main (or no primaries): use the original single-group logic unchanged.
    if len(primary_mains) <= 1:
        main_name = primary_mains[0].name if primary_mains else None
        return _extract_single_group(wb, columnar_profiles, spir_no, main_sheet_name=main_name)


    # Multiple independent main sheets: partition non-primaries by parent main.
    groups = _group_by_main(primary_mains, non_primaries, all_profiles)

    all_rows: list[dict[str, Any]] = []
    for main_profile, group_conts in groups:
        group_sheets = [main_profile] + group_conts
        group_rows = _extract_single_group(wb, group_sheets, spir_no, main_sheet_name=main_profile.name)

        # Save physical source sheet before overriding. The override makes all
        # rows in this group share the same sheet label, which is required by
        # the EXTEND/SPLIT detection in _enrich_equipment_data (SPLIT = multiple
        # columns on the same sheet; EXTEND = columns spanning different sheets).
        # The original label is restored in extract_workbook after enrichment so
        # the output correctly reflects each row's physical source sheet.
        main_name_upper = main_profile.name.upper()
        for r in group_rows:
            if r.get("sheet") != main_name_upper:
                # Save _orig_sheet only for formally-named continuation sheets
                # (those whose sheet name contains "conti" or "continuation").
                # These are distinct SPIR pages where the physical label is
                # meaningful in the output (e.g. "CONTI SHEET-4 (ELECTRICAL)").
                # Overflow continuation sheets (e.g. "2 YEAR-SPARE-1-CONT")
                # do not contain "conti", so they keep the parent main sheet
                # label without restoration.
                _sheet_name_lower = str(r.get("sheet") or "").lower()
                if any(_kw in _sheet_name_lower for _kw in ("conti", "continuation")):
                    r["_orig_sheet"] = r.get("sheet", "")
                r["sheet"] = main_name_upper

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
      1. Parenthetical sequence numbers: "Cont Sheet (2)" → (2) matches "Main Sheet (2)".
         These are explicit sequence markers added by Excel when sheet names are duplicated.
         Plain digits embedded in the sheet name (e.g. "2 YEAR-SPARE-1-CONT") are NOT
         sequence markers and must NOT be used for matching — they are part of the name.
      2. Positional fallback: assign to the last primary main that appears before this
         sheet in the workbook.
    """
    sheet_order = {p.name: i for i, p in enumerate(all_profiles)}

    def _paren_numbers(name: str) -> set[int]:
        """Extract numbers in parentheses — these are Excel sequence markers like (2), (3)."""
        return {int(m) for m in re.findall(r"\((\d+)\)", name)}

    sorted_mains = sorted(primary_mains, key=lambda p: sheet_order.get(p.name, 0))
    main_paren = {p.name: _paren_numbers(p.name) for p in sorted_mains}

    groups: dict[str, list[SheetProfile]] = {p.name: [] for p in sorted_mains}

    for cont in non_primaries:
        cont_paren = _paren_numbers(cont.name)
        best_main = None

        # Step 1: Match by shared parenthetical sequence number.
        if cont_paren:
            for main in sorted_mains:
                if cont_paren & main_paren[main.name]:
                    best_main = main
                    break

        # Step 2: Positional fallback — assign to the last primary main that appears
        # before this sheet in the workbook.
        if best_main is None:
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
    main_sheet_name: str | None = None,
) -> list[dict[str, Any]]:

    """
    Extract one cohesive group of COLUMN_HEADERS sheets sharing a single items_dict.

    1. Find the "item source" sheet (has description/price columns)
    2. Read items from it
    3. Merge items from other sheets in this group that fill gaps (PHASE 2)
    4. Extract all sheets in this group using the shared items_dict
    """
    # Fresh instance per call — state (_last_metadata_field_rows) is local to this task.
    columnar = ColumnarStrategy()

    all_rows: list[dict[str, Any]] = []

    # Find the item source: the sheet with the richest column_map
    # (has description, unit_price, part_number etc.)
    item_source = _find_item_source(columnar_profiles)

    # Read items from the source sheet
    items_dict: dict[int, dict[str, Any]] = {}
    item_source_name = item_source.name if item_source else ""
    if item_source:
        ws = wb[item_source_name]
        items_dict = columnar.read_items(ws, item_source)
        log.info(
            "Item source '%s': %d items read",
            item_source_name, len(items_dict),
        )

    # PHASE 2 FIX: Merge items from other sheets in THIS GROUP that have
    # description data missing from the item source.
    # Handles files where one main sheet splits items across two sub-sheets
    # (e.g., items 1-18 in MAIN SHEET, items 19-24 in MAIN SHEET (3)).
    for profile in columnar_profiles:
        if profile.name == item_source_name:
            continue
        ws = wb[profile.name]
        other_items = columnar.read_items(ws, profile)
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
            is_item_source_sheet = profile.name == item_source_name
            rows, disc_field_rows = columnar.extract(
                ws,
                profile,
                spir_no,
                items_dict=items_dict,
                metadata_field_rows=None if is_item_source_sheet else item_source_metadata_rows,
            )

            # Capture field-row positions from the item source sheet so that
            # continuation sheets can locate metadata rows without re-scanning.
            if is_item_source_sheet:
                item_source_metadata_rows = disc_field_rows

            # Inject logical main indicator for header deduplication
            for r in rows:
                r["_group_main"] = main_sheet_name or profile.name

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
        TagLayout.TAG_COLUMN: TabularStrategy,
        TagLayout.GLOBAL_TAG: TabularStrategy,
        TagLayout.ROW_HEADERS: TransposedStrategy,
    }
    cls = mapping.get(profile.tag_layout)
    if cls is None and profile.column_map:
        return TabularStrategy()
    return cls() if cls else None


def _resolve_spir_no(profiles: list[SheetProfile], filename: str) -> str:
    """Resolve the SPIR number: filename is the authoritative document identifier.

    Filename takes priority because a workbook may contain sheets from multiple
    embedded SPIR documents (different equipment). The filename always identifies
    the actual document; sheet-embedded SPIR numbers may belong to foreign sheets.
    """
    if filename:
        patterns = [
            r"([A-Z0-9]{2,}-[A-Z0-9]{2,}-[A-Z0-9][A-Z0-9\-]*)",
            r"(\d{4,}[\-_]\w+[\-_]\w+)",
        ]
        name = filename.rsplit(".", 1)[0]
        for pat in patterns:
            m = re.search(pat, name, re.IGNORECASE)
            if m:
                return m.group(1)

    for p in profiles:
        spir = p.metadata.get("spir_no")
        if spir:
            spir_clean = str(spir).strip()
            if len(spir_clean) >= 5 and re.search(r'[A-Z0-9]', spir_clean, re.I):
                return spir_clean

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
    *,
    per_col_dedup: bool = False,
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

    # Step 1a: Enrich registry serial numbers from continuation sheet header rows.
    # The continuation sheet has one column per sub-group (annexure ref + serial row)
    # whose order matches the Annexure List's model sub-groups.
    _enrich_registry_serials_from_continuations(annexure_registry, wb, profiles)

    # Step 1b-pre: Build per-column item sets for annexure keys that span multiple
    # continuation columns (e.g. 5 "Annexure 4" columns in the CONT sheet).
    # Used during fan-out to assign each registry entry only its own items.
    subgroup_item_map = _build_subgroup_item_map(wb, profiles)

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
                # PHASE 5 FIX: Deduplicate header rows by tag_no and _group_main to avoid
                # duplicates when multiple sheets reference the same annexure, while
                # allowing separate headers for different logical main sheets.
                # Include _annex_col so that two columns in the SAME main sheet
                # referencing the SAME annexure (e.g. col3 qty=14, col4 qty=2 both
                # pointing to Annexure 12) each get their own header row.
                existing_keys = {(h.get("tag_no"), h.get("_group_main"), h.get("_annex_col")) for h in annex_groups[annex_key]["headers"]}
                if (tag, row.get("_group_main"), row.get("_annex_col")) not in existing_keys:
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

            # Deduplicate detail rows. When per_col_dedup=True (per-group
            # enrichment calls), include _annex_col in the key so each tag
            # column's specific item rows are preserved independently — required
            # when CONT sub-group columns have diverse item sets. When False
            # (top-level call), use the legacy (item_num, _group_main) key to
            # avoid behaviour changes in existing single-group files.
            seen_keys: set = set()
            deduped_details: list[dict[str, Any]] = []
            for _dtl in grp["details"]:
                _inum = _dtl.get("item_num")
                if _inum is not None:
                    if per_col_dedup:
                        _key = (_inum, _dtl.get("_group_main"), _dtl.get("_annex_col"))
                    else:
                        _key = (_inum, _dtl.get("_group_main"))
                    if _key not in seen_keys:
                        seen_keys.add(_key)
                        deduped_details.append(_dtl)
                else:
                    deduped_details.append(_dtl)

            # Per-tag pairs: for each real tag, emit header + spares
            # Only enrich model/serial from annexure; manufacturer stays
            # from global metadata (top-right of SPIR = EQPT MAKE)
            _TAG_ENRICH = ("model", "serial")
            N = len(annex_tags)

            # Positional fan-out: when multiple registry entries share the same tag
            # (e.g. all "N/A") AND the continuation sheet maps each one to its own
            # column with distinct items, assign items positionally rather than
            # applying all merged items to every entry.
            _pos_items: list[list[int]] | None = None
            if N > 1 and len({td["tag"] for td in annex_tags}) == 1:
                _col_item_sets = subgroup_item_map.get(annex_key)
                if _col_item_sets and len(_col_item_sets) == N:
                    _pos_items = _col_item_sets

            # Compute model-based sub-group sizes. Applied only when the tag slice
            # covers the full annexure (not a partial SPLIT-mode slice) so that
            # per-column eqpt_qty values from the main sheet are preserved in SPLIT.
            _entry_subgroup_size: list[int] = []
            _num_subgroups = 0
            if N > 1:
                _i = 0
                while _i < N:
                    _m = annex_tags[_i].get("model")
                    _j = _i + 1
                    while _j < N and annex_tags[_j].get("model") == _m:
                        _j += 1
                    _sub_size = _j - _i
                    _entry_subgroup_size.extend([_sub_size] * _sub_size)
                    _num_subgroups += 1
                    _i = _j
            else:
                _entry_subgroup_size = [N]
                _num_subgroups = 1
            _use_subgroup_eqpt_qty = _num_subgroups > 1

            if len(grp["headers"]) > 1:
                # Multi-reference fan-out: partition headers by _group_main so
                # each logical main-sheet group independently fans out against the
                # FULL annexure list. This prevents cross-group tag stealing when
                # two independent primary mains (e.g. NORMAL + NORMAL(2)) each
                # reference the same annexure — both groups get the complete tag
                # list rather than competing for a single sequential offset.
                # Within each partition, the sequential offset still applies so
                # CONT sub-sheets that share _group_main with their parent main
                # correctly receive only the remaining tags after the main header
                # has consumed its slice.

                # Build ordered list of unique _group_main values (first-seen order).
                _seen_gm_list: list[str | None] = []
                _seen_gm_set: set = set()
                for _h in grp["headers"]:
                    _gm_val = _h.get("_group_main")
                    if _gm_val not in _seen_gm_set:
                        _seen_gm_set.add(_gm_val)
                        _seen_gm_list.append(_gm_val)

                for _gm in _seen_gm_list:
                    _gm_headers = [h for h in grp["headers"] if h.get("_group_main") == _gm]

                    # Per-partition dedup using (item_num, _annex_col) so each
                    # tag column's distinct item rows are preserved independently.
                    _seen_dtl_keys: set = set()
                    _gm_details: list[dict[str, Any]] = []
                    for _dtl in grp["details"]:
                        if _dtl.get("_group_main") != _gm:
                            continue
                        _inum = _dtl.get("item_num")
                        if _inum is not None:
                            _dtl_key = (_inum, _dtl.get("_annex_col"))
                            if _dtl_key not in _seen_dtl_keys:
                                _seen_dtl_keys.add(_dtl_key)
                                _gm_details.append(_dtl)
                        else:
                            _gm_details.append(_dtl)

                    # Diverse-items detection within this partition: if the
                    # non-first headers have different item sets from each other,
                    # process them first so each gets its specific slice.
                    _col_item_sets_gm: dict[int, frozenset] = {}
                    for _dd in _gm_details:
                        _dc = _dd.get("_annex_col")
                        _di = _dd.get("item_num")
                        if _dc is not None and _di is not None:
                            if _dc not in _col_item_sets_gm:
                                _col_item_sets_gm[_dc] = set()
                            _col_item_sets_gm[_dc].add(_di)
                    _col_fsets_gm = {c: frozenset(s) for c, s in _col_item_sets_gm.items()}
                    _rest_cols_gm = [h.get("_annex_col") for h in _gm_headers[1:]]
                    _rest_fsets_gm = [_col_fsets_gm.get(c, frozenset()) for c in _rest_cols_gm if c is not None]
                    _non_empty_gm = [s for s in _rest_fsets_gm if s]
                    _cont_diverse_gm = len(set(_non_empty_gm)) > 1

                    # EXTEND mode: headers span different physical sheet labels,
                    # meaning each continuation sheet independently adds spare
                    # item rows for the SAME complete tag set (e.g. MAIN references
                    # Annexure I items 1-24, CONT-1 references same Annexure I
                    # item 25). Every section sees ALL N tags; only the first
                    # section emits per-tag header rows.
                    # SPLIT mode (all headers share the same sheet label): the
                    # existing sequential offset allocates a distinct tag slice to
                    # each header column (e.g. col3 qty=14, col4 qty=2).
                    _sheets_in_gm_hdrs = {h.get("sheet") for h in _gm_headers}
                    _is_extend_mode = len(_sheets_in_gm_hdrs) > 1

                    if not _is_extend_mode and _cont_diverse_gm:
                        _ordered_hdrs_gm = _gm_headers[1:] + _gm_headers[:1]
                    else:
                        _ordered_hdrs_gm = _gm_headers

                    _offset = 0
                    for _hdr_idx, hdr in enumerate(_ordered_hdrs_gm):
                        _hdr_col = hdr.get("_annex_col")
                        _hdr_details = [d for d in _gm_details if d.get("_annex_col") == _hdr_col]
                        if not _hdr_details:
                            _hdr_details = _gm_details

                        if _is_extend_mode:
                            # All sections get the full tag list; only the first
                            # emits per-tag header rows.
                            tag_slice = annex_tags
                            _abs_slice_start = 0
                            _emit_header = (_hdr_idx == 0)
                        else:
                            # SPLIT mode: sequential slice by eqpt_qty.
                            _qty = int(hdr.get("eqpt_qty") or 0)
                            tag_slice = annex_tags[_offset: _offset + _qty] if _qty > 0 else annex_tags[_offset:]
                            _abs_slice_start = _offset
                            _offset += len(tag_slice)
                            _emit_header = True

                        _N_slice = len(tag_slice)
                        for _si, tdata in enumerate(tag_slice):
                            if not _hdr_details and not _emit_header:
                                continue
                            if _emit_header:
                                tag_hdr = dict(hdr)
                                tag_hdr["tag_no"] = tdata["tag"]
                                for field in _TAG_ENRICH:
                                    if tdata.get(field):
                                        tag_hdr[field] = tdata[field]
                                _abs_idx = _abs_slice_start + _si
                                if (_use_subgroup_eqpt_qty and _N_slice == N
                                        and _abs_idx < len(_entry_subgroup_size)):
                                    tag_hdr["eqpt_qty"] = _entry_subgroup_size[_abs_idx]
                                enriched.append(tag_hdr)
                            for dtl in _hdr_details:
                                tag_dtl = dict(dtl)
                                tag_dtl["tag_no"] = tdata["tag"]
                                for field in _TAG_ENRICH:
                                    if tdata.get(field):
                                        tag_dtl[field] = tdata[field]
                                if _N_slice > 1:
                                    raw_qty = tag_dtl.get("quantity")
                                    try:
                                        q = float(raw_qty) if raw_qty is not None else None
                                        if q and q > 0:
                                            per_tag = q / _N_slice
                                            tag_dtl["quantity"] = int(per_tag) if per_tag == int(per_tag) else per_tag
                                    except (TypeError, ValueError):
                                        pass
                                enriched.append(tag_dtl)
            else:
                for entry_idx, tdata in enumerate(annex_tags):
                    # Determine which detail rows apply to this specific entry
                    if _pos_items is not None:
                        allowed_items = set(_pos_items[entry_idx])
                        entry_details = [d for d in deduped_details
                                         if d.get("item_num") in allowed_items]
                    else:
                        entry_details = deduped_details

                    # Skip tags that have no associated spare items to avoid header-only rows
                    if not entry_details:
                        continue
                    # Tag header
                    for hdr in grp["headers"]:
                        tag_hdr = dict(hdr)
                        tag_hdr["tag_no"] = tdata["tag"]
                        for field in _TAG_ENRICH:
                            if tdata.get(field):
                                tag_hdr[field] = tdata[field]
                        if _use_subgroup_eqpt_qty and entry_idx < len(_entry_subgroup_size):
                            tag_hdr["eqpt_qty"] = _entry_subgroup_size[entry_idx]
                        enriched.append(tag_hdr)
                    # Tag spare rows — divide total qty by N annexure tags
                    for dtl in entry_details:
                        tag_dtl = dict(dtl)
                        tag_dtl["tag_no"] = tdata["tag"]
                        for field in _TAG_ENRICH:
                            if tdata.get(field):
                                tag_dtl[field] = tdata[field]
                        if N > 1 and _pos_items is None:
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

        # Rows whose tag column says "N/A" represent real equipment without an assigned
        # installation tag (e.g. retrieval tools, service valves). Include them with
        # tag="N/A" so their spare items still appear in the BOM output.
        if re.search(r"(?i)\bn/?a\b", tag_val) and len(tag_val.replace(" ", "")) <= 10:
            if model_val:
                result[current_key].append({"tag": "N/A", "model": model_val})
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


def _read_continuation_serial_map(ws) -> dict[str, list[str]]:
    """
    Scan a continuation sheet's header area to build a per-annexure serial list.

    Detects dynamically:
      - The row whose column values are annexure references (e.g. "Annexure 1")
      - The row whose first-column label contains "ser no" or "serial"

    Returns {normalized_annexure_key: [serial_str, ...]} where each entry
    corresponds to one sub-group column (in left-to-right order).
    """
    max_row = ws.max_row or 0
    max_col = ws.max_column or 0
    if max_row < 2 or max_col < 2:
        return {}

    SCAN_ROWS = min(15, max_row)
    LABEL_COLS = 3  # first N columns are row-label columns, not data columns

    # Find the annexure-reference row: first row that has ≥1 annexure ref
    # in its data columns (beyond the label columns).
    annex_ref_row: int | None = None
    data_start_col: int | None = None
    for r in range(1, SCAN_ROWS + 1):
        for c in range(LABEL_COLS + 1, max_col + 1):
            v = str(ws.cell(r, c).value or "").strip()
            if v and _normalize_annexure_ref(v):
                annex_ref_row = r
                data_start_col = c
                break
        if annex_ref_row:
            break

    if annex_ref_row is None or data_start_col is None:
        return {}

    # Find the serial-number row: row whose label column contains "ser no" / "serial"
    serial_row: int | None = None
    _ser_kws = ("ser no", "serial no", "serial number", "mfr ser", "mfr serial")
    for r in range(1, SCAN_ROWS + 1):
        for c in range(1, LABEL_COLS + 1):
            v = str(ws.cell(r, c).value or "").lower().strip()
            if any(kw in v for kw in _ser_kws):
                serial_row = r
                break
        if serial_row:
            break

    if serial_row is None:
        return {}

    # Collect (annexure_key, serial) per data column
    result: dict[str, list[str]] = {}
    for c in range(data_start_col, max_col + 1):
        ref_val = str(ws.cell(annex_ref_row, c).value or "").strip()
        ref_key = _normalize_annexure_ref(ref_val)
        if not ref_key or ref_key == "ANNEXURE_ANY":
            continue
        raw_serial_cell = ws.cell(serial_row, c).value
        # Skip placeholder cells (N/A, nil, -, etc.) — clean_str returns None for these
        # but is_placeholder is a more direct check on the raw cell value.
        if is_placeholder(raw_serial_cell):
            continue
        serial_val = clean_str(raw_serial_cell)
        if not serial_val:
            continue
        # Reject single/double digit values — these are interchangeability flags,
        # not real serial numbers.
        if re.fullmatch(r"\d{1,2}", serial_val):
            continue
        result.setdefault(ref_key, []).append(serial_val)

    return result


def _enrich_registry_serials_from_continuations(
    registry: dict[str, list[dict[str, Any]]],
    wb,
    profiles: list[SheetProfile],
) -> None:
    """
    Read serial numbers from continuation sheets and assign them to each
    annexure registry sub-group.

    Alignment: continuation columns for a given annexure reference appear in
    the same order as the model sub-groups in the registry (as read from the
    Annexure List). The i-th continuation column → i-th model sub-group.
    Tags that share the same model belong to the same sub-group.
    """
    # Identify continuation sheets: COLUMN_HEADERS layout, name contains "cont"
    cont_profiles = [
        p for p in profiles
        if p.is_extractable
        and p.tag_layout == TagLayout.COLUMN_HEADERS
        and any(kw in p.name.lower() for kw in ("cont", "continuation"))
    ]
    if not cont_profiles:
        return

    # Merge serial maps from all continuation sheets (first one wins per sub-group)
    merged_serial_map: dict[str, list[str]] = {}
    for cp in cont_profiles:
        ws = wb[cp.name]
        smap = _read_continuation_serial_map(ws)
        for annex_key, serials in smap.items():
            if annex_key not in merged_serial_map:
                merged_serial_map[annex_key] = serials
        if merged_serial_map:
            break  # one continuation is enough — they duplicate each other

    if not merged_serial_map:
        return

    for annex_key, entries in registry.items():
        serials = merged_serial_map.get(annex_key)
        if not serials or not entries:
            continue

        # Group consecutive same-model entries into sub-groups.
        # The Annexure List inserts all tags of the same model consecutively
        # (one model-row → N tag entries), so this grouping is stable.
        sub_groups: list[tuple[int, int]] = []  # (start_idx, end_idx) per sub-group
        prev_model = entries[0].get("model")
        group_start = 0
        for i, entry in enumerate(entries[1:], 1):
            m = entry.get("model")
            if m != prev_model:
                sub_groups.append((group_start, i))
                prev_model = m
                group_start = i
        sub_groups.append((group_start, len(entries)))

        for sg_idx, (start, end) in enumerate(sub_groups):
            if sg_idx >= len(serials):
                break
            serial = serials[sg_idx]
            # Skip placeholder or single/double-digit serials — these are
            # interchangeability flags or N/A values that slipped through.
            if not serial or re.fullmatch(r"\d{1,2}", str(serial)):
                continue
            for i in range(start, end):
                if not entries[i].get("serial"):
                    entries[i]["serial"] = serial

        log.info(
            "Registry '%s': assigned serials to %d sub-groups (%d entries)",
            annex_key, len(sub_groups), len(entries),
        )


def _read_continuation_subgroup_items(ws) -> dict[str, list[list[int]]]:
    """
    For each annexure key in a continuation sheet, return a list of item-number
    sets — one per column position (left-to-right) that carries that annexure label.

    Used to do positional fan-out when multiple same-label columns (e.g. five
    "Annexure 4" columns) each have their own subset of applicable items.

    Returns {normalized_key: [[item_nums_col0], [item_nums_col1], ...]}
    """
    max_row = ws.max_row or 0
    max_col = ws.max_column or 0
    if max_row < 2 or max_col < 2:
        return {}

    SCAN_ROWS = min(15, max_row)
    LABEL_COLS = 3

    # Find the annexure-reference row
    annex_ref_row: int | None = None
    data_start_col: int | None = None
    for r in range(1, SCAN_ROWS + 1):
        for c in range(LABEL_COLS + 1, max_col + 1):
            v = str(ws.cell(r, c).value or "").strip()
            if v and _normalize_annexure_ref(v):
                annex_ref_row = r
                data_start_col = c
                break
        if annex_ref_row:
            break

    if annex_ref_row is None or data_start_col is None:
        return {}

    # Find the item-number label column (first col 1-LABEL_COLS with numeric-ish values
    # after the header area rows — i.e. the item# column).  We detect the first data
    # row after the header area by looking for a row where col 3 (or col in 1-LABEL_COLS)
    # has a numeric value AND data columns (beyond LABEL_COLS) have values.
    item_num_col: int = LABEL_COLS  # default: 3rd label column (0-based would be col 3)
    header_end_row: int = SCAN_ROWS  # rows after this are item rows

    # Find the first row after SCAN_ROWS boundary where col 3 has an integer
    for r in range(SCAN_ROWS + 1, max_row + 1):
        v = ws.cell(r, item_num_col).value
        if v is not None:
            try:
                int(float(str(v).strip()))
                header_end_row = r - 1
                break
            except (ValueError, TypeError):
                pass

    # Also check within SCAN_ROWS: the header area ends when column-3 starts
    # showing integer item numbers
    for r in range(annex_ref_row + 1, SCAN_ROWS + 1):
        v = ws.cell(r, item_num_col).value
        if v is not None:
            try:
                int(float(str(v).strip()))
                header_end_row = r - 1
                break
            except (ValueError, TypeError):
                pass

    # Build col_index → annexure_key map for data columns
    col_to_key: dict[int, str] = {}
    for c in range(data_start_col, max_col + 1):
        ref_val = str(ws.cell(annex_ref_row, c).value or "").strip()
        ref_key = _normalize_annexure_ref(ref_val)
        if ref_key and ref_key != "ANNEXURE_ANY":
            col_to_key[c] = ref_key

    if not col_to_key:
        return {}

    # For each annexure key, enumerate columns in left-to-right order → positions
    # {annex_key: [col1, col2, ...]}
    key_to_cols: dict[str, list[int]] = {}
    for c, key in sorted(col_to_key.items()):
        key_to_cols.setdefault(key, []).append(c)

    # Identify keys that appear in multiple columns (those need positional handling)
    multi_col_keys = {k for k, cols in key_to_cols.items() if len(cols) > 1}
    if not multi_col_keys:
        return {}

    # Read item rows: rows after header_end_row where item_num_col has an integer
    result: dict[str, list[list[int]]] = {}
    for key in multi_col_keys:
        cols = key_to_cols[key]
        result[key] = [[] for _ in cols]

    for r in range(header_end_row + 1, max_row + 1):
        raw_item = ws.cell(r, item_num_col).value
        if raw_item is None:
            continue
        try:
            item_num = int(float(str(raw_item).strip()))
        except (ValueError, TypeError):
            continue

        for key in multi_col_keys:
            cols = key_to_cols[key]
            for pos, c in enumerate(cols):
                v = ws.cell(r, c).value
                if v is not None and str(v).strip() not in ("", "0"):
                    result[key][pos].append(item_num)

    # Remove keys with all-empty position lists
    result = {k: v for k, v in result.items() if any(v)}
    return result


def _build_subgroup_item_map(
    wb, profiles: list[SheetProfile]
) -> dict[str, list[list[int]]]:
    """
    Merge per-column item sets from all continuation sheets.
    Returns {normalized_annexure_key: [[item_nums_pos0], [item_nums_pos1], ...]}
    for annexure keys that span multiple columns in at least one CONT sheet.
    """
    cont_profiles = [
        p for p in profiles
        if p.is_extractable
        and p.tag_layout == TagLayout.COLUMN_HEADERS
        and any(kw in p.name.lower() for kw in ("cont", "continuation"))
    ]
    merged: dict[str, list[list[int]]] = {}
    for cp in cont_profiles:
        ws = wb[cp.name]
        smap = _read_continuation_subgroup_items(ws)
        for key, item_lists in smap.items():
            if key not in merged:
                merged[key] = item_lists
        if merged:
            break
    return merged


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

        # Cross-check with an explicit-digit fallback for abbreviated names like
        # "Annx-11" where _normalize_annexure_ref may misread the trailing 'x'
        # as Roman numeral X=10. The fallback only matches decimal digits,
        # so it's unambiguous. Also covers "Anx-1(New-Skd)" that the main
        # regex doesn't match at all.
        _explicit_m = re.search(
            r"(?i)\b(?:annex(?:ure)?|ann?x)[\s\-_]*(\d+)", profile.name
        )
        if _explicit_m:
            _explicit_key = f"ANNEXURE{_explicit_m.group(1)}"
            if annex_key is None and profile.role == SheetRole.ANNEXURE:
                annex_key = _explicit_key
            elif annex_key is not None and annex_key != _explicit_key:
                # Main regex returned a wrong key (likely Roman-numeral misread).
                # Trust the explicit decimal-digit key instead.
                log.debug(
                    "Sheet '%s': key %s overridden by explicit-digit key %s",
                    profile.name, annex_key, _explicit_key,
                )
                annex_key = _explicit_key

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
            _ann_columnar = ColumnarStrategy()
            tag_info = _ann_columnar._read_tag_headers(ws, profile)
            meta, *_ = _ann_columnar._read_tag_metadata(ws, profile, tag_info)
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
    # Always run _scan_annexure_headers to get the theme-corrected tag column.
    # The profile's column_map uses longest-keyword-wins which may pick a
    # neighbouring tag column (e.g. "Pump Motor Tag No" over "Isolater Tag No"
    # on the "Annx-11 (Isolater)" sheet). The scanner's theme-based override
    # selects the column that best matches the sheet name instead.
    _scanner_col_map, scanner_header_row = _scan_annexure_headers(ws, sheet_name=profile.name)
    if not col_map:
        col_map = _scanner_col_map
    else:
        # Override only the tag column with the scanner's theme-corrected result.
        _s_tag = _scanner_col_map.get("tag")
        _profile_tag = col_map.get("tag")
        if _s_tag and _s_tag != _profile_tag:
            col_map = dict(col_map)
            col_map["tag"] = _s_tag
            col_map["all_tag_cols"] = _scanner_col_map.get("all_tag_cols", [_s_tag])
            # When the scanner moves the tag column to the right, any
            # model/serial columns that sat between the old and new tag column
            # belong to the OLD equipment class — clear them.
            if _profile_tag is not None and _profile_tag < _s_tag:
                for _field in ("model", "serial"):
                    _col_val = col_map.get(_field)
                    if _col_val is not None and _profile_tag <= _col_val < _s_tag:
                        col_map.pop(_field, None)
            # Pull in the scanner's cleaned model/serial when the profile lacks them
            for _field in ("model", "serial"):
                if _field not in col_map and _scanner_col_map.get(_field):
                    col_map[_field] = _scanner_col_map[_field]
        elif "all_tag_cols" not in col_map and _scanner_col_map.get("all_tag_cols"):
            col_map = dict(col_map)
            col_map["all_tag_cols"] = _scanner_col_map.get("all_tag_cols")
        # Always propagate cross_ref_tag_cols from the scanner (the profile
        # mapper doesn't track them).
        if _scanner_col_map.get("cross_ref_tag_cols"):
            if not isinstance(col_map, dict) or col_map is profile.column_map:
                col_map = dict(col_map)
            col_map["cross_ref_tag_cols"] = _scanner_col_map["cross_ref_tag_cols"]

    tag_col = col_map.get("tag")
    # All tag columns — for annexures with multiple side-by-side tag columns
    # (e.g. "JB Digital Tag no" and "JB Analogue Tag no" on same rows).
    all_tag_cols_list: list[int] = col_map.get("all_tag_cols") or ([tag_col] if tag_col else [])
    # Cross-reference tag columns (e.g. "Pump Motor Tag No" on the Isolater
    # annexure) — used to skip rows that belong to a different equipment.
    cross_ref_tag_cols: list[int] = col_map.get("cross_ref_tag_cols") or []
    model_col = col_map.get("model") or col_map.get("manufacturer_model")
    serial_col = col_map.get("serial")
    mfr_col = col_map.get("manufacturer")

    # Correct left-of-tag model/serial columns for multi-section annexure layouts.
    # (e.g. "SKID Model No" at col 4 beats "Pump Model No" at col 11 in first-match
    # scanning when the tag column is at col 10 — this corrects that.)
    if tag_col:
        fixed_model, fixed_serial = _fix_annexure_col_positions(
            ws, profile.header_row or 1, tag_col, model_col, serial_col,
        )
        if fixed_model is not None:
            model_col = fixed_model
        if fixed_serial is not None:
            serial_col = fixed_serial

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
    # Annexure sheets often have sparse data with blank rows between entries
    # (e.g. Anx-14 has SI.NO 1–7 close together then gaps to 8–12). The
    # header_detector's data_end_row stops at the first consecutive blank
    # block, missing the later entries. Always scan to the worksheet's max
    # row for annexure reading — the inner loop already skips empty rows.
    end_row = ws.max_row or 0

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
        # Read shared row values (model/serial/mfr apply to all tag columns)
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

        if len(all_tag_cols_list) > 1:
            # Multi-tag-column annexure: each column in the same row holds a different
            # tag (e.g. "JB Digital Tag no" and "JB Analogue Tag no"). Emit one entry
            # per column per row; all share the same model/serial/mfr from this row.
            row_had_tag = False
            for tc in all_tag_cols_list:
                tv = clean_str(ws.cell(r, tc).value)
                if not tv:
                    continue
                row_had_tag = True
                for tag in split_tags(tv):
                    entry: dict[str, Any] = {"tag": tag}
                    if model_val:
                        entry["model"] = model_val
                    if serial_val:
                        entry["serial"] = serial_val
                    if mfr_val:
                        entry["manufacturer"] = mfr_val
                    if group_val is not None:
                        entry["_group"] = group_val
                    entries.append(entry)
            if not row_had_tag:
                # Skip rows with no tag in any column
                continue
        else:
            tag_val = clean_str(ws.cell(r, tag_col).value)

            # Keep rows even when tag is blank — the tag may be missing/pending
            # but other data (serial, model, etc.) should still be extracted
            if not tag_val:
                # If this annexure has cross-reference tag columns (e.g. a
                # "Pump Motor Tag No" cross-ref on an "Isolater" annexure) and
                # any of them has a value for this row, the row belongs to a
                # DIFFERENT equipment that simply doesn't have an isolator —
                # skip it rather than emitting a blank-tag entry.
                if cross_ref_tag_cols and any(
                    clean_str(ws.cell(r, _xc).value) for _xc in cross_ref_tag_cols
                ):
                    continue
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

            # Handle serial ranges for multi-tag cells
            serials = _split_serial_range(serial_val, len(tags)) if serial_val else [None] * len(tags)

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
    # Track ALL columns that match any tag keyword (for multi-tag-column annexures
    # like Annexure 14 which has "JB Digital Tag no" and "JB Analogue Tag no").
    # Stored as col → (matched_keyword, kw_len, row) so we can compare matching
    # keywords later — peer columns share the same matching keyword.
    all_tag_col_set: dict[int, tuple[str, int, int]] = {}

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
                        if field == "tag":
                            # Collect every column that looks like a tag column,
                            # keeping the longest-matching keyword per column.
                            if c not in all_tag_col_set or kw_len > all_tag_col_set[c][1]:
                                all_tag_col_set[c] = (kw, kw_len, r)
                        break  # one keyword per cell per field

    # Row-preference fix: if the best "tag" column is on a different row from the
    # model/serial columns, prefer a tag column that IS on the same row as model/serial.
    # This prevents a sheet-title cell in row 1 from winning as the primary tag column
    # when the actual tag header is in row 2 alongside the model/serial headers.
    if "tag" in best_match:
        _tag_row = best_match["tag"][2]
        _anchor_rows = {best_match[f][2] for f in ("model", "serial") if f in best_match}
        if _anchor_rows and _tag_row not in _anchor_rows:
            _preferred_row = next(iter(_anchor_rows))
            for _c, (_kw, _kw_len, _r) in all_tag_col_set.items():
                if _r == _preferred_row:
                    best_match["tag"] = (_c, _kw_len, _r)
                    break

    # Theme-based tag column override: if the sheet name contains words not present
    # in the best-matched tag column header, look for an alternative "tag" column
    # whose header shares more words with the sheet name.  This disambiguates sheets
    # that have multiple tag-like columns (e.g. "Pump Motor Tag No" vs "Isolater Tag No").
    _pre_theme_primary_col: int | None = (
        best_match["tag"][0] if "tag" in best_match else None
    )
    if sheet_name and "tag" in best_match:
        current_tag_col = best_match["tag"][0]
        theme_col = _find_theme_tag_col(
            ws, sheet_name, current_tag_col, max_col, scan_rows
        )
        if theme_col is not None:
            best_match["tag"] = (theme_col, best_match["tag"][1], best_match["tag"][2])

    col_map = {field: col for field, (col, _, _) in best_match.items()}
    header_row_found = max((row for _, _, row in best_match.values()), default=1) if best_match else 1

    # When theme override moves the primary tag column to the right (e.g.
    # old primary was col 4 "Pump Motor Tag", new primary is col 8 "ECP Tag"),
    # any model/serial column that sits between the old and new primary belongs
    # to the OLD equipment class — clear it so the wrong serial/model is not
    # carried over to entries for the new equipment.
    if (
        _pre_theme_primary_col is not None
        and col_map.get("tag") is not None
        and _pre_theme_primary_col < col_map["tag"]
    ):
        _new_tag = col_map["tag"]
        for _field in ("model", "serial"):
            _col_val = col_map.get(_field)
            if _col_val is not None and _pre_theme_primary_col <= _col_val < _new_tag:
                col_map.pop(_field, None)

    # Store all detected tag columns so _read_annexure_equipment can read from each.
    # Peer columns share the SAME matching keyword as the primary tag column (e.g.
    # "JB Digital Tag no" and "JB Analogue Tag no" both match "tag no").
    # Non-peer columns like "Pump Motor Tag No" (a cross-reference column on the
    # Isolater annexure) match a different keyword and must be excluded.
    _primary_tag = col_map.get("tag")
    _primary_kw = all_tag_col_set.get(_primary_tag, (None, 0, 0))[0] if _primary_tag else None
    _theme_displaced = (
        _pre_theme_primary_col is not None
        and _pre_theme_primary_col != _primary_tag
    )

    _primary_row = all_tag_col_set.get(_primary_tag, (None, 0, 0))[2] if _primary_tag else None
    _peer_tag_cols: list[int] = []
    _cross_ref_tag_cols: list[int] = []
    for c, (kw, _kw_len, _row) in all_tag_col_set.items():
        if c == _primary_tag:
            continue
        # Theme override happened → the primary column is uniquely identified
        # by the sheet's theme. All other tag-like columns are cross-references
        # to different equipment classes, never peers.
        if _theme_displaced:
            _cross_ref_tag_cols.append(c)
            continue
        # Peers must be on the SAME header row as the primary tag column.
        # A tag-like cell on a different row (e.g. a sheet-title row) is not a peer.
        if _primary_row is not None and _row != _primary_row:
            _cross_ref_tag_cols.append(c)
            continue
        # No theme override → peers must share the primary's matching keyword.
        # Cross-reference cols typically use a more specific keyword (e.g.
        # "pump motor tag" rather than the generic "tag no" that peers share).
        if _primary_kw is not None and kw != _primary_kw:
            _cross_ref_tag_cols.append(c)
            continue
        _peer_tag_cols.append(c)
    _peer_tag_cols.sort()
    _cross_ref_tag_cols.sort()

    if _primary_tag:
        col_map["all_tag_cols"] = [_primary_tag] + _peer_tag_cols
    elif _peer_tag_cols:
        col_map["all_tag_cols"] = _peer_tag_cols

    # Cross-reference tag columns (e.g. "Pump Motor Tag No" on the Isolater
    # annexure) — these are NOT peers but signal that a row is about a different
    # equipment. _read_annexure_equipment uses them to skip rows whose primary
    # tag is blank but a cross-ref column has a value.
    if _cross_ref_tag_cols:
        col_map["cross_ref_tag_cols"] = _cross_ref_tag_cols

    # Correct left-of-tag model/serial columns (same logic as _fix_annexure_col_positions).
    tag_c = col_map.get("tag")
    if tag_c:
        fixed_model, fixed_serial = _fix_annexure_col_positions(
            ws, header_row_found, tag_c,
            col_map.get("model"),
            col_map.get("serial"),
        )
        if fixed_model is not None:
            col_map["model"] = fixed_model
        if fixed_serial is not None:
            col_map["serial"] = fixed_serial

    return col_map, header_row_found


def _fix_annexure_col_positions(
    ws, header_row: int, tag_col: int, model_col, serial_col
) -> tuple:
    """
    Correct model/serial column detection for multi-section annexure layouts.

    Some annexure sheets have a left section (skid / manifold context columns such
    as "SKID Model No", "SKID MFG Serial no") followed by a right section that
    contains the real equipment columns ("Pump Model No", "Pump Serial N°", "S/N").

    When model_col or serial_col land LEFT of tag_col the column mapper picked from
    the wrong section.  This function rescans the header row starting just AFTER
    tag_col to find a better match.  If nothing is found to the right, the original
    column is kept — so the fix is always conservative.

    Additionally, when serial_col is None, the scan looks for "s/n" / "s.n." headers
    that the column mapper maps to item_number instead of serial.

    Returns (corrected_model_col, corrected_serial_col).
    A None return value means "no change" for that field.
    """
    needs_model_fix  = model_col  is not None and model_col  < tag_col
    needs_serial_fix = serial_col is None     or (serial_col is not None and serial_col < tag_col)

    if not needs_model_fix and not needs_serial_fix:
        return None, None

    _MODEL_KWS  = ["model no", "model number", "model"]
    _SERIAL_KWS = ["serial no", "serial number", "serial", "ser no", "s/n", "s.n."]

    max_col = min(ws.max_column or 20, 30)
    new_model  = None
    new_serial = None

    # Scan the header row (and one row above for tolerance).
    # Do NOT scan below the header row — data cells can contain values with
    # serial-like substrings (e.g. "B6FX50S/FS/NA" contains "s/n") which
    # would cause the model column to be wrongly re-used as serial.
    for r in range(max(1, header_row - 1), min(header_row + 1, 9)):
        for c in range(tag_col + 1, max_col + 1):
            raw = ws.cell(r, c).value
            if raw is None:
                continue
            cell_lower = str(raw).lower().strip()

            if needs_model_fix and new_model is None:
                if any(kw in cell_lower for kw in _MODEL_KWS):
                    new_model = c

            if needs_serial_fix and new_serial is None:
                # Don't assign serial to the column already identified as model
                if c != model_col and any(kw in cell_lower for kw in _SERIAL_KWS):
                    new_serial = c

        if (new_model is not None or not needs_model_fix) and \
           (new_serial is not None or not needs_serial_fix):
            break

    final_model  = new_model  if (needs_model_fix  and new_model  is not None) else None
    final_serial = new_serial if  needs_serial_fix                              else None

    if final_model is not None:
        log.info(
            "[annexure_col_fix] header_row=%d tag_col=%d: model col %s→%s "
            "(left-of-tag corrected to right-section column)",
            header_row, tag_col, model_col, final_model,
        )
    if final_serial is not None:
        log.info(
            "[annexure_col_fix] header_row=%d tag_col=%d: serial col %s→%s (%s)",
            header_row, tag_col, serial_col, final_serial,
            "S/N detection" if serial_col is None else "left-of-tag corrected",
        )

    return final_model, final_serial


def _find_theme_tag_col(ws, sheet_name: str, current_col: int, max_col: int, scan_rows: int):
    """
    Return an alternative tag column whose header better matches the sheet name's
    theme words, or None if the current column is already the best fit.

    Theme words are extracted by stripping the leading annexure identifier
    (e.g. "Annx-11") and then collecting distinct alphabetic words of 3+ chars.

    Also detects uppercase abbreviations (e.g. "ECP" → "Electric Control Panel"):
    a 2–5-uppercase-letter token in the sheet name is matched against the initials
    of multi-word tag column headers.
    """
    _STOP_WORDS = frozenset({"the", "and", "for", "with", "new", "old", "tab", "page", "sheet"})
    # Strip leading "Annx-N / Anx-N / Annexure-N" prefix then extract words
    cleaned = re.sub(r"(?i)^ann?(?:ex(?:ure)?)?[\s\-_]*\d+[\s\-_]*", "", sheet_name)
    theme_words = {
        w for w in re.findall(r"[a-z]{3,}", cleaned.lower())
        if w not in _STOP_WORDS
    }
    # Uppercase abbreviations in the ORIGINAL case (e.g. "ECP" in "(ECP- 4Pumps)")
    theme_abbrevs = {
        a.upper() for a in re.findall(r"\b[A-Z]{2,5}\b", cleaned)
    }
    if not theme_words and not theme_abbrevs:
        return None

    # Score every column that contains "tag" in its header
    best_col = None
    best_score = 0
    for r in range(1, scan_rows):
        for c in range(1, max_col + 1):
            v = ws.cell(r, c).value
            if v is None:
                continue
            cell_val = str(v)
            cell_lower = cell_val.lower()
            if "tag" not in cell_lower:
                continue
            score = sum(1 for w in theme_words if w in cell_lower)
            # Abbreviation match: build initials from each multi-word substring
            # in the header (e.g. "Electric Control Panel Tag no" → "ECPTN").
            if theme_abbrevs:
                # Take the part before "tag" (case-insensitive) as the equipment
                # name, then build initials from its words.
                _pre_tag = re.split(r"(?i)\btag\b", cell_val, maxsplit=1)[0]
                _initials = "".join(
                    w[0].upper() for w in re.findall(r"[A-Za-z]+", _pre_tag)
                )
                for abbr in theme_abbrevs:
                    if abbr in _initials:
                        score += 2  # abbreviation match weighs more than word match
                        break
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

    # Numeric hyphen range: "240430-240431" → ["240430", "240431"]
    # Only triggers when all parts are pure digits and count matches expected tags.
    if "-" in s and expected_count > 1:
        parts = [p.strip() for p in s.split("-") if p.strip()]
        if len(parts) == expected_count and all(p.isdigit() for p in parts):
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


def _attach_vendor_info(
    wb,
    all_rows: list[dict[str, Any]],
    profiles: list[SheetProfile],
) -> None:
    """
    Extract vendor contact details from the MANUFACTURERS/SUPPLIERS FOCAL POINT cell
    and attach the 5 vendor fields to every row.

    Modifies rows in-place by adding new keys only — never overwrites existing fields.
    Safe under Celery: no shared/global state, one call per workbook execution.
    """
    from spir_dynamic.services.vendor_extractor import find_focal_point_cell, extract_vendor_details

    # Resolve supplier name from profile metadata (already extracted by find_metadata)
    supplier_name = ""
    for p in profiles:
        s = p.metadata.get("supplier", "")
        if s and str(s).strip():
            supplier_name = str(s).strip()
            break

    # Scan non-annexure sheets for the focal point cell
    focal_text: str | None = None
    for profile in profiles:
        if profile.role == SheetRole.ANNEXURE:
            continue
        try:
            ws = wb[profile.name]
            focal_text = find_focal_point_cell(ws)
            if focal_text:
                break
        except Exception as exc:
            log.debug("Vendor focal point scan failed for '%s': %s", profile.name, exc)

    if not focal_text:
        log.debug("vendor_extractor: focal text NOT found, supplier=%s", supplier_name)

    if not focal_text and not supplier_name:
        return

    details = extract_vendor_details(focal_text or "", supplier_name)

    # Map extractor keys → output schema field names
    vendor_fields: dict[str, str] = {
        "vendor_name":    details.get("vendor_name", ""),
        "vendor_email1":  details.get("email1", ""),
        "vendor_email2":  details.get("email2", ""),
        "vendor_contact": details.get("contact", ""),
        "vendor_country": details.get("country", ""),
    }

    # Attach to every row: add new keys only, skip empty values
    for row in all_rows:
        for k, v in vendor_fields.items():
            if v is not None and v != "":
                row[k] = v


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
    if re.search(r"(?i)ann(?:ex|e)", text):
        return "ANNEXURE_ANY"
    return None


def _is_annexure_sheet(sheet_name: str, profiles: list[SheetProfile]) -> bool:
    """Check if a sheet name belongs to an annexure-classified sheet."""
    for p in profiles:
        if p.name.upper() == sheet_name and p.role == SheetRole.ANNEXURE:
            return True
    return False
