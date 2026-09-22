"""
Main extraction pipeline — orchestrates validation, extraction,
post-processing, and output building.
"""
from __future__ import annotations

import cProfile
import gc
import io
import pstats
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional, Union

import openpyxl
import structlog

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
from spir_dynamic.services.currency_service import (
    CurrencyConversionError,
    CurrencyRateService,
    RateSnapshot,
    get_currency_rate_service,
    normalize_currency_code,
)
from spir_dynamic.services.storage import get_storage
from spir_dynamic.app.config import get_settings
from spir_dynamic.utils.logging import timed
from spir_dynamic.monitoring.metrics import (
    PROCESSING_DURATION,
    EXTRACTION_ROWS,
    WORKBOOK_OPEN_DURATION,
)

log = structlog.stdlib.get_logger(__name__)

_SLOW_EXTRACTION_WARN_SECONDS = 120


@timed
def run_pipeline(file_input: Union[bytes, Path], original_filename: str) -> dict[str, Any]:
    """
    Full extraction pipeline: validate -> extract -> post-process -> build xlsx.

    Accepts either raw bytes (legacy/Celery path) or a Path to a temp file on disk
    (new streaming path). When given a Path, the workbook is opened directly from
    disk — no raw-bytes copy is held in memory during extraction.

    Returns a metadata dict with file_id, preview_rows, statistics, etc.
    """
    # ── cProfile — opt-in via SPIR_PROFILE=true (disabled in production) ────
    import os as _os
    _profile_enabled = _os.environ.get("SPIR_PROFILE", "").lower() in ("1", "true", "yes")
    _profiler = None
    if _profile_enabled:
        _profiler = cProfile.Profile()
        _profiler.enable()
    # ────────────────────────────────────────────────────────────────────────

    _pipeline_start = time.time()
    is_path = isinstance(file_input, Path)

    try:
        if is_path:
            size_mb = file_input.stat().st_size / (1024 * 1024)
        else:
            size_mb = len(file_input) / (1024 * 1024)

        log.info("pipeline.start", filename=original_filename, size_mb=round(size_mb, 1))
        if size_mb > 500:
            log.warning("pipeline.large_file", filename=original_filename, size_mb=round(size_mb, 1))

        cfg = get_settings()

        # Step 1: Validate
        validate_file(original_filename, file_input, cfg.max_file_size_mb)

        # Step 2: Load workbook (not read_only — merged_cells.ranges used in column_mapper)
        # keep_links=False skips external link parsing, saving ~0.1s on link-heavy files.
        _wb_start = time.perf_counter()
        if is_path:
            # Open directly from disk — no raw-bytes copy in RAM.
            wb = openpyxl.load_workbook(str(file_input), data_only=True, keep_links=False)
            wb._spir_raw_bytes = None
            # VML form-control detection re-opens the ZIP from the temp-file path.
            wb._spir_raw_path = str(file_input)
        else:
            # Bytes path — original behavior (Celery workers use this).
            wb = openpyxl.load_workbook(
                io.BytesIO(file_input), data_only=True, keep_links=False
            )
            wb._spir_raw_bytes = file_input
            wb._spir_raw_path = None

        _wb_dur = time.perf_counter() - _wb_start
        WORKBOOK_OPEN_DURATION.observe(_wb_dur)
        log.debug("workbook.loaded", duration_s=round(_wb_dur, 2), filename=original_filename)

        _extract_start = time.perf_counter()
        try:
            # Step 3: Extract
            result = extract_workbook(wb, original_filename)
        finally:
            wb.close()
            # Explicitly release the parsed workbook for large files to reclaim RAM
            # before building the output Excel (which also needs memory).
            if size_mb > 100:
                del wb
                gc.collect()

        _extract_dur = time.perf_counter() - _extract_start
        if _extract_dur > _SLOW_EXTRACTION_WARN_SECONDS:
            log.warning("pipeline.slow", duration_s=round(_extract_dur, 1), filename=original_filename, size_mb=round(size_mb, 1))

        raw_rows = result.get("rows", [])
        spir_no = result.get("spir_no", "")

        # Step 4: Convert dicts to the standard 27-column output schema
        output_rows = [row_from_dict(r) for r in raw_rows]

        # Step 4b: Ensure SPIR NO on all rows
        spir_col = CI.get("SPIR NO", 0)
        for row in output_rows:
            if spir_col < len(row) and row[spir_col] is None and spir_no:
                row[spir_col] = spir_no

        # The result's file_id doubles as the processing-job id the currency
        # snapshot is recorded against (extraction_history stores both).
        file_id = str(uuid.uuid4())

        # Step 5: Currency conversion — one frozen rate snapshot per job.
        currency_snapshot = _apply_currency_conversion(output_rows, job_id=file_id)

        # Step 6: Post-process (position numbers + SPF numbers)
        # Pass ordered sheet_profiles so SheetTracker can pre-map every sheet
        # (including continuations) to the correct OMN index before processing rows.
        sheet_profiles = result.get("sheet_profiles", [])
        output_rows = post_process_rows(output_rows, spir_no, sheet_profiles=sheet_profiles)

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

        # Step 7c: Uppercase all string values
        for row in output_rows:
            for i, v in enumerate(row):
                if isinstance(v, str):
                    row[i] = v.upper()

        # Step 8: Build styled Excel (always 27 columns)
        xlsx_bytes = build_xlsx(output_rows, spir_no)

        # Step 9: Store result
        # Use the original input filename (stem only) as the download name so that
        # the user sees their own file naming convention in the frontend.
        safe_stem = re.sub(r'[\r\n\t/\\:*?"<>|]+', ' ', Path(original_filename).stem).strip()
        out_filename = f"{safe_stem}_Extraction.xlsx"
        get_storage().put(file_id, xlsx_bytes, out_filename)

        # Step 10: Build response — full dataset, no row limit
        preview_rows = [
            [_jsonify(v) for v in row] for row in output_rows
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
            # Rate snapshot this job converted with (None = no priced rows).
            "currency_rates": currency_snapshot.to_dict() if currency_snapshot else None,
        }

        try:
            import resource as _resource
            _rss_kb = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
            _mem_mb: object = round(_rss_kb / 1024, 1)  # Linux: KB → MB
        except Exception:
            _mem_mb = "N/A"

        log.info(
            "pipeline.done",
            rows=len(output_rows),
            tags=result.get("total_tags", 0),
            format=result.get("format"),
            mem_rss_mb=_mem_mb,
        )

        EXTRACTION_ROWS.inc(len(output_rows))
        return response

    finally:
        # ── cProfile report (remove after identifying bottlenecks) ──────────────
        if _profiler is not None:
            _profiler.disable()
            _s = io.StringIO()
            pstats.Stats(_profiler, stream=_s).sort_stats("cumulative").print_stats(30)
            log.info("pipeline.profile", stats=_s.getvalue())
        # ────────────────────────────────────────────────────────────────────────
        PROCESSING_DURATION.observe(time.time() - _pipeline_start)


def retrieve_result(file_id: str) -> Optional[tuple[bytes, str]]:
    """Retrieve stored extraction result."""
    return get_storage().get(file_id)


def _apply_currency_conversion(
    rows: list[list],
    job_id: Optional[str] = None,
    service: Optional[CurrencyRateService] = None,
) -> Optional[RateSnapshot]:
    """
    Convert UNIT PRICE to UNIT PRICE (QAR) using one frozen rate snapshot.

    1. Scan the rows once and collect the ISO codes that actually need a rate
       (rows with a currency AND a price; QAR itself needs none).
    2. Ask the CurrencyRateService for those rates ONCE (build_snapshot). Each
       rate comes from the live API, else the persisted last-success store,
       else the static table — the snapshot says which.
    3. Convert every row from the snapshot — the provider is never touched
       again for this job, so a mid-job upstream change cannot leak in.

    Rows whose currency is unrecognised or rejected by the provider as invalid
    keep an empty QAR cell, as they always did. A currency that needs a rate
    but has none anywhere fails the job (CurrencyConversionError) — blank or
    invented financial values are never produced for that reason. Returns the
    snapshot (None when no row needed conversion) so the caller can persist it.
    """
    currency_col = CI.get("CURRENCY")
    price_col = CI.get("UNIT PRICE")
    qar_col = CI.get("UNIT PRICE (QAR)")

    if currency_col is None or price_col is None or qar_col is None:
        return None

    min_col = max(currency_col, price_col, qar_col)

    # Pass 1 — which currencies does this file need?
    needed: set[str] = set()
    unrecognized: set[str] = set()
    priced_rows = 0
    for row in rows:
        if len(row) <= min_col:
            continue
        currency = row[currency_col]
        price = row[price_col]
        if not currency or price is None:
            continue
        priced_rows += 1
        code = normalize_currency_code(currency)
        if code:
            needed.add(code)
        elif len(unrecognized) < 10:
            unrecognized.add(str(currency).strip().upper()[:12])

    if priced_rows == 0:
        return None

    # Pass 2 — one lookup per currency, frozen for the whole job.
    svc = service or get_currency_rate_service()
    snapshot = svc.build_snapshot(needed, job_id=job_id, unrecognized=unrecognized)

    if snapshot.unavailable:
        # Live API down AND nothing in the fallback chain for these codes.
        # Fail loudly: a blank or made-up QAR column would be wrong financial
        # data. The sync route reports this message; Celery retries with backoff.
        details = "; ".join(e.error or e.source_currency for e in snapshot.unavailable)
        codes = ", ".join(e.source_currency for e in snapshot.unavailable)
        log.error("currency.conversion_failed", job_id=job_id, currencies=codes, details=details)
        raise CurrencyConversionError(
            f"Currency conversion failed — no exchange rate available for {codes} -> "
            f"{snapshot.target_currency}. The live rate service could not be reached and no "
            f"previously successful or fallback rate exists for this currency. Retry once the "
            f"rate service is reachable, or process a file that already has QAR prices. "
            f"Details: {details}"
        )

    # Pass 3 — convert from the snapshot only (same arithmetic/rounding as before).
    for row in rows:
        if len(row) <= min_col:
            continue
        currency = row[currency_col]
        price = row[price_col]
        if not currency or price is None:
            continue
        try:
            code = normalize_currency_code(currency)
            if not code:
                continue
            flt_price = float(price)
            if code == snapshot.target_currency:
                row[qar_col] = round(flt_price, 2)
            else:
                rate = snapshot.rate_for(code)
                if rate is not None:
                    row[qar_col] = round(flt_price * rate, 2)
        except (ValueError, TypeError):
            pass

    return snapshot


def _jsonify(v: Any) -> Any:
    """Convert a cell value to JSON-safe type."""
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)
