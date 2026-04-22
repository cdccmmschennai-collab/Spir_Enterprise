import sys
sys.path.insert(0, 'src')
import logging
logging.basicConfig(level=logging.WARNING, stream=sys.stdout)

import openpyxl
from spir_dynamic.extraction.unified_extractor import (
    extract_workbook, _normalize_annexure_ref, _resolve_subgroup_key,
    _build_annexure_registry, _get_annexure_key
)
from spir_dynamic.analysis.workbook_analyzer import analyze_workbook
from spir_dynamic.extraction.strategies.columnar import ColumnarStrategy
from collections import defaultdict

wb = openpyxl.load_workbook(r'templates/bad working/VEN-4460-DGEN-5-43-0001-1.xlsm', data_only=True)
profiles = analyze_workbook(wb)

# Build annexure registry to inspect it
registry = _build_annexure_registry(wb, profiles)
print("=== Annexure Registry Keys ===")
for k, v in sorted(registry.items()):
    print(f"  {k}: {len(v)} tags -> {[e['tag'] for e in v[:3]]}{'...' if len(v)>3 else ''}")

# Run extraction to see raw rows before enrichment
from spir_dynamic.extraction.unified_extractor import _find_item_source
_columnar = ColumnarStrategy()

columnar_profiles = [p for p in profiles if p.is_extractable and p.tag_layout.value == 'column_headers']
print(f"\n=== Columnar Profiles: {[p.name for p in columnar_profiles]} ===")

item_source = _find_item_source(columnar_profiles)
print(f"Item source: {item_source.name if item_source else None}")

# Read tag headers for MAIN SHEET-1
main1 = wb['MAIN SHEET-1']
main1_profile = next(p for p in columnar_profiles if p.name == 'MAIN SHEET-1')
tag_info = _columnar._read_tag_headers(main1, main1_profile)
print(f"\n=== MAIN SHEET-1 tag_info ===")
for col, tags in sorted(tag_info.items()):
    print(f"  col={col}: tags={tags}")

tag_meta = _columnar._read_tag_metadata(main1, main1_profile, tag_info)
print(f"\n=== MAIN SHEET-1 tag_metadata ===")
for tag, meta in tag_meta.items():
    print(f"  {tag}: {meta}")

# Now trace what Step 1c does with these
print("\n=== Step 1c resolution trace ===")
for tag, meta in tag_meta.items():
    key = _normalize_annexure_ref(tag)
    eqpt_qty = meta.get('eqpt_qty')
    if key and key != 'ANNEXURE_ANY' and key not in registry:
        resolved = _resolve_subgroup_key(key, registry, eqpt_qty)
        print(f"  tag='{tag}' eqpt_qty={eqpt_qty} normalized='{key}' -> resolved='{resolved}'")
    elif key in registry:
        print(f"  tag='{tag}' eqpt_qty={eqpt_qty} normalized='{key}' -> DIRECT MATCH in registry")
    else:
        print(f"  tag='{tag}' eqpt_qty={eqpt_qty} normalized='{key}' -> ANNEXURE_ANY or None")
