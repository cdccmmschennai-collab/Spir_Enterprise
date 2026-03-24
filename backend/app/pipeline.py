"""
app/pipeline.py  — SPIR extraction pipeline
"""
from __future__ import annotations
import io, json, logging, uuid
from typing import Optional

import openpyxl

from app.config import get_settings
from extraction.output_schema import OUTPUT_COLS, CI, make_empty_row
from extraction.spir_detector import validate_file, ValidationError
from extraction.spir_extractor import extract_workbook
from extraction.post_processor_v2 import post_process_rows
from services.excel_builder import build_xlsx
from services.storage import InMemoryStorage

log = logging.getLogger(__name__)
cfg = get_settings()

_PROGRESS: dict[str, dict] = {}
_STORAGE: Optional[InMemoryStorage] = None

def _storage() -> InMemoryStorage:
    global _STORAGE
    if _STORAGE is None:
        _STORAGE = InMemoryStorage()
        log.info("Storage: InMemoryStorage created")
    return _STORAGE


def _dict_to_row(item: dict, is_header: bool) -> list:
    """
    Convert a spir_extractor output dict (keyed by column name)
    directly into an ordered list using the CI index map.
    """
    row = make_empty_row()
    for col_name, col_idx in CI.items():
        val = item.get(col_name)
        if val is not None and str(val).strip() not in ("", "."):
            row[col_idx] = val
    if row[CI["SPIR ERROR"]] is None:
        row[CI["SPIR ERROR"]] = 0
    return row

    if is_header:
        row[CI["EQPT QTY"]]                        = item.get("eqpt_qty")
        row[CI["QUANTITY IDENTICAL PARTS FITTED"]]  = None
    else:
        row[CI["EQPT QTY"]] = None

    if row[CI["SPIR ERROR"]] is None:
        row[CI["SPIR ERROR"]] = 0
    return row


def _execute(file_bytes: bytes, original_filename: str) -> dict:
    validate_file(original_filename, file_bytes, max_mb=cfg.max_file_size_mb)

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    try:
        result = extract_workbook(wb, spir_filename=original_filename)
    finally:
        wb.close()

    raw_rows = result.get("rows", [])
    spir_no  = result.get("spir_no", "") or ""
    log.info("Extracted: format=%s rows=%d spir_no=%r tags=%d",
             result.get("format"), len(raw_rows), spir_no,
             result.get("total_tags", 0))

    rows = [_dict_to_row(r, r.get("EQPT QTY") is not None) for r in raw_rows]
    rows = post_process_rows(rows, spir_no)

    xlsx = build_xlsx(rows, spir_no)
    if hasattr(xlsx, "read"): xlsx = xlsx.read()
    assert isinstance(xlsx, bytes), f"build_xlsx returned {type(xlsx).__name__}"

    file_id  = str(uuid.uuid4())
    safe     = (spir_no or original_filename.rsplit(".", 1)[0])
    filename = safe.replace(" ", "_").replace("/", "-") + "_Extraction.xlsx"
    _storage().put(file_id, xlsx, filename)
    log.info("Stored: file_id=%s  size=%d bytes", file_id, len(xlsx))

    preview_rows = [
        [_jsonify(cell) for cell in row]
        for row in rows[:cfg.preview_row_count]
    ]

    spare_items = sum(1 for r in raw_rows if not r.get("_is_header", False))
    tags_found  = {r.get("tag_no") for r in raw_rows if r.get("tag_no")}

    return {
        "file_id":        file_id,
        "filename":       filename,
        "status":         "done",
        "total_rows":     len(rows),
        "spare_items":    spare_items,
        "total_tags":     len(tags_found),
        "dup1_count":     0,
        "sap_count":      0,
        "annexure_count": result.get("annexure_count", 0),
        "format":         result.get("format", "UNKNOWN"),
        "spir_no":        spir_no,
        "equipment":      result.get("equipment", ""),
        "manufacturer":   result.get("manufacturer", ""),
        "supplier":       result.get("supplier", ""),
        "spir_type":      result.get("spir_type"),
        "eqpt_qty":       result.get("eqpt_qty", 0),
        "annexure_stats": result.get("annexure_stats", {}),
        "preview_cols":   OUTPUT_COLS,
        "preview_rows":   preview_rows,
    }


def _jsonify(v):
    if v is None: return None
    if isinstance(v, (int, float, bool, str)): return v
    return str(v)


def run_pipeline(file_bytes: bytes, original_filename: str) -> dict:
    log.info("Pipeline: '%s' (%.2f MB)", original_filename, len(file_bytes)/1_048_576)
    return _execute(file_bytes, original_filename)


def retrieve_result(file_id: str) -> Optional[tuple[bytes, str]]:
    return _storage().get(file_id)


def _set_progress(job_id: str, status: str, progress: int, message: str = "") -> None:
    _PROGRESS[job_id] = {"status": status, "progress": progress, "message": message}


def get_progress(job_id: str) -> Optional[dict]:
    return _PROGRESS.get(job_id)


run_extraction_pipeline = run_pipeline
get_job_progress        = get_progress
enqueue_pipeline        = run_pipeline