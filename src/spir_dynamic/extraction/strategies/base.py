"""
Base protocol for extraction strategies.

Each strategy handles a specific TagLayout type and knows how to
extract data rows from a worksheet given its SheetProfile.
"""
from __future__ import annotations

from typing import Any, Protocol

from spir_dynamic.models.sheet_profile import SheetProfile


class ExtractionStrategy(Protocol):
    """Protocol that all extraction strategies must implement."""

    def extract(
        self,
        ws,
        profile: SheetProfile,
        spir_no: str,
    ) -> list[dict[str, Any]]:
        """
        Extract data rows from a worksheet.

        Args:
            ws: openpyxl worksheet object
            profile: Analyzed SheetProfile with header row, column map, etc.
            spir_no: SPIR document number for output

        Returns:
            List of dicts with field names matching output_schema field names.
        """
        ...
