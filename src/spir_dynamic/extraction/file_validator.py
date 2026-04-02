"""
File validation before extraction.
Checks extension, size, and whether the file can be opened.
"""
from __future__ import annotations

import io
import logging

import openpyxl

log = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".xlsx", ".xlsm", ".xls", ".csv"}


class ValidationError(Exception):
    """Raised when file validation fails."""


def validate_file(
    filename: str,
    content: bytes,
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

    # Size check
    if not content:
        raise ValidationError("File is empty.")

    size_mb = len(content) / (1024 * 1024)
    if size_mb > max_mb:
        raise ValidationError(
            f"File is {size_mb:.1f} MB — exceeds limit of {max_mb} MB."
        )

    # Openability check (skip for CSV)
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
