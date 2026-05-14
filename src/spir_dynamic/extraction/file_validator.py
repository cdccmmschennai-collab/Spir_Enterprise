"""
File validation before extraction.
Checks extension, size, and whether the file can be opened.

When given a Path, validation is lightweight (ZIP magic check only) — the workbook
is opened exactly once inside pipeline.py.
When given bytes, original behavior is preserved (used by Celery workers).
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Union

import openpyxl

log = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".xlsx", ".xlsm", ".xls", ".csv"}

# PK ZIP local-file header — all xlsx/xlsm are ZIP archives
_XLSX_MAGIC = b'PK\x03\x04'


class ValidationError(Exception):
    """Raised when file validation fails."""


def validate_file(
    filename: str,
    content: Union[bytes, Path],
    max_mb: int = 2048,
) -> None:
    """
    Validate an uploaded file before processing.

    Raises ValidationError with a human-readable message on failure.
    """
    if not filename:
        raise ValidationError("No filename provided.")

    # Extension check
    ext = ""
    dot_idx = filename.rfind(".")
    if dot_idx >= 0:
        ext = filename[dot_idx:].lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(
            f"Unsupported file type '{ext}'. "
            f"Accepted: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

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

        if ext in (".xlsx", ".xlsm"):
            # ZIP magic number check: 4-byte read, no parse cost
            with open(content, "rb") as fh:
                magic = fh.read(4)
            if magic != _XLSX_MAGIC:
                raise ValidationError(
                    "File does not appear to be a valid Excel file (bad ZIP header)."
                )

        elif ext == ".csv":
            # Read first two lines only — enough to confirm data rows exist
            with open(content, "r", encoding="utf-8", errors="ignore") as fh:
                fh.readline()          # header row
                first_data = fh.readline()
            if not first_data.strip():
                raise ValidationError("CSV file has no data rows.")

        # .xls: no cheap signature check — openpyxl/xlrd will surface errors
        return

    # ── Bytes-based validation — original behavior (Celery workers use this) ───
    if not content:
        raise ValidationError("File is empty.")

    size_mb = len(content) / (1024 * 1024)
    if size_mb > max_mb:
        raise ValidationError(
            f"File is {size_mb:.1f} MB — exceeds limit of {max_mb} MB."
        )

    if ext == ".csv":
        lines = content.decode("utf-8", errors="ignore").strip().split("\n")
        if len(lines) < 2:
            raise ValidationError("CSV file has no data rows.")
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
