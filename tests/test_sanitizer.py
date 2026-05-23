"""
Integration tests for the Excel workbook sanitizer.

Each test builds a workbook (or a raw ZIP) programmatically so the suite
runs without needing real industrial SPIR files.  Tests cover:

  1. Small file below threshold   — sanitizer skipped
  2. File with embedded media     — sanitized, media removed
  3. File with OLE embeddings     — sanitized, embeddings removed
  4. Corrupted ZIP                — fallback to original
  5. Sanitization validation fail — fallback to original
  6. Disabled via config          — sanitizer skipped unconditionally
  7. Non-XLSX extension           — sanitizer skipped
  8. Validation: workbook.xml present after sanitization
  9. Validation: worksheets intact after sanitization
 10. xl/drawings preserved        — VML form controls survive sanitization
"""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

import openpyxl
import pytest

# ---------------------------------------------------------------------------
# Helpers — build minimal XLSX files on disk
# ---------------------------------------------------------------------------

def _write_minimal_xlsx(path: Path, add_media: bool = False, add_embeddings: bool = False,
                        add_drawings: bool = False, add_printer: bool = False) -> None:
    """
    Write a valid minimal XLSX to *path*.

    Optionally injects synthetic bulk-asset entries so the sanitizer has
    something to strip.
    """
    buf = io.BytesIO()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Main"
    ws["A1"] = "TAG"
    ws["B1"] = "DESCRIPTION"
    ws["A2"] = "23V01"
    ws["B2"] = "Spare valve"
    wb.save(buf)
    buf.seek(0)

    # Re-open as ZIP and inject extra entries
    original_bytes = buf.read()

    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original_bytes), "r") as zin, \
         zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for entry in zin.infolist():
            zout.writestr(entry, zin.read(entry.filename))

        if add_media:
            # Simulate an embedded image (fake PNG bytes)
            zout.writestr("xl/media/image1.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 512)
            zout.writestr("xl/media/image2.jpg", b"\xff\xd8\xff" + b"\x00" * 512)

        if add_embeddings:
            # Simulate an OLE object embedding
            zout.writestr("xl/embeddings/oleObject1.bin", b"\xd0\xcf\x11\xe0" + b"\x00" * 1024)
            zout.writestr("xl/embeddings/oleObject2.bin", b"\xd0\xcf\x11\xe0" + b"\x00" * 1024)

        if add_drawings:
            # Simulate a VML drawing (checkbox / form control)
            vml = b'<xml xmlns:v="urn:schemas-microsoft-com:vml"><v:shape type="#_x0000_t201"/></xml>'
            zout.writestr("xl/drawings/drawing1.xml", vml)
            zout.writestr("xl/drawings/_rels/drawing1.xml.rels", b"<Relationships/>")

        if add_printer:
            zout.writestr("xl/printerSettings/printerSettings1.bin", b"\x00" * 256)
            zout.writestr("printerSettings/printerSettings1.bin", b"\x00" * 256)
            zout.writestr("docProps/thumbnail.jpeg", b"\xff\xd8\xff" + b"\x00" * 128)

    path.write_bytes(out.getvalue())


def _write_corrupt_file(path: Path) -> None:
    """Write a file that is NOT a valid ZIP archive."""
    path.write_bytes(b"This is not a ZIP file at all. \x00\x01\x02")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_upload_dir(tmp_path: Path) -> Path:
    d = tmp_path / "batch_uploads"
    d.mkdir()
    return d


@pytest.fixture()
def settings_above_threshold(monkeypatch):
    """Patch settings so the threshold is 0 MB — all files are sanitized."""
    monkeypatch.setenv("SANITIZER_ENABLED", "true")
    monkeypatch.setenv("SANITIZER_THRESHOLD_MB", "0")
    # Clear lru_cache so new env vars are picked up.
    from spir_dynamic.app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def settings_high_threshold(monkeypatch):
    """Patch settings so threshold is 9999 MB — nothing is sanitized."""
    monkeypatch.setenv("SANITIZER_ENABLED", "true")
    monkeypatch.setenv("SANITIZER_THRESHOLD_MB", "9999")
    from spir_dynamic.app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def settings_disabled(monkeypatch):
    """Patch settings so the sanitizer is explicitly disabled."""
    monkeypatch.setenv("SANITIZER_ENABLED", "false")
    monkeypatch.setenv("SANITIZER_THRESHOLD_MB", "0")
    from spir_dynamic.app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Test 1 — Small file below threshold: sanitizer skipped
# ---------------------------------------------------------------------------

def test_small_file_skipped(tmp_upload_dir, settings_high_threshold):
    from spir_dynamic.extraction.sanitizer import sanitize_workbook

    xlsx = tmp_upload_dir / "small.xlsx"
    _write_minimal_xlsx(xlsx)

    result = sanitize_workbook(xlsx, "small.xlsx")

    assert result.sanitized_path is None
    assert not result.used_fallback
    assert "below_threshold" in result.skip_reason


# ---------------------------------------------------------------------------
# Test 2 — Workbook with embedded media: sanitized, media removed
# ---------------------------------------------------------------------------

def test_embedded_media_removed(tmp_upload_dir, settings_above_threshold):
    from spir_dynamic.extraction.sanitizer import sanitize_workbook

    xlsx = tmp_upload_dir / "with_media.xlsx"
    _write_minimal_xlsx(xlsx, add_media=True)

    result = sanitize_workbook(xlsx, "with_media.xlsx")

    try:
        assert result.sanitized_path is not None, f"Expected sanitized copy, got: {result.skip_reason}"
        assert not result.used_fallback

        # Verify media entries are gone from sanitized ZIP
        with zipfile.ZipFile(result.sanitized_path, "r") as zf:
            names = zf.namelist()
        media_entries = [n for n in names if n.startswith("xl/media/")]
        assert media_entries == [], f"xl/media/ entries still present: {media_entries}"

        # Verify worksheets are intact
        assert any(n.startswith("xl/worksheets/sheet") for n in names)

        # Verify openpyxl can still read the data correctly
        wb = openpyxl.load_workbook(str(result.sanitized_path), data_only=True)
        ws = wb.active
        assert ws["A1"].value == "TAG"
        assert ws["A2"].value == "23V01"
        wb.close()

        assert result.entries_removed >= 2
        assert result.reduction_pct > 0

    finally:
        if result.sanitized_path and result.sanitized_path.exists():
            result.sanitized_path.unlink()


# ---------------------------------------------------------------------------
# Test 3 — Workbook with OLE embeddings: sanitized, embeddings removed
# ---------------------------------------------------------------------------

def test_ole_embeddings_removed(tmp_upload_dir, settings_above_threshold):
    from spir_dynamic.extraction.sanitizer import sanitize_workbook

    xlsx = tmp_upload_dir / "with_ole.xlsx"
    _write_minimal_xlsx(xlsx, add_embeddings=True)

    result = sanitize_workbook(xlsx, "with_ole.xlsx")

    try:
        assert result.sanitized_path is not None, f"Expected sanitized copy: {result.skip_reason}"
        assert not result.used_fallback

        with zipfile.ZipFile(result.sanitized_path, "r") as zf:
            names = zf.namelist()

        embed_entries = [n for n in names if n.startswith("xl/embeddings/")]
        assert embed_entries == [], f"xl/embeddings/ entries still present: {embed_entries}"

        # Data integrity
        wb = openpyxl.load_workbook(str(result.sanitized_path), data_only=True)
        ws = wb.active
        assert ws["B2"].value == "Spare valve"
        wb.close()

    finally:
        if result.sanitized_path and result.sanitized_path.exists():
            result.sanitized_path.unlink()


# ---------------------------------------------------------------------------
# Test 4 — Corrupted file: fallback to original, no crash
# ---------------------------------------------------------------------------

def test_corrupted_file_fallback(tmp_upload_dir, settings_above_threshold):
    from spir_dynamic.extraction.sanitizer import sanitize_workbook

    corrupt = tmp_upload_dir / "corrupt.xlsx"
    _write_corrupt_file(corrupt)

    result = sanitize_workbook(corrupt, "corrupt.xlsx")

    # Must fall back gracefully — sanitized_path stays None
    assert result.sanitized_path is None
    # Either skip_reason (not_a_zip) or used_fallback=True
    assert result.used_fallback or "not_a_zip" in result.skip_reason
    # Original file must still exist (not deleted by sanitizer)
    assert corrupt.exists()


# ---------------------------------------------------------------------------
# Test 5 — Sanitization succeeds but openpyxl validation fails: fallback
# ---------------------------------------------------------------------------

def test_validation_failure_triggers_fallback(tmp_upload_dir, settings_above_threshold):
    from spir_dynamic.extraction import sanitizer as san_module
    from spir_dynamic.extraction.sanitizer import sanitize_workbook

    xlsx = tmp_upload_dir / "valid_but_invalid_after.xlsx"
    _write_minimal_xlsx(xlsx, add_media=True)

    # Patch _validate_workbook to always return failure
    with patch.object(san_module, "_validate_workbook", return_value=(False, "injected_test_failure")):
        result = sanitize_workbook(xlsx, "valid_but_invalid_after.xlsx")

    assert result.sanitized_path is None
    assert result.used_fallback
    assert "fallback" in result.skip_reason
    # Original still exists
    assert xlsx.exists()


# ---------------------------------------------------------------------------
# Test 6 — Sanitizer disabled via config: always skipped
# ---------------------------------------------------------------------------

def test_sanitizer_disabled(tmp_upload_dir, settings_disabled):
    from spir_dynamic.extraction.sanitizer import sanitize_workbook

    xlsx = tmp_upload_dir / "big_disabled.xlsx"
    _write_minimal_xlsx(xlsx, add_media=True)

    result = sanitize_workbook(xlsx, "big_disabled.xlsx")

    assert result.sanitized_path is None
    assert not result.used_fallback
    assert result.skip_reason == "disabled"


# ---------------------------------------------------------------------------
# Test 7 — Non-XLSX extension: sanitizer skipped
# ---------------------------------------------------------------------------

def test_non_xlsx_skipped(tmp_upload_dir, settings_above_threshold):
    from spir_dynamic.extraction.sanitizer import sanitize_workbook

    csv_file = tmp_upload_dir / "data.csv"
    csv_file.write_text("TAG,DESCRIPTION\n23V01,Spare valve\n")

    result = sanitize_workbook(csv_file, "data.csv")

    assert result.sanitized_path is None
    assert not result.used_fallback
    assert "unsupported_ext" in result.skip_reason


# ---------------------------------------------------------------------------
# Test 8 — workbook.xml present after sanitization
# ---------------------------------------------------------------------------

def test_workbook_xml_preserved(tmp_upload_dir, settings_above_threshold):
    from spir_dynamic.extraction.sanitizer import sanitize_workbook

    xlsx = tmp_upload_dir / "workbook_xml.xlsx"
    _write_minimal_xlsx(xlsx, add_media=True, add_printer=True)

    result = sanitize_workbook(xlsx, "workbook_xml.xlsx")

    try:
        assert result.sanitized_path is not None
        with zipfile.ZipFile(result.sanitized_path, "r") as zf:
            assert "xl/workbook.xml" in zf.namelist()
    finally:
        if result.sanitized_path and result.sanitized_path.exists():
            result.sanitized_path.unlink()


# ---------------------------------------------------------------------------
# Test 9 — Worksheets intact after sanitization
# ---------------------------------------------------------------------------

def test_worksheets_intact(tmp_upload_dir, settings_above_threshold):
    from spir_dynamic.extraction.sanitizer import sanitize_workbook

    xlsx = tmp_upload_dir / "sheets.xlsx"
    _write_minimal_xlsx(xlsx, add_media=True)

    result = sanitize_workbook(xlsx, "sheets.xlsx")

    try:
        assert result.sanitized_path is not None
        with zipfile.ZipFile(result.sanitized_path, "r") as zf:
            names = zf.namelist()
        assert any(n.startswith("xl/worksheets/sheet") and n.endswith(".xml") for n in names)

        wb = openpyxl.load_workbook(str(result.sanitized_path), data_only=True)
        assert "Main" in wb.sheetnames
        wb.close()
    finally:
        if result.sanitized_path and result.sanitized_path.exists():
            result.sanitized_path.unlink()


# ---------------------------------------------------------------------------
# Test 10 — xl/drawings preserved (VML form controls for SPIR-type detection)
# ---------------------------------------------------------------------------

def test_drawings_preserved(tmp_upload_dir, settings_above_threshold):
    from spir_dynamic.extraction.sanitizer import sanitize_workbook

    xlsx = tmp_upload_dir / "with_drawings.xlsx"
    _write_minimal_xlsx(xlsx, add_drawings=True, add_media=True)

    result = sanitize_workbook(xlsx, "with_drawings.xlsx")

    try:
        assert result.sanitized_path is not None, f"Expected sanitized copy: {result.skip_reason}"

        with zipfile.ZipFile(result.sanitized_path, "r") as zf:
            names = zf.namelist()

        # Drawings must be preserved
        drawing_entries = [n for n in names if n.startswith("xl/drawings/")]
        assert len(drawing_entries) > 0, "xl/drawings/ was removed — VML detection will break"

        # Media must be gone
        media_entries = [n for n in names if n.startswith("xl/media/")]
        assert media_entries == [], f"xl/media/ entries still present: {media_entries}"

    finally:
        if result.sanitized_path and result.sanitized_path.exists():
            result.sanitized_path.unlink()


# ---------------------------------------------------------------------------
# Test 11 — Printer settings and thumbnails removed
# ---------------------------------------------------------------------------

def test_printer_and_thumbnail_removed(tmp_upload_dir, settings_above_threshold):
    from spir_dynamic.extraction.sanitizer import sanitize_workbook

    xlsx = tmp_upload_dir / "with_printer.xlsx"
    _write_minimal_xlsx(xlsx, add_printer=True)

    result = sanitize_workbook(xlsx, "with_printer.xlsx")

    try:
        assert result.sanitized_path is not None, f"Expected sanitized copy: {result.skip_reason}"

        with zipfile.ZipFile(result.sanitized_path, "r") as zf:
            names = zf.namelist()

        printer_entries = [
            n for n in names
            if "printerSettings" in n or "thumbnail" in n.lower()
        ]
        assert printer_entries == [], f"Printer/thumbnail entries remain: {printer_entries}"

    finally:
        if result.sanitized_path and result.sanitized_path.exists():
            result.sanitized_path.unlink()


# ---------------------------------------------------------------------------
# Test 12 — SanitizerResult metrics populated correctly on success
# ---------------------------------------------------------------------------

def test_metrics_populated_on_success(tmp_upload_dir, settings_above_threshold):
    from spir_dynamic.extraction.sanitizer import sanitize_workbook

    xlsx = tmp_upload_dir / "metrics_check.xlsx"
    _write_minimal_xlsx(xlsx, add_media=True, add_embeddings=True)

    result = sanitize_workbook(xlsx, "metrics_check.xlsx")

    try:
        assert result.sanitized_path is not None
        # Use actual byte size for tiny synthetic files where rounding gives 0.0 MB
        assert result.sanitized_path.stat().st_size > 0
        assert xlsx.stat().st_size > 0
        assert xlsx.stat().st_size >= result.sanitized_path.stat().st_size
        assert 0 <= result.reduction_pct <= 100
        assert result.duration_s > 0
        assert result.entries_removed >= 1
        assert result.bytes_removed >= 0
        assert result.skip_reason == ""
        assert not result.used_fallback
    finally:
        if result.sanitized_path and result.sanitized_path.exists():
            result.sanitized_path.unlink()
