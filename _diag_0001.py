import sys
sys.path.insert(0, 'src')
import logging
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(name)s - %(levelname)s - %(message)s')

import openpyxl
from spir_dynamic.extraction.unified_extractor import extract_workbook
from collections import defaultdict

wb = openpyxl.load_workbook(r'templates/bad working/VEN-4460-DGEN-5-43-0001-1.xlsm', data_only=True)
result = extract_workbook(wb, 'VEN-4460-DGEN-5-43-0001-1.xlsm')
rows = result['rows']
print(f'Total rows: {len(rows)}, unique tags: {result["total_tags"]}')

tag_headers = defaultdict(int)
tag_spares = defaultdict(int)
for r in rows:
    tag = str(r.get('tag_no') or '').strip()
    if r.get('item_num') is None:
        tag_headers[tag] += 1
    else:
        tag_spares[tag] += 1

all_tags = set(list(tag_headers.keys()) + list(tag_spares.keys())) - {''}
no_hdr = [t for t in all_tags if tag_headers[t] == 0]
has_hdr = [t for t in tag_headers if tag_headers[t] > 0 and t]
multi_hdr = [t for t in tag_headers if tag_headers[t] > 1 and t]

print(f'Tags WITH header rows: {len(has_hdr)}, WITHOUT header rows: {len(no_hdr)}, multi-header: {len(multi_hdr)}')
print(f'First 5 without headers: {no_hdr[:5]}')
print(f'First 5 with headers: {has_hdr[:5]}')

# Check what tag values these no-header tags have in all_rows
print('\nSample rows for tags without headers:')
shown = 0
for r in rows:
    tag = str(r.get('tag_no') or '').strip()
    if tag in no_hdr[:3]:
        print(f'  tag={tag}, item_num={r.get("item_num")}, sheet={r.get("sheet")}, desc={str(r.get("desc") or "")[:30]}')
        shown += 1
    if shown >= 10:
        break

# Show annexure registry info
print('\nSheet profiles:')
for p in result['sheet_profiles']:
    print(f'  {p["name"]} role={p["role"]} layout={p["tag_layout"]} header_row={p["header_row"]}')
