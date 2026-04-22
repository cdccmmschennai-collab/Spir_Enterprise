"""Verify the Pass 1.5 re-resolution bug: does ANNEXURE1-2 get overwritten?"""
import sys
sys.path.insert(0, 'src')
import re
from spir_dynamic.extraction.unified_extractor import _normalize_annexure_ref, _resolve_subgroup_key

# Simulate the registry with subgroups
registry = {
    "ANNEXURE1-1": [{"tag": "BV-X741"}, {"tag": "BV-X742"}, {"tag": "BV-X743"}, {"tag": "BV-C11"}],
    "ANNEXURE1-2": [{"tag": "BV-X660"}],
    "ANNEXURE1-3": [{"tag": "BV-X832"}, {"tag": "BV-X833"}],
    "ANNEXURE1-4": [{"tag": "t1"}, {"tag": "t2"}, {"tag": "t3"}, {"tag": "t4"}, {"tag": "t5"},
                    {"tag": "t6"}, {"tag": "t7"}, {"tag": "t8"}, {"tag": "t9"}, {"tag": "t10"}, {"tag": "t11"}],
    "ANNEXURE2-1": [{"tag": "NRV-X523"}],
    "ANNEXURE2-2": [{"tag": "a"}, {"tag": "b"}, {"tag": "c"}, {"tag": "d"}, {"tag": "e"}],
    "ANNEXURE2-3": [{"tag": "x1"}, {"tag": "x2"}],
}

# Simulate 4 MAIN SHEET-1 header rows
rows = [
    {"tag_no": "REFER TO ANNEX 1", "eqpt_qty": 4, "sheet": "MAIN SHEET-1", "item_num": None},
    {"tag_no": "REFER TO ANNEX 2", "eqpt_qty": 1, "sheet": "MAIN SHEET-1", "item_num": None},
    {"tag_no": "REFER TO ANNEX 3", "eqpt_qty": 2, "sheet": "MAIN SHEET-1", "item_num": None},
    {"tag_no": "REFER TO ANNEX 4", "eqpt_qty": 11, "sheet": "MAIN SHEET-1", "item_num": None},
]

print("=== BEFORE Pass 1 ===")
for r in rows:
    print(f"  {r['tag_no']}, eqpt_qty={r['eqpt_qty']}")

# Pass 1
_header_resolved = {}
_sheet_to_prefix = {}
for _row in rows:
    if _row.get("item_num") is not None:
        continue
    _tag = str(_row.get("tag_no") or "").strip()
    _key = _normalize_annexure_ref(_tag)
    if _key and _key != "ANNEXURE_ANY" and _key not in registry:
        _sheet_name = _row.get("sheet") or ""
        _prefer = _sheet_to_prefix.get(_sheet_name)
        _resolved = _resolve_subgroup_key(_key, registry, _row.get("eqpt_qty"), prefer_prefix=_prefer)
        print(f"  Pass1: tag={_tag!r}, key={_key!r}, prefer={_prefer!r} -> resolved={_resolved!r}")
        if _resolved:
            _header_resolved[(_sheet_name, _tag)] = _resolved
            _row["tag_no"] = _resolved
            if _sheet_name and _sheet_name not in _sheet_to_prefix:
                _pfx_match = re.match(r"([A-Z]+\d+)-", _resolved)
                if _pfx_match:
                    _sheet_to_prefix[_sheet_name] = _pfx_match.group(1)

print("\n=== AFTER Pass 1 ===")
for r in rows:
    print(f"  {r['tag_no']}, eqpt_qty={r['eqpt_qty']}, id={id(r)}")

# Pass 1.5
for _row in rows:
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
    print(f"  Pass1.5: tag={_tag!r}, key={_key!r}, prefer={_prefer!r}, qty={_row.get('eqpt_qty')!r} -> resolved={_resolved!r}")
    if _resolved:
        _header_resolved[(_sheet_name, _tag)] = _resolved
        _row["tag_no"] = _resolved

print("\n=== AFTER Pass 1.5 ===")
for r in rows:
    print(f"  {r['tag_no']}, eqpt_qty={r['eqpt_qty']}")
