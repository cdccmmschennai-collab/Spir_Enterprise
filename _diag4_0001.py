"""Full simulation of Step 1c + Step 4 to count what goes into grp['headers']."""
import sys
sys.path.insert(0, 'src')
import logging, re
logging.basicConfig(level=logging.WARNING, stream=sys.stdout)

import openpyxl
from collections import defaultdict
from spir_dynamic.extraction.unified_extractor import (
    _normalize_annexure_ref, _resolve_subgroup_key, _get_annexure_key,
    _build_annexure_registry, _find_item_source, _extract_columnar_group
)
from spir_dynamic.analysis.workbook_analyzer import analyze_workbook
from spir_dynamic.models.sheet_profile import TagLayout

wb = openpyxl.load_workbook(r'templates/bad working/VEN-4460-DGEN-5-43-0001-1.xlsm', data_only=True)
profiles = analyze_workbook(wb)
registry = _build_annexure_registry(wb, profiles)
columnar_profiles = [p for p in profiles if p.is_extractable and p.tag_layout == TagLayout.COLUMN_HEADERS]

spir_no = "VEN-4460-DGEN-5-43-0001"
raw_rows = _extract_columnar_group(wb, columnar_profiles, spir_no, profiles)

# Simulate Step 1c (Pass 1)
_header_resolved = {}
_sheet_to_prefix = {}

for _row in raw_rows:
    if _row.get("item_num") is not None:
        continue
    _tag = str(_row.get("tag_no") or "").strip()
    _key = _normalize_annexure_ref(_tag)
    if _key and _key != "ANNEXURE_ANY" and _key not in registry:
        _sheet_name = _row.get("sheet") or ""
        _prefer = _sheet_to_prefix.get(_sheet_name)
        _resolved = _resolve_subgroup_key(_key, registry, _row.get("eqpt_qty"), prefer_prefix=_prefer)
        if _resolved:
            _header_resolved[(_sheet_name, _tag)] = _resolved
            _row["tag_no"] = _resolved
            if _sheet_name and _sheet_name not in _sheet_to_prefix:
                _pfx_match = re.match(r"([A-Z]+\d+)-", _resolved)
                if _pfx_match:
                    _sheet_to_prefix[_sheet_name] = _pfx_match.group(1)

# Simulate Pass 1.5
for _row in raw_rows:
    if _row.get("item_num") is not None:
        continue
    _tag = str(_row.get("tag_no") or "").strip()
    _key = _normalize_annexure_ref(_tag)
    if not (_key and _key != "ANNEXURE_ANY" and _key not in registry):
        continue
    _sheet_name = _row.get("sheet") or ""
    _prefer = _sheet_to_prefix.get(_sheet_name)
    if not _prefer:
        continue
    _resolved = _resolve_subgroup_key(_key, registry, _row.get("eqpt_qty"), prefer_prefix=_prefer)
    if _resolved:
        _header_resolved[(_sheet_name, _tag)] = _resolved
        _row["tag_no"] = _resolved

# Simulate Pass 2 (item rows)
for _row in raw_rows:
    if _row.get("item_num") is None:
        continue
    _orig_tag = str(_row.get("tag_no") or "").strip()
    _sheet_name = _row.get("sheet") or ""
    _resolved = _header_resolved.get((_sheet_name, _orig_tag))
    if _resolved:
        _row["tag_no"] = _resolved

# Now check what the header rows look like after Step 1c
hdr_rows = [r for r in raw_rows if r.get("item_num") is None]
dtl_rows = [r for r in raw_rows if r.get("item_num") is not None]
print(f"After Step 1c: {len(hdr_rows)} header rows, {len(dtl_rows)} detail rows")

still_unresolved = [(r.get("sheet"), r.get("tag_no")) for r in hdr_rows if _normalize_annexure_ref(r.get("tag_no","")) in (None, "ANNEXURE_ANY") and "ANNEX" not in str(r.get("tag_no",""))]
annex_keys_in_hdrs = [(r.get("sheet"), r.get("tag_no"), r.get("eqpt_qty")) for r in hdr_rows]
print(f"Header row (sheet, tag, eqpt_qty) after Step 1c:")
for sh, tag, qty in annex_keys_in_hdrs[:20]:
    annex_key = _get_annexure_key(tag or "", registry)
    print(f"  sheet={sh}, tag={tag}, qty={qty} -> _get_annexure_key returns: {annex_key}")

# Simulate Step 4: build annex_groups
annex_groups = {}
non_annex_rows = []
for idx, row in enumerate(raw_rows):
    tag = row.get("tag_no") or ""
    annex_key = _get_annexure_key(tag, registry)
    if annex_key:
        if annex_key not in annex_groups:
            annex_groups[annex_key] = {"headers": [], "details": [], "order": idx}
        if row.get("item_num"):
            annex_groups[annex_key]["details"].append(row)
        else:
            existing_tags = {h.get("tag_no") for h in annex_groups[annex_key]["headers"]}
            if tag not in existing_tags:
                annex_groups[annex_key]["headers"].append(row)
    else:
        non_annex_rows.append((idx, row))

# Report
empty_hdr_groups = {k: v for k,v in annex_groups.items() if not v["headers"]}
has_hdr_groups = {k: v for k,v in annex_groups.items() if v["headers"]}
print(f"\nStep 4 results: {len(annex_groups)} annex groups")
print(f"  Groups WITH headers: {len(has_hdr_groups)}")
print(f"  Groups WITHOUT headers: {len(empty_hdr_groups)}")
print(f"  Non-annex rows: {len(non_annex_rows)}")
print(f"\nGroups with headers: {list(has_hdr_groups.keys())}")
print(f"\nGroups without headers (first 10): {list(empty_hdr_groups.keys())[:10]}")
print(f"\nNon-annex rows sample:")
for idx, r in non_annex_rows[:5]:
    print(f"  tag={r.get('tag_no')}, item_num={r.get('item_num')}, sheet={r.get('sheet')}")
