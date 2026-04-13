"""
Builds the styled output Excel workbook.

STYLING (matches original tool):
  Header: dark green #375623, white bold text, Calibri 10pt
  Data:   Calibri 10pt
  Borders: thin on all cells

TO ADD/REMOVE/REORDER COLUMNS:
  Edit extraction/output_schema.py only.
  For per-file dynamic columns, pass a DynamicSchema to build_xlsx().
"""
from __future__ import annotations

import io
import logging
import re

import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side, NamedStyle
from openpyxl.utils import get_column_letter

from spir_dynamic.extraction.output_schema import OUTPUT_COLS, COL_WIDTHS
from spir_dynamic.utils.logging import timed

log = logging.getLogger(__name__)

_HDR_BG = "375623"
_HDR_FONT_CLR = "FFFFFF"
_DATA_FONT = "Calibri"
_FONT_SIZE = 10
_HDR_HEIGHT = 30
_ROW_HEIGHT = 15
_DEFAULT_WIDTH = 14

_THIN = Side(style="thin", color="D0D0D0")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


@timed
def build_xlsx(rows: list[list], spir_no: str = "") -> bytes:
    """
    Build a styled .xlsx workbook from extracted rows (always 27 columns).
    Returns raw .xlsx bytes.
    """
    col_names = OUTPUT_COLS
    col_widths = COL_WIDTHS
    n_cols = len(col_names)

    wb = openpyxl.Workbook()
    ws = wb.active
    safe_title = re.sub(r'[\\/*?:\[\]]+', ' ', spir_no or "SPIR Extraction").strip()
    ws.title = safe_title[:31]

    # Register NamedStyles ONCE on the workbook.
    # openpyxl's per-cell style assignment (cell.font = ...) triggers
    # indexed_list.add() which hashes + mutates the stylesheet for every cell.
    # Using NamedStyle + cell.style = "name" stores only a name reference —
    # the stylesheet is updated only once per workbook instead of N×cols times.
    _hdr_style = NamedStyle(name="spir_hdr")
    _hdr_style.fill      = PatternFill("solid", fgColor=_HDR_BG)
    _hdr_style.font      = Font(name=_DATA_FONT, size=_FONT_SIZE, bold=True, color=_HDR_FONT_CLR)
    _hdr_style.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    _hdr_style.border    = _BORDER
    wb.add_named_style(_hdr_style)

    _data_style = NamedStyle(name="spir_data")
    _data_style.font      = Font(name=_DATA_FONT, size=_FONT_SIZE)
    _data_style.alignment = Alignment(vertical="center", wrap_text=False)
    _data_style.border    = _BORDER
    wb.add_named_style(_data_style)

    # Column widths
    for idx, col_name in enumerate(col_names, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = col_widths.get(col_name, _DEFAULT_WIDTH)

    # Default row height for ALL data rows — avoids creating a RowDimension
    # object per row (previously: one dict insert + object init per row).
    ws.sheet_format.defaultRowHeight = _ROW_HEIGHT
    ws.sheet_format.customHeight = True

    # Header row (explicit height override, not the default)
    ws.row_dimensions[1].height = _HDR_HEIGHT
    for col_idx, col_name in enumerate(col_names, start=1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.style = "spir_hdr"

    ws.freeze_panes = "A2"

    # Data rows — write values via append() then apply named style in one pass.
    # append() is faster than ws.cell() per-cell because it skips Cell object
    # construction overhead for the value path.
    for row in rows:
        if isinstance(row, (list, tuple)):
            r = list(row)
            if len(r) < n_cols:
                r += [None] * (n_cols - len(r))
            r = r[:n_cols]
        else:
            r = [None] * n_cols

        # Sanitize sentinel "." → None
        r = [None if v == "." else v for v in r]
        ws.append(r)

    # Apply data style to all data rows in a single worksheet scan.
    # iter_rows() returns pre-built Cell references — no repeated ws.cell() lookup.
    for ws_row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=n_cols):
        for cell in ws_row:
            cell.style = "spir_data"

    # Auto-filter
    ws.auto_filter.ref = f"A1:{get_column_letter(n_cols)}1"

    # Save to bytes
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    result = buf.read()

    log.info("Excel built: %d rows x %d cols -> %d bytes", len(rows), n_cols, len(result))
    return result
