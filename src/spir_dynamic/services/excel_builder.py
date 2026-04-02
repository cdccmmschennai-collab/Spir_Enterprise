"""
Builds the styled output Excel workbook.

STYLING (matches original tool):
  Header: dark green #375623, white bold text, Calibri 10pt
  Data:   Calibri 10pt
  Borders: thin on all cells

TO ADD/REMOVE/REORDER COLUMNS:
  Edit extraction/output_schema.py only.
"""
from __future__ import annotations

import io
import logging

import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from spir_dynamic.extraction.output_schema import OUTPUT_COLS, COL_WIDTHS

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


def build_xlsx(rows: list[list], spir_no: str = "") -> bytes:
    """
    Build a styled .xlsx workbook from extracted rows.
    Returns raw .xlsx bytes.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    # Sanitize sheet title — Excel forbids: \ / * ? : [ ]
    import re
    safe_title = re.sub(r'[\\/*?:\[\]]+', ' ', spir_no or "SPIR Extraction").strip()
    ws.title = safe_title[:31]

    hdr_fill = PatternFill("solid", fgColor=_HDR_BG)
    hdr_font = Font(name=_DATA_FONT, size=_FONT_SIZE, bold=True, color=_HDR_FONT_CLR)
    hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    data_font = Font(name=_DATA_FONT, size=_FONT_SIZE)
    data_align = Alignment(vertical="center", wrap_text=False)

    # Column widths
    for idx, col_name in enumerate(OUTPUT_COLS, start=1):
        width = COL_WIDTHS.get(col_name, _DEFAULT_WIDTH)
        ws.column_dimensions[get_column_letter(idx)].width = width

    # Header row
    ws.row_dimensions[1].height = _HDR_HEIGHT
    for col_idx, col_name in enumerate(OUTPUT_COLS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = hdr_align
        cell.border = _BORDER

    ws.freeze_panes = "A2"

    # Data rows
    n_cols = len(OUTPUT_COLS)
    for row_idx, row in enumerate(rows, start=2):
        ws.row_dimensions[row_idx].height = _ROW_HEIGHT

        if isinstance(row, (list, tuple)):
            r = list(row)
            if len(r) < n_cols:
                r += [None] * (n_cols - len(r))
            r = r[:n_cols]
        else:
            r = [None] * n_cols

        for col_idx, value in enumerate(r, start=1):
            if value == ".":
                value = None
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.font = data_font
            cell.alignment = data_align
            cell.border = _BORDER

    # Auto-filter
    last_col = get_column_letter(len(OUTPUT_COLS))
    ws.auto_filter.ref = f"A1:{last_col}1"

    # Save to bytes
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    result = buf.read()

    log.info("Excel built: %d rows x %d cols -> %d bytes", len(rows), len(OUTPUT_COLS), len(result))
    return result
