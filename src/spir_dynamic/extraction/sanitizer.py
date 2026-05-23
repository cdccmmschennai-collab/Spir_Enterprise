"""
Excel workbook sanitizer.

Strips embedded non-tabular bulk assets from XLSX/XLSM files before
openpyxl extraction, reducing memory pressure and I/O overhead on large
industrial files that contain embedded PDFs, images, or OLE objects.

Strategy: treat .xlsx as a ZIP archive, extract to a disk-based temp directory,
remove known-safe entries, re-zip to a new temp file. Falls back to the original
on any failure — extraction always proceeds.

Safe to remove (bulk binary assets irrelevant to tabular data):
  xl/media/            — embedded images, logos, photos
  xl/embeddings/       — OLE objects, embedded PDFs, CAD previews
  printerSettings/     — printer configuration blobs (top-level or inside xl/)
  xl/externalLinks/    — external link caches (pipeline uses keep_links=False)
  docProps/thumbnail*  — workbook thumbnail previews
  */thumbnail*         — any nested thumbnail path

Preserved (conservative — affects extraction correctness):
  xl/drawings/         — VML form controls used for SPIR-type checkbox detection
  xl/comments/         — may contain structured notes referenced by extraction
  xl/worksheets/       — core tabular data (never touched)
  xl/workbook.xml      — workbook structure (never touched)
  xl/sharedStrings.xml — cell string table (never touched)
  xl/styles.xml        — cell styles (never touched)
  [Content_Types].xml  — ZIP content manifest (never touched)
  _rels/               — relationship graph (never touched)
"""
from __future__ import annotations

import fnmatch
import os
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import openpyxl
import structlog

log = structlog.stdlib.get_logger(__name__)

# ZIP entry prefixes that are safe to strip unconditionally.
_SAFE_REMOVAL_PREFIXES: tuple[str, ...] = (
    "xl/media/",
    "xl/embeddings/",
    "printerSettings/",        # top-level (some vendors)
    "xl/printerSettings/",     # nested variant
    "xl/externalLinks/",
)

# Glob patterns applied to full entry names.
_SAFE_REMOVAL_PATTERNS: tuple[str, ...] = (
    "docProps/thumbnail*",
    "*/thumbnail*",
)


@dataclass
class SanitizerResult:
    """Outcome of a single sanitization attempt."""

    sanitized_path: Optional[Path] = None  # None → caller must use original
    used_fallback: bool = False
    original_size_mb: float = 0.0
    sanitized_size_mb: float = 0.0
    reduction_pct: float = 0.0
    duration_s: float = 0.0
    entries_removed: int = 0
    bytes_removed: int = 0
    skip_reason: str = ""  # non-empty when sanitizer was not attempted


def sanitize_workbook(upload_path: Path, filename: str) -> SanitizerResult:
    """
    Strip bulk-asset ZIP entries from an XLSX file and return a sanitized copy.

    The original file is never modified.  The sanitized copy is written to the
    same directory as the original (same filesystem partition — no cross-device
    copy).  The caller is responsible for deleting ``result.sanitized_path``
    after extraction completes.

    If ``sanitized_path`` is None the caller must use the original upload_path.
    This function never raises — all failures produce a fallback result.
    """
    from spir_dynamic.app.config import get_settings
    cfg = get_settings()

    t0 = time.perf_counter()

    # ── Bypass: disabled ─────────────────────────────────────────────────────
    if not cfg.sanitizer_enabled:
        return SanitizerResult(skip_reason="disabled")

    # ── Bypass: unsupported extension ────────────────────────────────────────
    ext = upload_path.suffix.lower()
    if ext not in (".xlsx", ".xlsm"):
        return SanitizerResult(skip_reason=f"unsupported_ext:{ext}")

    # ── Bypass: below size threshold ─────────────────────────────────────────
    try:
        original_size_bytes = upload_path.stat().st_size
    except OSError as exc:
        return SanitizerResult(skip_reason=f"stat_failed:{exc}")

    original_size_mb = original_size_bytes / (1024 * 1024)

    if original_size_mb < cfg.sanitizer_threshold_mb:
        log.debug(
            "sanitizer.skipped",
            filename=filename,
            size_mb=round(original_size_mb, 1),
            threshold_mb=cfg.sanitizer_threshold_mb,
            reason="below_threshold",
        )
        return SanitizerResult(
            original_size_mb=round(original_size_mb, 2),
            skip_reason=(
                f"below_threshold:{original_size_mb:.1f}MB"
                f"<{cfg.sanitizer_threshold_mb}MB"
            ),
        )

    # ── Bypass: not a valid ZIP (fast check before allocating temp space) ────
    if not zipfile.is_zipfile(upload_path):
        return SanitizerResult(
            original_size_mb=round(original_size_mb, 2),
            skip_reason="not_a_zip",
        )

    log.info(
        "sanitizer.start",
        filename=filename,
        original_size_mb=round(original_size_mb, 1),
        threshold_mb=cfg.sanitizer_threshold_mb,
    )

    # ── Create output file alongside original (same partition) ───────────────
    out_fd, out_path_str = tempfile.mkstemp(
        suffix=ext, prefix="san_", dir=upload_path.parent
    )
    os.close(out_fd)
    sanitized_path = Path(out_path_str)

    try:
        entries_removed, bytes_removed = _strip_zip_entries(upload_path, sanitized_path)

        sanitized_size_bytes = sanitized_path.stat().st_size
        sanitized_size_mb = sanitized_size_bytes / (1024 * 1024)
        reduction_pct = (
            (original_size_bytes - sanitized_size_bytes) / original_size_bytes * 100
            if original_size_bytes > 0
            else 0.0
        )

        # ── Validate workbook integrity before returning ───────────────────────
        valid, reason = _validate_workbook(sanitized_path)
        if not valid:
            raise ValueError(f"Integrity check failed: {reason}")

        duration_s = round(time.perf_counter() - t0, 2)
        log.info(
            "sanitizer.success",
            filename=filename,
            original_mb=round(original_size_mb, 1),
            sanitized_mb=round(sanitized_size_mb, 1),
            reduction_pct=round(reduction_pct, 1),
            entries_removed=entries_removed,
            bytes_removed=bytes_removed,
            duration_s=duration_s,
        )

        return SanitizerResult(
            sanitized_path=sanitized_path,
            used_fallback=False,
            original_size_mb=round(original_size_mb, 2),
            sanitized_size_mb=round(sanitized_size_mb, 2),
            reduction_pct=round(reduction_pct, 1),
            duration_s=duration_s,
            entries_removed=entries_removed,
            bytes_removed=bytes_removed,
        )

    except Exception as exc:
        duration_s = round(time.perf_counter() - t0, 2)
        log.warning(
            "sanitizer.fallback",
            filename=filename,
            reason=str(exc),
            duration_s=duration_s,
        )
        _safe_unlink(sanitized_path)
        return SanitizerResult(
            sanitized_path=None,
            used_fallback=True,
            original_size_mb=round(original_size_mb, 2),
            duration_s=duration_s,
            skip_reason=f"fallback:{type(exc).__name__}:{exc}",
        )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _is_safe_to_remove(entry_name: str) -> bool:
    """Return True if this ZIP entry is a bulk asset safe to strip."""
    for prefix in _SAFE_REMOVAL_PREFIXES:
        if entry_name.startswith(prefix):
            return True
    for pattern in _SAFE_REMOVAL_PATTERNS:
        if fnmatch.fnmatch(entry_name, pattern):
            return True
    return False


def _strip_zip_entries(src: Path, dst: Path) -> tuple[int, int]:
    """
    Copy src ZIP to dst ZIP, omitting bulk-asset entries.

    Uses a disk-based temporary directory — never loads the full archive
    into memory via BytesIO.

    Returns (entries_removed, bytes_removed).
    """
    entries_removed = 0
    bytes_removed = 0

    with tempfile.TemporaryDirectory(prefix="spir_san_") as workdir:
        workdir_path = Path(workdir)

        with zipfile.ZipFile(src, "r") as zin:
            all_entries = zin.infolist()
            zin.extractall(workdir)

        with zipfile.ZipFile(
            dst, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
        ) as zout:
            for entry in all_entries:
                if _is_safe_to_remove(entry.filename):
                    entries_removed += 1
                    bytes_removed += entry.file_size
                    continue

                extracted = workdir_path / entry.filename
                if extracted.exists() and extracted.is_file():
                    zout.write(extracted, entry.filename)
                elif not extracted.exists() and entry.filename.endswith("/"):
                    # Directory entry — write as empty directory record
                    zout.mkdir(entry.filename)

    return entries_removed, bytes_removed


def _validate_workbook(path: Path) -> tuple[bool, str]:
    """
    Verify sanitized workbook has structural integrity.

    Checks (in order):
      1. Valid ZIP archive
      2. xl/workbook.xml present
      3. At least one xl/worksheets/sheet*.xml present
      4. openpyxl can open and read sheet names without error

    Returns (ok: bool, reason: str).
    """
    if not zipfile.is_zipfile(path):
        return False, "not_a_valid_zip"

    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())

    if "xl/workbook.xml" not in names:
        return False, "missing_workbook.xml"

    has_sheet = any(
        n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
        for n in names
    )
    if not has_sheet:
        return False, "no_worksheet_xml"

    try:
        wb = openpyxl.load_workbook(
            str(path), data_only=True, keep_links=False, read_only=True
        )
        has_sheets = bool(wb.sheetnames)
        wb.close()
    except Exception as exc:
        return False, f"openpyxl_open_failed:{exc}"

    if not has_sheets:
        return False, "no_sheets_after_load"

    return True, ""


def _safe_unlink(path: Path) -> None:
    """Delete a file, silently ignore if absent or permission denied."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
