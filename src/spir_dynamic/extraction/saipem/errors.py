"""Errors raised by the Saipem extraction path."""
from __future__ import annotations

from spir_dynamic.extraction.file_validator import ValidationError

# User-facing messages stay short; the detailed diagnosis goes to the server log.
NOT_SAIPEM_MESSAGE = "This looks like a Standard SPIR file. Switch to Standard and try again."
NO_SAIPEM_ROWS_MESSAGE = "No spare parts could be matched to equipment tags in this Saipem file."


class SaipemFormError(ValidationError):
    """
    The workbook cannot be extracted as a Saipem SPIR form.

    A ValidationError, so the sync route answers 422 with this message; the
    Celery task fails the file at once (retrying cannot change the workbook).
    Raised instead of ever falling back to the Standard path or exporting an
    empty / invalid Saipem workbook.
    """
