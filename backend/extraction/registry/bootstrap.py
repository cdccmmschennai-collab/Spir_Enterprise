"""
extraction/registry/bootstrap.py
──────────────────────────────────
Wires all known formats (FORMAT1-FORMAT8) into the global registry.
Uses a bridge adapter so the old spir_engine.py API works unchanged.

Call bootstrap_registry() once at application startup (app/main.py).
It is idempotent — safe to call multiple times.

ADDING FORMAT9:
    Add one line to REGISTRY_CONFIG below:
        ("FORMAT9", 15, "My new format description")
    That's it.
"""
from __future__ import annotations
import logging
from extraction.registry.format_registry import (
    FormatEntry, FormatResult, register, get_registry,
)

log = logging.getLogger(__name__)
_done = False

# ── Config: (format_name, priority, description) ──────────────────────────────
# Lower priority = checked FIRST. Order matters: most specific first.
REGISTRY_CONFIG = [
    ("FORMAT8",  10, "Categorised numbered sheets — MAIN SHEET N (EQUIPMENT)"),
    ("FORMAT7",  20, "Multiple numbered main sheets — MAIN SHEET 1, MAIN SHEET 2"),
    ("FORMAT5",  30, "Multiple continuation sheets"),
    ("FORMAT6",  40, "Single continuation + multi-tag comma columns"),
    ("FORMAT4",  50, "Single continuation sheet, matrix layout"),
    ("FORMAT1",  60, "Multi-annexure SPIR"),
    ("FORMAT3",  70, "Single-sheet, multiple tag columns"),
    ("FORMAT2",  80, "Single-sheet, single tag — most generic"),
]


def _bridge(fmt_name: str):
    """
    Build (detect_fn, extract_fn) that wrap the existing spir_engine API.
    This bridge means spir_engine.py is untouched.
    """
    from extraction.spir_engine import detect_format, extract_spir

    def detect(wb) -> bool:
        try:
            return detect_format(wb) == fmt_name
        except Exception:
            return False

    def extract(wb) -> FormatResult:
        raw = extract_spir(wb)
        return FormatResult(
            format_name    = raw.get("format", fmt_name),
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

    return detect, extract


def bootstrap_registry() -> None:
    """Register all known formats. Idempotent."""
    global _done
    if _done:
        return
    for fmt_name, priority, description in REGISTRY_CONFIG:
        det, ext = _bridge(fmt_name)
        register(FormatEntry(
            name=fmt_name, description=description,
            detect=det, extract=ext, priority=priority,
        ))
    _done = True
    log.info("Registry: %d formats registered", len(REGISTRY_CONFIG))

    
REGISTERED_FORMATS = []

def bootstrap_registry():
    global REGISTERED_FORMATS
    REGISTERED_FORMATS = []

def list_registered_formats():
    return REGISTERED_FORMATS