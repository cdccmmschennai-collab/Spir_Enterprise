"""Count unique real tags in annexure registry and compare with extraction output."""
import sys
sys.path.insert(0, 'src')
import openpyxl
from collections import defaultdict
from spir_dynamic.extraction.unified_extractor import (
    _build_annexure_registry, extract_workbook
)
from spir_dynamic.analysis.workbook_analyzer import analyze_workbook

wb = openpyxl.load_workbook(r'templates/bad working/VEN-4460-DGEN-5-43-0001-1.xlsm', data_only=True)
profiles = analyze_workbook(wb)
registry = _build_annexure_registry(wb, profiles)

# Count unique real tags per annexure family
placeholder_patterns = {'no tag', 'not applicable', 'n/a', 'tba', 'tbd', '-', 'nil', 'none', 'spare', 'no tag no'}

def is_placeholder(tag_str):
    if not tag_str:
        return True
    t = str(tag_str).strip().lower()
    if not t:
        return True
    return any(p in t for p in placeholder_patterns)

print("=== Annexure Registry Analysis ===")
all_tags_by_family = defaultdict(set)
placeholder_count_by_family = defaultdict(int)

for key, entries in sorted(registry.items()):
    fam = key.split('-')[0]  # e.g. "ANNEXURE1" from "ANNEXURE1-7"
    real_tags = []
    placeholder_count = 0
    for e in entries:
        tag = e.get('tag') or ''
        if is_placeholder(tag):
            placeholder_count += 1
        else:
            real_tags.append(tag)
            all_tags_by_family[fam].add(tag)
    placeholder_count_by_family[fam] += placeholder_count
    print(f"  {key}: {len(entries)} entries, {len(real_tags)} real, {placeholder_count} placeholder | sample real: {real_tags[:3]}")

print(f"\n=== Summary ===")
total_unique = 0
for fam, tags in sorted(all_tags_by_family.items()):
    print(f"  {fam}: {len(tags)} unique real tags, {placeholder_count_by_family[fam]} total placeholder entries")
    total_unique += len(tags)
print(f"\nTotal unique real tags across all annexure families: {total_unique}")

# Now run extraction and compare
print("\n=== Extraction Output ===")
result = extract_workbook(wb, 'VEN-4460-DGEN-5-43-0001-1.xlsm')
rows = result['rows']

tag_headers = defaultdict(int)
tag_spares = defaultdict(int)
for r in rows:
    tag = str(r.get('tag_no') or '').strip()
    if r.get('item_num') is None:
        tag_headers[tag] += 1
    else:
        tag_spares[tag] += 1

extracted_tags = set(k for k in tag_headers if k) | set(k for k in tag_spares if k)
print(f"Total rows: {len(rows)}")
print(f"Unique extracted tags: {len(extracted_tags)}")
print(f"Tags with header rows: {sum(1 for t in extracted_tags if tag_headers[t] > 0)}")
print(f"Tags without header rows: {sum(1 for t in extracted_tags if tag_headers[t] == 0)}")
print(f"Tags with spare rows: {sum(1 for t in extracted_tags if tag_spares[t] > 0)}")
print(f"Total spare rows: {sum(tag_spares.values())}")
print(f"Total header rows: {sum(tag_headers.values())}")

# Check which registry tags are NOT in the extracted output
print("\n=== Tags in registry but NOT in extraction (first 20) ===")
all_registry_tags = set()
for fam, tags in all_tags_by_family.items():
    all_registry_tags |= tags
missing = sorted(all_registry_tags - extracted_tags)
print(f"Count missing from extraction: {len(missing)}")
for t in missing[:20]:
    # Find which groups this tag is in
    grps = [k for k, es in registry.items() if any((e.get('tag') or '') == t for e in es)]
    print(f"  {t!r} -> in groups: {grps}")

# Check which extracted tags are NOT in the registry
print("\n=== Extracted tags NOT in any registry group (first 20) ===")
extra = sorted(extracted_tags - all_registry_tags - {''})
print(f"Count extra in extraction: {len(extra)}")
for t in extra[:20]:
    print(f"  {t!r} headers={tag_headers[t]} spares={tag_spares[t]}")
