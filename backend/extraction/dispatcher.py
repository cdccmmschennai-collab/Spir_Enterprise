"""
extraction/dispatcher.py
─────────────────────────
Master dispatcher — single entry point for all extraction.

ARCHITECTURE CHANGE v1 → v2:
──────────────────────────────
v1: Known ENGINE → (fallback if 0 rows) → Adaptive
v2: Known ENGINE + Adaptive → intelligent merge

The merger is sheet-aware:
  • Rows from the known engine are authoritative for their sheets.
  • Adaptive extractor fills in sheets the known engine missed.
  • No row is duplicated.

SHEET-LEVEL PROCESSING:
  Each sheet is classified independently.
  A workbook can have FORMAT3 in sheet 1 and FORMAT6 in sheet 2.
  Unknown sheets fall through to AdaptiveExtractor per-sheet.

PARALLEL PROCESSING:
  Sheets are processed concurrently via ThreadPoolExecutor.
  openpyxl read-only workbooks are safe for concurrent reads.
  MAX_SHEET_WORKERS controls parallelism (tune to server CPU count).

Public API:
    dispatch(wb, parallel=True) → dict   (same shape as old spir_dispatcher)
"""
from __future__ import annotations
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

from extraction.registry.format_registry import FormatResult, get_registry
from extraction.registry.bootstrap import bootstrap_registry

log = logging.getLogger(__name__)
MAX_SHEET_WORKERS = 4


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ci():
    from extraction.output_schema import CI
    return CI


def _empty_result() -> FormatResult:
    return FormatResult(
        format_name="UNKNOWN", spir_no="", equipment="", manufacturer="",
        supplier="", spir_type=None, eqpt_qty=0, spare_items=0, total_tags=0,
        annexure_count=0, annexure_stats={}, rows=[],
    )


def _result_to_dict(r: FormatResult, preview_count: int = 12) -> dict:
    """Convert FormatResult to the legacy dict shape routes.py expects."""
    CI = _ci()
    return {
        "format":         r.format_name,
        "spir_no":        r.spir_no,
        "equipment":      r.equipment,
        "manufacturer":   r.manufacturer,
        "supplier":       r.supplier,
        "spir_type":      r.spir_type,
        "eqpt_qty":       r.eqpt_qty,
        "spare_items":    r.spare_items,
        "total_tags":     r.total_tags,
        "annexure_count": r.annexure_count,
        "annexure_stats": r.annexure_stats,
        "rows":           r.rows,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Adaptive extractor wrapper
# ─────────────────────────────────────────────────────────────────────────────

def _run_adaptive(wb) -> Optional[FormatResult]:
    try:
        from extraction.adaptive_extractor import AdaptiveExtractor
        raw = AdaptiveExtractor(wb).extract()
        return FormatResult(
            format_name    = raw.get("format", "FORMAT_ADAPTIVE"),
            spir_no        = raw.get("spir_no", "") or "",
            equipment      = raw.get("equipment", "") or "",
            manufacturer   = raw.get("manufacturer", "") or "",
            supplier       = raw.get("supplier", "") or "",
            spir_type      = raw.get("spir_type"),
            eqpt_qty       = int(raw.get("eqpt_qty") or 0),
            spare_items    = int(raw.get("spare_items") or 0),
            total_tags     = int(raw.get("total_tags") or 0),
            annexure_count = int(raw.get("annexure_count") or 0),
            annexure_stats = raw.get("annexure_stats") or {},
            rows           = raw.get("rows") or [],
        )
    except Exception as exc:
        log.error("Adaptive extractor failed: %s", exc, exc_info=True)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Parallel adaptive extraction
# ─────────────────────────────────────────────────────────────────────────────

def _run_adaptive_parallel(wb) -> Optional[FormatResult]:
    """
    Process each data sheet in a separate thread.
    Safe: openpyxl read-only workbook, no writes during extraction.
    """
    try:
        from extraction.adaptive_extractor import AdaptiveExtractor
        from extraction.sheet_classifier import classify_workbook, SheetRole, get_extraction_plan

        profiles = classify_workbook(wb)
        data_sheets = [
            name for name, p in profiles.items()
            if p.role not in (SheetRole.VALIDATION, SheetRole.UNKNOWN)
        ]
        if not data_sheets:
            return _run_adaptive(wb)

        log.info("Parallel sheet extraction: %d sheets, %d workers",
                 len(data_sheets), min(MAX_SHEET_WORKERS, len(data_sheets)))

        # Extract global context once (fast, no per-sheet work)
        master = AdaptiveExtractor(wb)
        master._profiles = profiles
        master._plan     = get_extraction_plan(profiles)
        master._extract_global_context()
        ctx = master._global_context

        all_rows: list[list] = []
        stats:    dict[str, int] = {}

        def _extract_sheet(name: str):
            try:
                ws      = wb[name]
                profile = profiles[name]
                ex      = AdaptiveExtractor(wb)
                ex._global_context = ctx
                items   = ex._extract_data_sheet(ws, profile)
                rows    = ex._build_output_rows(items)
                return name, rows, None
            except Exception as exc:
                return name, [], exc

        workers = min(MAX_SHEET_WORKERS, len(data_sheets))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_extract_sheet, n): n for n in data_sheets}
            for fut in as_completed(futures):
                name, rows, err = fut.result()
                if err:
                    log.warning("Sheet '%s' failed: %s", name, err)
                else:
                    all_rows.extend(rows)
                    stats[name] = len(rows)

        if not all_rows:
            return None

        CI = _ci()
        tags  = {r[CI['TAG NO']]      for r in all_rows if r[CI.get('TAG NO', -1)] is not None}
        items = {r[CI['ITEM NUMBER']] for r in all_rows if r[CI.get('ITEM NUMBER', -1)] is not None}

        return FormatResult(
            format_name    = "FORMAT_ADAPTIVE_PARALLEL",
            spir_no        = ctx.get("spir_no", ""),
            equipment      = ctx.get("equipment", ""),
            manufacturer   = ctx.get("manufacturer", ""),
            supplier       = ctx.get("supplier", ""),
            spir_type      = ctx.get("spir_type"),
            eqpt_qty       = ctx.get("eqpt_qty") or len(data_sheets),
            spare_items    = len(items),
            total_tags     = len(tags),
            annexure_count = 0,
            annexure_stats = stats,
            rows           = all_rows,
        )
    except Exception as exc:
        log.error("Parallel adaptive failed: %s", exc, exc_info=True)
        return _run_adaptive(wb)


# ─────────────────────────────────────────────────────────────────────────────
# Merge logic
# ─────────────────────────────────────────────────────────────────────────────

def _merge(known: Optional[FormatResult], adaptive: Optional[FormatResult]) -> FormatResult:
    """
    Merge known engine rows + adaptive rows.
    Known rows are authoritative. Adaptive fills uncovered sheets.
    """
    if known is None and adaptive is None:
        return _empty_result()
    if known is None:
        return adaptive
    if adaptive is None:
        return known

    CI = _ci()
    sheet_ci = CI.get("SHEET")

    known_sheets = set()
    if sheet_ci is not None:
        known_sheets = {r[sheet_ci] for r in known.rows if r[sheet_ci] is not None}

    # Add adaptive rows only for sheets the known engine did NOT cover
    extra = [
        r for r in adaptive.rows
        if sheet_ci is None or r[sheet_ci] not in known_sheets
    ]

    merged_rows = known.rows + extra
    suffix      = "+ADAPTIVE" if extra else ""

    tag_ci  = CI.get("TAG NO")
    item_ci = CI.get("ITEM NUMBER")

    return FormatResult(
        format_name    = known.format_name + suffix,
        spir_no        = known.spir_no or adaptive.spir_no,
        equipment      = known.equipment or adaptive.equipment,
        manufacturer   = known.manufacturer or adaptive.manufacturer,
        supplier       = known.supplier or adaptive.supplier,
        spir_type      = known.spir_type or adaptive.spir_type,
        eqpt_qty       = known.eqpt_qty or adaptive.eqpt_qty,
        spare_items    = len({r[item_ci] for r in merged_rows
                              if item_ci is not None and r[item_ci] is not None}),
        total_tags     = len({r[tag_ci]  for r in merged_rows
                              if tag_ci  is not None and r[tag_ci]  is not None}),
        annexure_count = known.annexure_count,
        annexure_stats = {**known.annexure_stats, **adaptive.annexure_stats},
        rows           = merged_rows,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main dispatcher
# ─────────────────────────────────────────────────────────────────────────────

def dispatch(wb, parallel: bool = True) -> dict:
    """
    Main entry point. Replaces legacy spir_dispatcher().

    Runs known engine + adaptive extractor, merges results.
    Returns legacy dict shape for backward compatibility with routes.py.

    Args:
        wb:       openpyxl Workbook.
        parallel: If True, use ThreadPoolExecutor for multi-sheet workbooks.
    """
    bootstrap_registry()

    # ── 1. Known engine ───────────────────────────────────────────────────────
    known: Optional[FormatResult] = None
    try:
        known = get_registry().dispatch(wb)
    except Exception as exc:
        log.warning("Known engine raised: %s", exc)

    # ── 2. Adaptive extractor ─────────────────────────────────────────────────
    adaptive: Optional[FormatResult] = None
    try:
        n_sheets = len(wb.sheetnames)
        if parallel and n_sheets > 2:
            adaptive = _run_adaptive_parallel(wb)
        else:
            adaptive = _run_adaptive(wb)
    except Exception as exc:
        log.warning("Adaptive raised: %s", exc)

    # ── 3. Merge ──────────────────────────────────────────────────────────────
    result = _merge(known, adaptive)
    log.info("Dispatch: format=%s rows=%d tags=%d items=%d",
             result.format_name, len(result.rows), result.total_tags, result.spare_items)

    return _result_to_dict(result)


# Backward-compatible alias used by existing worker.py
spir_dispatcher = dispatch
