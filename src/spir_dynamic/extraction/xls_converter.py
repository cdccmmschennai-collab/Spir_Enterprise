"""
Legacy .xls (Excel 97-2003, BIFF) support.

openpyxl only reads the OOXML formats (.xlsx/.xlsm), so a .xls upload is
converted once into a temporary .xlsx and the normal pipeline opens that copy.
Extraction logic never sees the difference; .xlsx/.xlsm files never come here.

Carried over: sheet order and names, sheet visibility (hidden sheets stay
hidden), cell values with their types (text, numbers, dates in the workbook's
own 1900/1904 date system, booleans, error codes) and merged ranges.

Not carried over: formulas (xlrd exposes only the cached result — the pipeline
reads cached values anyway, data_only=True), styles, comments, images and
form-control tick boxes. SPIR TYPE detection therefore relies on the cell-based
scan for .xls files; the VML tick-box scan has nothing to read.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

import openpyxl
import structlog

from spir_dynamic.extraction.file_validator import ValidationError

log = structlog.stdlib.get_logger(__name__)

# xlrd Sheet.visibility → openpyxl sheet_state
_VISIBILITY = {0: "visible", 1: "hidden", 2: "veryHidden"}

# Largest integer a float holds exactly — beyond it keep the float as-is.
_MAX_EXACT_INT = 2 ** 53


def is_xls(filename: str) -> bool:
    """True when the upload name says legacy .xls (the validator checks the content)."""
    return Path(filename or "").suffix.lower() == ".xls"


def convert_xls_to_xlsx(source: Union[bytes, Path], out_dir: Optional[Path] = None) -> Path:
    """
    Convert a .xls workbook into a new temporary .xlsx file and return its path.

    The copy is written next to `source` when it is a Path (same partition as
    the upload), else in `out_dir` / the system temp dir. The caller deletes it.
    Raises ValidationError when the file cannot be read as an Excel 97-2003 workbook.
    """
    import xlrd  # only .xls uploads need it

    try:
        if isinstance(source, Path):
            book = xlrd.open_workbook(str(source), formatting_info=True, on_demand=True)
        else:
            book = xlrd.open_workbook(file_contents=source, formatting_info=True, on_demand=True)
    except Exception as exc:
        raise ValidationError(f"Cannot read .xls file: {exc}") from exc

    target_dir = out_dir or (source.parent if isinstance(source, Path) else None)
    fd, out_name = tempfile.mkstemp(suffix=".xlsx", prefix="xls_", dir=target_dir)
    os.close(fd)
    out_path = Path(out_name)

    try:
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        for idx in range(book.nsheets):
            sheet = book.sheet_by_index(idx)
            _copy_sheet(sheet, wb.create_sheet(title=sheet.name), book.datemode)
            book.unload_sheet(idx)
        if not wb.sheetnames:
            raise ValidationError("Excel file has no sheets.")
        wb.save(out_path)
    except ValidationError:
        out_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        out_path.unlink(missing_ok=True)
        raise ValidationError(f"Cannot convert .xls file: {exc}") from exc
    finally:
        book.release_resources()

    log.info("xls.converted", sheets=book.nsheets, out_bytes=out_path.stat().st_size)
    return out_path


def _copy_sheet(sheet, ws, datemode: int) -> None:
    ws.sheet_state = _VISIBILITY.get(sheet.visibility, "visible")

    for r in range(sheet.nrows):
        for c in range(sheet.row_len(r)):
            value = _cell_value(sheet.cell(r, c), datemode)
            if value is None:
                continue
            out = ws.cell(row=r + 1, column=c + 1, value=value)
            if isinstance(value, datetime):
                out.number_format = "yyyy-mm-dd hh:mm:ss" if value.time() != datetime.min.time() else "yyyy-mm-dd"

    # Merging after the values are written clears the non-anchor cells, which is
    # exactly what openpyxl shows for a merged range in a real .xlsx.
    for row_lo, row_hi, col_lo, col_hi in sheet.merged_cells:
        if row_hi - row_lo > 1 or col_hi - col_lo > 1:
            ws.merge_cells(
                start_row=row_lo + 1, start_column=col_lo + 1,
                end_row=row_hi, end_column=col_hi,
            )


def _cell_value(cell, datemode: int) -> Any:
    """Map an xlrd cell to the value openpyxl would read from the same cell in a .xlsx."""
    import xlrd

    ctype, value = cell.ctype, cell.value
    if ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return None
    if ctype == xlrd.XL_CELL_TEXT:
        return value if value != "" else None
    if ctype == xlrd.XL_CELL_NUMBER:
        return _number(value)
    if ctype == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate_as_datetime(value, datemode)
        except Exception:
            return _number(value)   # out-of-range serial — keep the number
    if ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(value)
    if ctype == xlrd.XL_CELL_ERROR:
        return xlrd.error_text_from_code.get(value, "#VALUE!")
    return value


def _number(value: float) -> Union[int, float]:
    # .xlsx stores whole numbers without a decimal point and openpyxl reads
    # them back as int — match that so "88496189" never becomes "88496189.0".
    if value.is_integer() and abs(value) < _MAX_EXACT_INT:
        return int(value)
    return value
