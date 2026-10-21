"""
services/extraction_mode.py
---------------------------
The user-selected extraction template: Standard or Saipem.

    standard  the generic extraction path — never switches to Saipem, however
              much the workbook resembles a Saipem form
    saipem    the dedicated Saipem path (extraction/saipem) — a workbook that
              is not a Saipem SPIR form fails with a clear error; there is no
              fallback to Standard

Compatibility: a caller that sends no mode (an older client, a Celery task
queued before the selector existed, an internal call) gets None, meaning the
structural auto-detection run_pipeline used before the selector was added.
An unrecognised value is rejected rather than guessed.
"""
from __future__ import annotations

from typing import Optional

MODE_STANDARD = "standard"
MODE_SAIPEM = "saipem"
EXTRACTION_MODES = (MODE_STANDARD, MODE_SAIPEM)


class InvalidExtractionMode(ValueError):
    """The submitted extraction mode is not one of EXTRACTION_MODES."""


def resolve_extraction_mode(value: Optional[str]) -> Optional[str]:
    """'standard' / 'saipem' (case and surrounding spaces ignored); None when not given."""
    if value is None:
        return None
    mode = str(value).strip().lower()
    if not mode:
        return None
    if mode not in EXTRACTION_MODES:
        raise InvalidExtractionMode(
            f"extraction_mode '{value}' is not valid — choose one of: {', '.join(EXTRACTION_MODES)}"
        )
    return mode
