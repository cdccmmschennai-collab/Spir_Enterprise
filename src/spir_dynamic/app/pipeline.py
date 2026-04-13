"""
Main extraction pipeline — orchestrates validation, extraction,
post-processing, and output building.
"""
from __future__ import annotations

import cProfile
import io
import logging
import pstats
import re
import uuid
from typing import Any, Optional

import openpyxl

from spir_dynamic.extraction.file_validator import validate_file
from spir_dynamic.extraction.unified_extractor import extract_workbook
from spir_dynamic.extraction.output_schema import (
    CI,
    OUTPUT_COLS,
    make_empty_row,
    row_from_dict,
)
from spir_dynamic.extraction.post_processor import post_process_rows
from spir_dynamic.services.excel_builder import build_xlsx
from spir_dynamic.services.duplicate_checker import deduplicate_rows, analyse_duplicates
from spir_dynamic.services.currency_service import get_rates_to_qar, _extract_code
from spir_dynamic.services.storage import get_storage
from spir_dynamic.app.config import get_settings
from spir_dynamic.utils.logging import timed

log = logging.getLogger(__name__)


@timed
def run_pipeline(file_bytes: bytes, original_filename: str) -> dict[str, Any]:
    """
    Full extraction pipeline: validate -> extract -> post-process -> build xlsx.

    Returns a metadata dict with file_id, preview_rows, statistics, etc.
    """
    # ── cProfile (remove after identifying bottlenecks) ─────────────────────
    _profiler = cProfile.Profile()
    _profiler.enable()
    # ────────────────────────────────────────────────────────────────────────

    size_mb = len(file_bytes) / (1024 * 1024)
    log.info("Pipeline start: %s (%.1f MB)", original_filename, size_mb)

    cfg = get_settings()

    # Step 1: Validate
    validate_file(original_filename, file_bytes, cfg.max_file_size_mb)

    # Step 2: Load workbook (not read_only — need merged_cells support)
    wb = openpyxl.load_workbook(
        io.BytesIO(file_bytes), data_only=True
    )

    try:
        # Step 3: Extract
        result = extract_workbook(wb, original_filename)
    finally:
        wb.close()

    raw_rows = result.get("rows", [])
    spir_no = result.get("spir_no", "")

    # Step 4: Convert dicts to the standard 27-column output schema
    output_rows = [row_from_dict(r) for r in raw_rows]

    # Step 4b: Ensure SPIR NO on all rows
    spir_col = CI.get("SPIR NO", 0)
    for row in output_rows:
        if spir_col < len(row) and row[spir_col] is None and spir_no:
            row[spir_col] = spir_no

    # Step 5: Currency conversion
    _apply_currency_conversion(output_rows)

    # Step 6: Post-process (position numbers + SPF numbers)
    # Extract main (data) sheet names — only these increment the OMN line prefix.
    # Continuation/annexure sheets inherit the prefix of their parent main sheet.
    main_sheet_names: set[str] = set()
    for sp in result.get("sheet_profiles", []):
        if sp.get("role") == "data":
            main_sheet_names.add(sp["name"])
    output_rows = post_process_rows(output_rows, spir_no, main_sheet_names)

    # Step 7: Deduplicate
    output_rows = deduplicate_rows(output_rows, CI)
    dup_info = analyse_duplicates(output_rows)

    # Step 7b: Ensure ERROR = 0 on all rows after duplicate analysis.
    error_col = CI.get("ERROR")
    if error_col is not None:
        for row in output_rows:
            if error_col < len(row):
                val = row[error_col]
                if val is None or val == "":
                    row[error_col] = 0

    # Step 8: Build styled Excel (always 27 columns)
    xlsx_bytes = build_xlsx(output_rows, spir_no)

    # Step 9: Store result
    file_id = str(uuid.uuid4())
    # Sanitize filename — remove chars invalid in filenames and HTTP headers
    safe_spir = re.sub(r'[\r\n\t/\\:*?"<>|]+', ' ', spir_no).strip() if spir_no else ""
    out_filename = f"{safe_spir}_Extraction.xlsx" if safe_spir else f"{original_filename}_Extraction.xlsx"
    get_storage().put(file_id, xlsx_bytes, out_filename)

    # Step 10: Build response
    preview_count = cfg.preview_row_count
    preview_rows = [
        [_jsonify(v) for v in row] for row in output_rows[:preview_count]
    ]

    response = {
        "status": "done",
        "file_id": file_id,
        "filename": out_filename,
        "format": result.get("format", "UNKNOWN"),
        "spir_no": spir_no,
        "equipment": result.get("equipment", ""),
        "manufacturer": result.get("manufacturer", ""),
        "supplier": result.get("supplier", ""),
        "spir_type": result.get("spir_type"),
        "eqpt_qty": result.get("eqpt_qty", 0),
        "spare_items": result.get("spare_items", 0),
        "total_tags": result.get("total_tags", 0),
        "annexure_count": result.get("annexure_count", 0),
        "total_rows": len(output_rows),
        "dup1_count": dup_info.get("dup1_count", 0),
        "sap_count": dup_info.get("sap_count", 0),
        "preview_cols": OUTPUT_COLS,
        "preview_rows": preview_rows,
        "sheet_profiles": result.get("sheet_profiles", []),
    }

    log.info(
        "Pipeline done: %d rows, %d tags, format=%s",
        len(output_rows), result.get("total_tags", 0), result.get("format"),
    )

    # ── cProfile report (remove after identifying bottlenecks) ──────────────
    if _profiler is not None:
        _profiler.disable()
        _s = io.StringIO()
        pstats.Stats(_profiler, stream=_s).sort_stats("cumulative").print_stats(30)
        log.info("[PROFILE] top 30 by cumulative time:\n%s", _s.getvalue())
    # ────────────────────────────────────────────────────────────────────────

    return response


def retrieve_result(file_id: str) -> Optional[tuple[bytes, str]]:
    """Retrieve stored extraction result."""
    return get_storage().get(file_id)


def _apply_currency_conversion(rows: list[list]) -> None:
    """Convert unit_price to QAR where currency is available."""
    currency_col = CI.get("CURRENCY")
    price_col = CI.get("UNIT PRICE")
    qar_col = CI.get("UNIT PRICE (QAR)")

    if currency_col is None or price_col is None or qar_col is None:
        return

    # Fetch rate table ONCE for the entire batch.
    # Previously to_qar() was called per-row, each invoking get_rates_to_qar()
    # which — when the network is unavailable — retried all 3 APIs on every
    # call (no fallback caching), producing ~477 HTTP round-trips per file.
    rates = get_rates_to_qar()
    min_col = max(currency_col, price_col, qar_col)

    for row in rows:
        if len(row) <= min_col:
            continue

        currency = row[currency_col]
        price    = row[price_col]

        if not currency or price is None:
            continue

        try:
            code = _extract_code(str(currency))
            if not code:
                continue
            flt_price = float(price)
            if code == "QAR":
                row[qar_col] = round(flt_price, 2)
            else:
                rate = rates.get(code)
                if rate is not None:
                    row[qar_col] = round(flt_price * rate, 2)
        except (ValueError, TypeError):
            pass


def _jsonify(v: Any) -> Any:
    """Convert a cell value to JSON-safe type."""
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)
