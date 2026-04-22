"""Count real unique tags in annexure registry and match with extraction."""
import sys
sys.path.insert(0, 'src')
import openpyxl
from collections import defaultdict
from spir_dynamic.extraction.unified_extractor import _build_annexure_registry, extract_workbook
from spir_dynamic.analysis.workbook_analyzer import analyze_workbook

wb = openpyxl.load_workbook(r'templates/bad working/VEN-4460-DGEN-5-43-0001-1.xlsm', data_only=True)
profiles = analyze_workbook(wb)
registry = _build_annexure_registry(wb, profiles)

# Print first few entries of each subgroup to see actual tag values
print("=== Sample registry entries ===")
for key in sorted(registry.keys())[:5]:
    entries = registry[key]
    print(f"  {key}: {[e.get('tag') for e in entries[:5]]}")

# Count ALL unique non-blank tag values in registry
all_tags_in_registry = set()
blank_in_registry = 0
for key, entries in registry.items():
    for e in entries:
        t = e.get('tag')
        if t and str(t).strip():
            all_tags_in_registry.add(str(t).strip())
        else:
            blank_in_registry += 1

print(f"\n=== Registry Totals ===")
print(f"Total entries in registry: {sum(len(v) for v in registry.values())}")
print(f"Blank/None tag entries: {blank_in_registry}")
print(f"Non-blank entries: {sum(len(v) for v in registry.values()) - blank_in_registry}")
print(f"Unique non-blank tag values: {len(all_tags_in_registry)}")
print(f"First 10 unique tags: {sorted(all_tags_in_registry)[:10]}")

# Per subgroup real vs blank
print(f"\n=== Per-subgroup breakdown ===")
for key in sorted(registry.keys()):
    entries = registry[key]
    real = [e.get('tag') for e in entries if e.get('tag') and str(e.get('tag')).strip()]
    blank = len(entries) - len(real)
    real_unique = set(str(t).strip() for t in real)
    print(f"  {key}: total={len(entries)}, real={len(real)}, blank={blank}, unique_real={len(real_unique)}")

# Run extraction
print("\n=== Extraction ===")
result = extract_workbook(wb, 'VEN-4460-DGEN-5-43-0001-1.xlsm')
rows = result['rows']
extracted = set(str(r.get('tag_no') or '').strip() for r in rows if r.get('tag_no') and str(r.get('tag_no')).strip())
hdrs = defaultdict(int)
sprs = defaultdict(int)
for r in rows:
    t = str(r.get('tag_no') or '').strip()
    if t:
        if r.get('item_num') is None:
            hdrs[t] += 1
        else:
            sprs[t] += 1
print(f"Total rows: {len(rows)}")
print(f"Unique extracted tags: {len(extracted)}")
print(f"Total header rows: {sum(hdrs.values())}, total spare rows: {sum(sprs.values())}")
print(f"Tags with 0 headers: {sum(1 for t in extracted if hdrs[t] == 0)}")
print(f"Tags with 0 spares: {sum(1 for t in extracted if sprs[t] == 0)}")

# Spare row distribution
dist = defaultdict(int)
for t in extracted:
    dist[sprs[t]] += 1
print(f"Spare count distribution: {dict(sorted(dist.items()))}")

# Tags in registry but not extracted
missing = all_tags_in_registry - extracted
extra = extracted - all_tags_in_registry
print(f"\nTags in registry but NOT extracted: {len(missing)}")
for t in sorted(missing)[:10]:
    grps = [k for k, es in registry.items() if any(str(e.get('tag') or '').strip() == t for e in es)]
    print(f"  {t!r} in groups: {grps}")
print(f"Extracted tags NOT in registry: {len(extra)}")
for t in sorted(extra)[:10]:
    print(f"  {t!r}")
