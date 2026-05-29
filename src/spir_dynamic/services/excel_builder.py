"""
Builds the styled output Excel workbook.

OUTPUT STRUCTURE (4-row header block):
  Row 1: Display headers     — bright green  #00B050, white bold
  Row 2: SPIR internal fields — navy blue    #002060, white bold
  Row 3: Character limits    — dark red      #C00000, white regular
  Row 4+: Extracted data rows (S.NO 0001, 0002 …)

TO ADD/REMOVE/REORDER DATA COLUMNS:
  Edit extraction/output_schema.py only.
  Metadata (SPIR field names, char limits) lives in services/spir_metadata.py.
"""
from __future__ import annotations

import io
import re

import structlog
import openpyxl
from openpyxl.styles import NamedStyle, PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from spir_dynamic.extraction.output_schema import OUTPUT_COLS, COL_WIDTHS
from spir_dynamic.services.spir_metadata import COLUMN_METADATA
from spir_dynamic.utils.logging import timed

log = structlog.stdlib.get_logger(__name__)

# ── Presentation colours (match QatarEnergy SPIR template) ────────────────────
_HDR_BG       = "00B050"   # Row 1 display headers: bright green
_META1_BG     = "002060"   # Row 2 SPIR field names: dark navy blue
_META2_BG     = "C00000"   # Row 3 character limits: dark red
_HDR_FONT_CLR = "FFFFFF"   # White text for all three header/meta rows

_DATA_FONT_NAME = "Calibri"
_FONT_SIZE      = 11
_HDR_HEIGHT     = 30
_ROW_HEIGHT     = 15
_DEFAULT_WIDTH  = 14
_SNO_WIDTH      = 5.43     # S.NO column width (matches template)

_THIN   = Side(style="thin", color="D0D0D0")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _register_styles(wb: openpyxl.Workbook) -> None:
    """Register the three header/metadata row NamedStyles once per workbook."""
    defs = [
        ("spir_hdr",   _HDR_BG,   True),
        ("spir_meta1", _META1_BG, True),
        ("spir_meta2", _META2_BG, False),
    ]
    for name, bg, bold in defs:
        if name not in wb.named_styles:
            s            = NamedStyle(name=name)
            s.fill       = PatternFill("solid", fgColor=bg)
            s.font       = Font(name=_DATA_FONT_NAME, size=_FONT_SIZE,
                                bold=bold, color=_HDR_FONT_CLR)
            s.alignment  = Alignment(horizontal="center", vertical="center",
                                     wrap_text=True)
            s.border     = _BORDER
            wb.add_named_style(s)


@timed
def build_xlsx(rows: list[list], spir_no: str = "") -> bytes:
    """
    Build a styled .xlsx workbook from extracted rows.

    Row 1 = display headers (S.NO + all OUTPUT_COLS)
    Row 2 = SPIR internal field names
    Row 3 = character limit metadata (visual / spec reference only)
    Row 4+ = extracted data; S.NO is zero-padded (0001, 0002 …)

    Extraction logic is untouched — only the presentation layer changes here.
    Returns raw .xlsx bytes.
    """
    col_names   = OUTPUT_COLS
    n_data_cols = len(col_names)
    n_cols      = n_data_cols + 1          # +1 for S.NO in column A

    wb = openpyxl.Workbook()
    ws = wb.active
    safe_title = re.sub(r'[\\/*?:\[\]]+', ' ', spir_no or "SPIR Extraction").strip()
    ws.title = safe_title[:31]

    _register_styles(wb)

    # ── Column widths ─────────────────────────────────────────────────────────
    ws.column_dimensions["A"].width = _SNO_WIDTH
    for idx, col_name in enumerate(col_names, start=2):
        ws.column_dimensions[get_column_letter(idx)].width = COL_WIDTHS.get(
            col_name, _DEFAULT_WIDTH
        )

    # ── Row heights for the three header rows ─────────────────────────────────
    ws.sheet_format.defaultRowHeight = _ROW_HEIGHT
    ws.sheet_format.customHeight     = True
    for r in (1, 2, 3):
        ws.row_dimensions[r].height = _HDR_HEIGHT

    # ── Row 1: display headers ────────────────────────────────────────────────
    ws.cell(row=1, column=1, value="S.NO").style = "spir_hdr"
    for col_idx, col_name in enumerate(col_names, start=2):
        ws.cell(row=1, column=col_idx, value=col_name).style = "spir_hdr"

    # ── Row 2: SPIR internal field names ──────────────────────────────────────
    ws.cell(row=2, column=1, value="NA").style = "spir_meta1"
    for col_idx, col_name in enumerate(col_names, start=2):
        spir_field = COLUMN_METADATA.get(col_name, {}).get("spir_field", "NA")
        ws.cell(row=2, column=col_idx, value=spir_field).style = "spir_meta1"

    # ── Row 3: character limit metadata (display/spec only, not DB limits) ────
    ws.cell(row=3, column=1, value=4).style = "spir_meta2"
    for col_idx, col_name in enumerate(col_names, start=2):
        limit = COLUMN_METADATA.get(col_name, {}).get("display_limit")
        ws.cell(row=3, column=col_idx, value=limit).style = "spir_meta2"

    # ── Freeze the three header rows ──────────────────────────────────────────
    ws.freeze_panes = "A4"

    # ── Auto-filter on the display header row ─────────────────────────────────
    ws.auto_filter.ref = f"A1:{get_column_letter(n_cols)}1"

    # ── Data rows (Row 4+) ────────────────────────────────────────────────────
    # ws.max_row is 3 after writing the header block, so ws.append() starts at 4.
    for idx, row in enumerate(rows, start=1):
        s_no = str(idx).zfill(4)

        if isinstance(row, (list, tuple)):
            r = list(row)
            if len(r) < n_data_cols:
                r += [None] * (n_data_cols - len(r))
            r = r[:n_data_cols]
        else:
            r = [None] * n_data_cols

        # Sanitize sentinel "." → None; uppercase all string values
        r = [None if v == "." else v for v in r]
        r = [v.upper() if isinstance(v, str) else v for v in r]

        ws.append([s_no] + r)

    # ── Serialise ─────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    result = buf.read()

    log.info("excel.built", rows=len(rows), cols=n_cols, bytes=len(result))
    return result
