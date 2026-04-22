"""Targeted diagnostic: trace what goes into grp['headers'] vs grp['details']."""
import sys
sys.path.insert(0, 'src')
import logging
logging.basicConfig(level=logging.WARNING, stream=sys.stdout)

import openpyxl
from collections import defaultdict
from spir_dynamic.extraction.unified_extractor import (
    _normalize_annexure_ref, _resolve_subgroup_key, _get_annexure_key,
    _build_annexure_registry, _find_item_source, _extract_columnar_group
)
from spir_dynamic.analysis.workbook_analyzer import analyze_workbook
from spir_dynamic.models.sheet_profile import TagLayout, SheetRole
from spir_dynamic.extraction.strategies.columnar import ColumnarStrategy
import re

wb = openpyxl.load_workbook(r'templates/bad working/VEN-4460-DGEN-5-43-0001-1.xlsm', data_only=True)
profiles = analyze_workbook(wb)
registry = _build_annexure_registry(wb, profiles)
columnar_profiles = [p for p in profiles if p.is_extractable and p.tag_layout == TagLayout.COLUMN_HEADERS]

# Get the raw rows
spir_no = "VEN-4460-DGEN-5-43-0001"
raw_rows = _extract_columnar_group(wb, columnar_profiles, spir_no, profiles)
print(f"Raw rows before enrichment: {len(raw_rows)}")

# Count raw headers vs details
raw_headers = [r for r in raw_rows if r.get('item_num') is None]
raw_details = [r for r in raw_rows if r.get('item_num') is not None]
print(f"Raw header rows: {len(raw_headers)}, raw detail rows: {len(raw_details)}")
print("Sample raw headers:")
for r in raw_headers[:10]:
    print(f"  sheet={r.get('sheet')}, tag={r.get('tag_no')}, eqpt_qty={r.get('eqpt_qty')}")

# Now trace Step 1c manually
print("\n=== Step 1c simulation ===")
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
            if _sheet_name and _sheet_name not in _sheet_to_prefix:
                _pfx_match = re.match(r"([A-Z]+\d+)-", _resolved)
                if _pfx_match:
                    _sheet_to_prefix[_sheet_name] = _pfx_match.group(1)
            print(f"  PASS1 RESOLVED: sheet={_sheet_name}, tag={_tag}, qty={_row.get('eqpt_qty')} -> {_resolved}")
        else:
            print(f"  PASS1 UNRESOLVED: sheet={_sheet_name}, tag={_tag}, qty={_row.get('eqpt_qty')}, key={_key}, prefer={_prefer}")

print(f"\nSheet prefixes learned: {_sheet_to_prefix}")
print(f"Total resolved: {len(_header_resolved)}")
print(f"Unique (sheet,tag) pairs in raw headers: {len(set((r.get('sheet'), r.get('tag_no')) for r in raw_headers))}")
