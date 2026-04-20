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
# "Refer Annexure 3", "ANNEXURE (P1)-1", "ANNEXURE (P2)-3", etc.
_ANNEXURE_REF_RE = re.compile(
    r"(?:refer\s+)?annexure[\s\-_]*(?:\(([^)]*)\)[\s\-_]*)?(\d+|[IVX]+)\b",
    re.IGNORECASE,
)

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
    columnar_profiles = [
        p for p in profiles
        if p.is_extractable and p.tag_layout == TagLayout.COLUMN_HEADERS
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

    1. Find the "item source" sheet (has description/price columns)
    2. Read items from it
    3. For each sheet (including source), extract using shared items_dict
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

    # PHASE 2 FIX: Merge items from other columnar sheets that have
    # description data missing from the item source.
    # Some SPIR files split items across sheets (e.g., items 1-18 in MAIN SHEET,
    # items 19-24 in MAIN SHEET (3)). The item source may have item numbers
    # for 19-24 but no description/part_number data.
    for profile in columnar_profiles:
        if profile.name == item_source_name:
            continue
        ws = wb[profile.name]
        other_items = _columnar.read_items(ws, profile)
        for item_num, item_data in other_items.items():
            if item_num not in items_dict:
                # New item not in source — add it
                items_dict[item_num] = item_data
            elif not items_dict[item_num].get("desc") and item_data.get("desc"):
                # Source has item number but no description — fill from other sheet
                items_dict[item_num].update(item_data)

    # Fix C: ensure item source is extracted first so its metadata_field_rows
    # are available for continuation sheets
    ordered_profiles = sorted(
        columnar_profiles,
        key=lambda p: 0 if p.name == item_source_name else 1,
    )

    # Extract from each columnar sheet
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

    # Step 2: Build tag→equipment lookup from annexure + continuation rows
    tag_equip = _build_tag_equipment_lookup(all_rows, profiles, annexure_registry)

    # Step 3: Determine which annexure sheets are being resolved via fan-out
    resolved_annexure_keys: set[str] = set()
    for row in all_rows:
        tag = row.get("tag_no") or ""
        annex_key = _normalize_annexure_ref(tag)
        if annex_key and annex_key in annexure_registry:
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

        annex_key = _normalize_annexure_ref(tag)
        if annex_key and annex_key in annexure_registry:
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

            # Per-tag pairs: for each real tag, emit header + spares
            # Only enrich model/serial from annexure; manufacturer stays
            # from global metadata (top-right of SPIR = EQPT MAKE)
            _TAG_ENRICH = ("model", "serial")
            N = len(annex_tags)
            for tdata in annex_tags:
                # Tag header
                for hdr in grp["headers"]:
                    tag_hdr = dict(hdr)
                    tag_hdr["tag_no"] = tdata["tag"]
                    for field in _TAG_ENRICH:
                        if tdata.get(field):
                            tag_hdr[field] = tdata[field]
                    enriched.append(tag_hdr)
                # Tag spare rows — divide total qty by N annexure tags
                for dtl in grp["details"]:
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


def _build_annexure_registry(
    wb, profiles: list[SheetProfile]
) -> dict[str, list[dict[str, Any]]]:
    """
    Read equipment data directly from annexure-classified sheets.

    Returns {normalized_key: [{tag, model, serial, manufacturer}, ...]}.

    Handles two patterns:
      A) Simple: sheet "Annexure 1" → key "ANNEXURE1", all rows are one group
      B) Grouped: sheet "ANNEXURE-P1" has a group number column (e.g.
         "ANNEXURE-P1 NUMBER" with values 1,2,3,4) → creates keys
         "ANNEXUREP1-1", "ANNEXUREP1-2", etc.
    """
    registry: dict[str, list[dict[str, Any]]] = {}

    for profile in profiles:
        annex_key = _normalize_annexure_ref(profile.name)
        is_annexure_like_sheet = (
            profile.role == SheetRole.ANNEXURE or annex_key is not None
        )
        if not is_annexure_like_sheet:
            continue

        if not annex_key:
            annex_key = profile.name.strip().upper()

        ws = wb[profile.name]

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
        if "annexure" in s and ("number" in s or "num" in s or "#" in s):
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
    print(f"DEBUG _read_annexure_equipment: sheet={profile.name!r} role={profile.role} layout={profile.tag_layout}")
    print(f"DEBUG _read_annexure_equipment: profile.column_map={profile.column_map}")
    entries: list[dict[str, Any]] = []

    col_map = profile.column_map
    if not col_map:
        col_map = _scan_annexure_headers(ws)

    tag_col = col_map.get("tag")
    print(f"DEBUG _read_annexure_equipment: tag_col={tag_col} col_map={col_map}")
    model_col = col_map.get("model") or col_map.get("manufacturer_model")
    serial_col = col_map.get("serial")
    mfr_col = col_map.get("manufacturer")

    # If manufacturer and model point to the same column (e.g. "Manufacturer Model No"),
    # treat it as model-only — the real manufacturer comes from sheet metadata.
    if mfr_col and model_col and mfr_col == model_col:
        mfr_col = None

    start_row = profile.data_start_row or (
        (profile.header_row + 1) if profile.header_row else 3
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
        print(f"DEBUG _read_annexure_equipment: data-scan best_col={best_col} best_count={best_count} start_row={start_row} scan_end={scan_end}")
        if best_col and best_count >= 1:
            tag_col = best_col
            log.info(
                "Annexure '%s': tag column not found by header; using col %d "
                "(%d tag-like values via data scan)",
                getattr(profile, "name", "?"), tag_col, best_count,
            )
        else:
            print(f"DEBUG _read_annexure_equipment: GIVING UP - no tag column found")
            return entries  # truly can't find tags

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

        # Read group number if group_col is provided
        group_val = None
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


def _scan_annexure_headers(ws) -> dict[str, int]:
    """Scan first rows of an annexure sheet to find tag/model/serial columns."""
    print(f"DEBUG _scan_annexure_headers: sheet={getattr(ws, 'title', '?')} max_row={ws.max_row} max_col={ws.max_column}")
    col_map: dict[str, int] = {}
    keywords = {
        "tag": ["tag no", "tag number", "tag number(s)", "valve tag", "equipment tag", "equip", "tag"],
        "model": ["model number", "model no", "model", "mfr type", "manufacturer model"],
        "serial": ["serial number", "serial no", "serial", "ser no", "sr no"],
        "manufacturer": ["manufacturer", "make", "mfr name"],
    }

    max_col = min(ws.max_column or 10, 20)
    for r in range(1, min(6, (ws.max_row or 0) + 1)):
        for c in range(1, max_col + 1):
            v = ws.cell(r, c).value
            if v is None:
                continue
            cell_lower = str(v).lower().strip()
            print(f"DEBUG _scan_annexure_headers: r={r} c={c} value={repr(v)}")
            for field, kws in keywords.items():
                if field not in col_map and any(kw in cell_lower for kw in kws):
                    col_map[field] = c
                    print(f"DEBUG _scan_annexure_headers: matched field={field} col={c} value={repr(v)}")
                    break

    print(f"DEBUG _scan_annexure_headers: result col_map={col_map}")
    return col_map


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
    "ANNEXURE (P1)-1"     → "ANNEXUREP1-1"
    "ANNEXURE (P2)-3"     → "ANNEXUREP2-3"
    "Annexure I"           → "ANNEXURE1"
    "Annexure II"          → "ANNEXURE2"
    Returns None if value is not an annexure reference.
    """
    if not value:
        return None
    m = _ANNEXURE_REF_RE.search(str(value).strip())
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
    cleaned = re.sub(r"[\s\-_]+", "", str(value).strip().upper())
    if cleaned.startswith("ANNEXURE") and any(c.isdigit() for c in cleaned):
        return cleaned
    # Bare "Refer Annexure" / "Annexure" without a number — sentinel for single-sheet resolution
    if re.search(r"(?i)annex", str(value).strip()):
        return "ANNEXURE_ANY"
    return None


def _is_annexure_sheet(sheet_name: str, profiles: list[SheetProfile]) -> bool:
    """Check if a sheet name belongs to an annexure-classified sheet."""
    for p in profiles:
        if p.name.upper() == sheet_name and p.role == SheetRole.ANNEXURE:
            return True
    return False
