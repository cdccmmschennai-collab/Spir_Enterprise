"""
app/pipeline.py
────────────────
Full extraction pipeline — 9 steps, inline (no Celery/Redis needed).

Steps:
  1. validate_file
  2. detect format (FORMAT8 → FORMAT7 → ... → adaptive)
  3. parse rows (tag splitting inside each parser)
  4. normalize to output schema
  5. deduplicate by (tag + description)
  6. currency conversion → INR
  7. build Excel output
  8. store result
  9. return metadata dict
"""
from __future__ import annotations
import io
import json
import logging
import uuid
from typing import Optional

import openpyxl

from app.config import get_settings
from extraction.output_schema import OUTPUT_COLS, CI, row_from_dict
from extraction.spir_detector import validate_file, ValidationError
from extraction.formats import get_all_parsers
from services.currency_service import convert_to_inr
from services.duplicate_checker import deduplicate_rows, analyse_duplicates
from services.excel_builder import build_xlsx
from services.storage import get_storage

log = logging.getLogger(__name__)
cfg = get_settings()


# ─────────────────────────────────────────────────────────────────────────────
# Step 2+3: detect format and parse
# ─────────────────────────────────────────────────────────────────────────────

def _detect_and_parse(wb) -> tuple[str, list[dict]]:
    """Try each parser in priority order. AdaptiveParser always last."""
    from extraction.formats.adaptive import AdaptiveParser
    for parser_cls in get_all_parsers():
        if parser_cls is AdaptiveParser:
            continue
        try:
            if parser_cls.detect(wb):
                rows = parser_cls.parse(wb)
                if rows:
                    log.info("Format: %s  rows=%d", parser_cls.FORMAT_NAME, len(rows))
                    return parser_cls.FORMAT_NAME, rows
        except Exception as exc:
            log.debug("%s failed: %s", parser_cls.FORMAT_NAME, exc)
    rows = AdaptiveParser.parse(wb)
    log.info("Format: FORMAT_ADAPTIVE  rows=%d", len(rows))
    return "FORMAT_ADAPTIVE", rows


# ─────────────────────────────────────────────────────────────────────────────
# Step 4: normalize
# ─────────────────────────────────────────────────────────────────────────────

def _normalize(raw_rows: list[dict]) -> list[list]:
    return [row_from_dict(r) for r in raw_rows]


# ─────────────────────────────────────────────────────────────────────────────
# Step 5: deduplicate (done in services/duplicate_checker.py)
# Step 6: currency → INR
# ─────────────────────────────────────────────────────────────────────────────

def _apply_currency(rows: list[list]) -> list[list]:
    price_col = CI.get("unit_price")
    curr_col  = CI.get("currency")
    inr_col   = CI.get("unit_price_inr")
    total_col = CI.get("total_price")
    qty_col   = CI.get("quantity")

    if price_col is None or inr_col is None:
        return rows

    for row in rows:
        price = row[price_col] if price_col < len(row) else None
        curr  = row[curr_col]  if curr_col  is not None and curr_col  < len(row) else None
        qty   = row[qty_col]   if qty_col   is not None and qty_col   < len(row) else None

        inr = convert_to_inr(price, curr)
        if inr_col < len(row):
            row[inr_col] = inr

        # Compute total if not already set
        if total_col is not None and total_col < len(row):
            if row[total_col] is None and inr is not None and qty is not None:
                try:
                    row[total_col] = round(float(qty) * inr, 2)
                except Exception:
                    pass
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Metadata helper
# ─────────────────────────────────────────────────────────────────────────────

def _metadata(raw_rows: list[dict], fmt: str) -> dict:
    import re
    tags   = {r.get("tag") for r in raw_rows if r.get("tag")}
    descs  = {r.get("description") for r in raw_rows if r.get("description")}
    sheets: dict[str, int] = {}
    for r in raw_rows:
        s = r.get("sheet") or "MAIN"
        sheets[s] = sheets.get(s, 0) + 1

    spir_re = re.compile(r'[A-Z]{2,5}-\d{4}-\S+', re.IGNORECASE)
    spir_no = ""
    for r in raw_rows[:20]:
        for fld in ("sheet", "tag", "description"):
            m = spir_re.search(str(r.get(fld) or ""))
            if m:
                spir_no = m.group(0)
                break
        if spir_no:
            break

    mfr_counts: dict[str, int] = {}
    for r in raw_rows:
        v = str(r.get("manufacturer") or "").strip()
        if v:
            mfr_counts[v] = mfr_counts.get(v, 0) + 1

    return {
        "format":         fmt,
        "spir_no":        spir_no,
        "equipment":      "",
        "manufacturer":   max(mfr_counts, key=mfr_counts.get, default=""),
        "supplier":       "",
        "spir_type":      None,
        "eqpt_qty":       len(tags),
        "spare_items":    len(descs),
        "total_tags":     len(tags),
        "annexure_count": max(0, len(sheets) - 1),
        "annexure_stats": sheets,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Core execution — the 9 steps
# ─────────────────────────────────────────────────────────────────────────────

def _execute(file_bytes: bytes, original_filename: str) -> dict:
    # 1. Validate
    validate_file(original_filename, file_bytes, max_mb=cfg.max_file_size_mb)

    # 2+3. Detect + Parse (tag splitting is inside each parser)
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    fmt, raw_rows = _detect_and_parse(wb)
    wb.close()

    log.info("Parsed %d rows, format=%s", len(raw_rows), fmt)

    # 4. Normalize to output schema
    rows = _normalize(raw_rows)

    # 5. Metadata (before dedup changes row count)
    meta = _metadata(raw_rows, fmt)

    # 6. Deduplicate
    rows = deduplicate_rows(rows, CI)
    dups = analyse_duplicates(rows)

    # 7. Currency → INR
    rows = _apply_currency(rows)

    # 8. Build Excel
    xlsx = build_xlsx(rows, meta.get("spir_no", ""))

    # 9. Store
    file_id  = str(uuid.uuid4())
    spir_no  = meta.get("spir_no") or original_filename.rsplit(".", 1)[0]
    filename = spir_no.replace(" ", "_").replace("/", "-") + "_Extraction.xlsx"

    try:
        get_storage().put(file_id, xlsx, filename)
    except Exception as exc:
        log.error("Storage.put failed: %s", exc)

    return {
        **meta,
        "dup1_count":   dups["dup1_count"],
        "sap_count":    dups["sap_count"],
        "total_rows":   len(rows),
        "preview_cols": OUTPUT_COLS,
        "preview_rows": rows[:cfg.preview_row_count],
        "file_id":      file_id,
        "filename":     filename,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def run_pipeline(file_bytes: bytes, original_filename: str) -> dict:
    log.info("Pipeline: '%s' (%.2f MB)", original_filename, len(file_bytes) / 1_048_576)
    return _execute(file_bytes, original_filename)


# ─────────────────────────────────────────────────────────────────────────────
# Progress tracking (in-memory fallback when Redis not running)
# ─────────────────────────────────────────────────────────────────────────────

_PROGRESS: dict[str, dict] = {}   # in-memory fallback


def _set_progress(job_id: str, status: str, progress: int, message: str = "") -> None:
    payload = {"status": status, "progress": progress, "message": message}
    _PROGRESS[job_id] = payload
    try:
        import redis as _r
        r = _r.from_url(cfg.redis_url, decode_responses=True,
                        socket_connect_timeout=1, socket_timeout=1)
        r.setex(f"spir:progress:{job_id}", cfg.result_ttl_seconds,
                json.dumps(payload))
    except Exception:
        pass   # Redis not running — in-memory is fine


def get_progress(job_id: str) -> Optional[dict]:
    # Check in-memory first
    if job_id in _PROGRESS:
        return _PROGRESS[job_id]
    # Try Redis
    try:
        import redis as _r
        r = _r.from_url(cfg.redis_url, decode_responses=True,
                        socket_connect_timeout=1, socket_timeout=1)
        raw = r.get(f"spir:progress:{job_id}")
        return json.loads(raw) if raw else None
    except Exception:
        return None


def retrieve_result(file_id: str):
    return get_storage().get(file_id)


# Backward-compat aliases
run_extraction_pipeline = run_pipeline
get_job_progress        = get_progress
enqueue_pipeline        = run_pipeline  # no Celery needed for now
