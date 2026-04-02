"""
Data models for dynamic sheet analysis.

SheetProfile is the central data structure — every sheet in a workbook gets
one, populated by the analysis engine and consumed by extraction strategies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SheetRole(Enum):
    """Role assigned to a sheet after content analysis."""

    DATA = "data"                              # Primary data sheet
    CONTINUATION = "continuation"              # Overflow rows continuing another sheet
    ANNEXURE = "annexure"                      # Transposed layout (tags as rows)
    UTILITY = "utility"                        # Validation / lookup / instructions (skip)
    UNKNOWN = "unknown"


class TagLayout(Enum):
    """How tags are arranged within a sheet."""

    COLUMN_HEADERS = "column_headers"          # Tags are column headers in top rows
    TAG_COLUMN = "tag_column"                  # Tags in a dedicated data column
    ROW_HEADERS = "row_headers"                # Tags as row labels (annexure)
    GLOBAL_TAG = "global_tag"                  # Single tag for entire sheet
    NONE = "none"                              # No tags detected


@dataclass
class SheetProfile:
    """Complete analysis result for a single worksheet."""

    name: str
    role: SheetRole = SheetRole.UNKNOWN
    tag_layout: TagLayout = TagLayout.NONE

    # Header / data boundaries (1-based row indices)
    header_row: int | None = None
    data_start_row: int | None = None
    data_end_row: int | None = None

    # Column mapping: logical field name -> 1-based column index
    column_map: dict[str, int] = field(default_factory=dict)

    # Tag locations (varies by layout)
    tag_columns: list[int] = field(default_factory=list)       # COLUMN_HEADERS
    tag_rows: list[int] = field(default_factory=list)          # ROW_HEADERS
    tag_column_index: int | None = None                        # TAG_COLUMN
    global_tag: str | None = None                              # GLOBAL_TAG

    # Metadata extracted from the header area
    metadata: dict[str, Any] = field(default_factory=dict)

    # Continuation relationship
    continuation_of: str | None = None

    # Statistics
    row_count: int = 0
    confidence: float = 0.0

    @property
    def is_extractable(self) -> bool:
        """True if this sheet should be processed for data extraction."""
        return self.role in (SheetRole.DATA, SheetRole.CONTINUATION, SheetRole.ANNEXURE)
