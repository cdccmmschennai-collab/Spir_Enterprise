"""
File validation before extraction.
Checks extension, size, file signature, and whether the file can be opened.

Accepted formats: .xlsx / .xlsm (ZIP-based, read by openpyxl) and .xls (BIFF in
an OLE2 compound file, read by xlrd — see workbook_loader.py). The signature
must match the extension, so a renamed file is rejected with an explicit reason.

When given a Path, validation is lightweight (signature check only) — the
workbook is opened exactly once inside pipeline.py.
When given bytes, the workbook is also opened (used by Celery workers).
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Union

import openpyxl

log = logging.getLogger(__name__)

OOXML_EXTENSIONS = frozenset({".xlsx", ".xlsm"})
LEGACY_XLS_EXTENSION = ".xls"
ALLOWED_EXTENSIONS = OOXML_EXTENSIONS | {LEGACY_XLS_EXTENSION}

# PK ZIP local-file header — all xlsx/xlsm are ZIP archives
_XLSX_MAGIC = b'PK\x03\x04'
# OLE2 Compound File header — legacy .xls (and password-protected .xlsx)
_OLE_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
_SIGNATURE_LEN = len(_OLE_MAGIC)

_RESAVE_HINT = "Open it in Excel, save it as .xlsx, and upload it again."


class ValidationError(Exception):
    """Raised when file validation fails."""


def file_extension(filename: str) -> str:
    """Lower-cased extension including the dot ('' when there is none)."""
    dot_idx = (filename or "").rfind(".")
    return filename[dot_idx:].lower() if dot_idx >= 0 else ""


def unsupported_extension_message(ext: str) -> str:
    accepted = ", ".join(sorted(ALLOWED_EXTENSIONS))
    if not ext:
        return f"The file has no extension. Accepted: {accepted}."
    if ext == ".csv":
        return f"CSV files are not supported — a SPIR sheet needs its Excel layout. {_RESAVE_HINT}"
    return f"Unsupported file type '{ext}'. Accepted: {accepted}. {_RESAVE_HINT}"


def validate_extension(filename: str) -> str:
    """Return the extension when it is accepted, else raise ValidationError."""
    if not filename:
        raise ValidationError("No filename provided.")
    ext = file_extension(filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(unsupported_extension_message(ext))
    return ext


def _check_signature(ext: str, head: bytes) -> None:
    """Reject content whose leading bytes do not match the extension."""
    is_zip = head[:len(_XLSX_MAGIC)] == _XLSX_MAGIC
    is_ole = head[:_SIGNATURE_LEN] == _OLE_MAGIC

    if ext in OOXML_EXTENSIONS:
        if is_zip:
            return
        if is_ole:
            raise ValidationError(
                f"The file is named {ext} but is not a ZIP-based Excel workbook: it is either "
                f"password-protected or a legacy .xls workbook saved with the wrong extension. "
                f"Remove any password, or open it in Excel, save it as .xlsx, and upload it again."
            )
        raise ValidationError(
            "File does not appear to be a valid Excel file (bad ZIP header)."
        )

    if ext == LEGACY_XLS_EXTENSION:
        if is_ole:
            return
        if is_zip:
            raise ValidationError(
                "The file is named .xls but contains a ZIP-based (.xlsx/.xlsm) workbook. "
                "Upload it with its correct extension."
            )
        raise ValidationError(
            "File does not appear to be a valid legacy Excel (.xls) workbook "
            "(bad compound-file header)."
        )


def validate_file(
    filename: str,
    content: Union[bytes, Path],
    max_mb: int = 2048,
) -> None:
    """
    Validate an uploaded file before processing.

    Raises ValidationError with a human-readable message on failure.
    """
    ext = validate_extension(filename)

    if isinstance(content, Path):
        # ── Path-based validation — lightweight, no workbook parse ──────────────
        # The workbook is opened ONCE inside pipeline.py; do not open it here.
        if not content.exists():
            raise ValidationError("Uploaded file is missing.")

        file_size = content.stat().st_size
        if file_size == 0:
            raise ValidationError("File is empty.")

        size_mb = file_size / (1024 * 1024)
        if size_mb > max_mb:
            raise ValidationError(
                f"File is {size_mb:.1f} MB — exceeds limit of {max_mb} MB."
            )

        # Signature check: 8-byte read, no parse cost
        with open(content, "rb") as fh:
            _check_signature(ext, fh.read(_SIGNATURE_LEN))
        return

    # ── Bytes-based validation — original behavior (Celery workers use this) ───
    if not content:
        raise ValidationError("File is empty.")

    size_mb = len(content) / (1024 * 1024)
    if size_mb > max_mb:
        raise ValidationError(
            f"File is {size_mb:.1f} MB — exceeds limit of {max_mb} MB."
        )

    _check_signature(ext, content[:_SIGNATURE_LEN])

    if ext == LEGACY_XLS_EXTENSION:
        _validate_xls_bytes(content)
        return

    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        if not wb.sheetnames:
            raise ValidationError("Excel file has no sheets.")
        wb.close()
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(f"Cannot open file: {exc}") from exc


def _validate_xls_bytes(content: bytes) -> None:
    """Open the .xls workbook index (no cell parse) to confirm xlrd can read it."""
    import xlrd

    class _Quiet:
        def write(self, *_a):
            pass

    try:
        book = xlrd.open_workbook(file_contents=content, on_demand=True, logfile=_Quiet())
    except xlrd.XLRDError as exc:
        if "encrypted" in str(exc).lower():
            raise ValidationError(
                "This .xls workbook is password-protected. Remove the password in "
                "Excel and upload it again."
            ) from exc
        raise ValidationError(f"Cannot read this .xls workbook: {exc}") from exc
    except Exception as exc:
        raise ValidationError(f"Cannot read this .xls workbook — the file is damaged: {exc}") from exc
    try:
        if book.nsheets == 0:
            raise ValidationError("Excel file has no sheets.")
    finally:
        book.release_resources()
