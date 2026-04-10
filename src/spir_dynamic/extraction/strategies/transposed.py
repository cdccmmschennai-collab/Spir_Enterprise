"""
Transposed extraction strategy.

Handles sheets where tags are row labels (annexure layout).
In this layout, tags appear in column A as row headers, and data
(descriptions, quantities, prices) extends to the right.

This covers the annexure pattern where each row represents a different
tag's data for the same spare parts.
"""
from __future__ import annotations

import logging
from typing import Any

from spir_dynamic.models.sheet_profile import SheetProfile
from spir_dynamic.utils.cell_utils import clean_str, clean_num, is_placeholder, split_tags
from spir_dynamic.analysis.header_detector import is_footer_row

log = logging.getLogger(__name__)


class TransposedStrategy:
    """Extract from sheets with tags as row headers (annexure layout)."""

    def extract(
        self,
        ws,
        profile: SheetProfile,
        spir_no: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        if not profile.column_map and not profile.tag_rows:
            log.warning("TransposedStrategy: no column_map or tag_rows for '%s'", profile.name)
            return rows

        # If we have a column map, use standard tabular extraction
        # but with tag values from column A row labels
        if profile.column_map and profile.header_row:
            rows = self._extract_with_header(ws, profile, spir_no)
        else:
            # Pure transposed: figure out columns from content
            rows = self._extract_pure_transposed(ws, profile, spir_no)

        log.info(
            "TransposedStrategy: extracted %d rows from '%s'",
            len(rows), profile.name,
        )
        return rows

    def _extract_with_header(
        self,
        ws,
        profile: SheetProfile,
        spir_no: str,
    ) -> list[dict[str, Any]]:
        """Extract when we have a recognized header row + tag rows."""
        rows: list[dict[str, Any]] = []

        start_row = profile.data_start_row or (profile.header_row + 1)
        end_row = profile.data_end_row or (ws.max_row or 0)

        current_tag: str | None = None

        for r in range(start_row, end_row + 1):
            # Check column A for a tag value
            col_a = ws.cell(r, 1).value
            if col_a is not None:
                s = str(col_a).strip()
                if s and not is_placeholder(s):
                    from spir_dynamic.utils.cell_utils import looks_like_tag
                    if looks_like_tag(s):
                        current_tag = s

            # Read mapped fields
            item: dict[str, Any] = {"sheet": profile.name}
            for field, col in profile.column_map.items():
                raw = ws.cell(r, col).value
                if field in ("quantity", "unit_price", "total_price", "delivery_weeks"):
                    item[field] = clean_num(raw)
                else:
                    item[field] = clean_str(raw)

            # Check for footer
            desc = item.get("description") or ""
            if desc and is_footer_row(desc):
                break

            # Skip empty rows
            if not item.get("description") and not item.get("part_number"):
                continue

            # Expand tags
            tag_val = current_tag or item.get("tag")
            tags = split_tags(tag_val) if tag_val else [None]

            for tag in tags:
                row = dict(item)
                row["tag_no"] = tag
                row["spir_no"] = spir_no or profile.metadata.get("spir_no")
                row["desc"] = row.pop("description", None)
                row["mfr_part_no"] = row.pop("part_number", None)
                row["qty_identical"] = row.pop("quantity", None)
                row["item_num"] = row.pop("item_number", None)
                row["supplier_name"] = row.pop("supplier", None)
                row["sap_no"] = row.pop("sap_number", None)
                row["manufacturer"] = row.pop("manufacturer", None) or profile.metadata.get("manufacturer")

                if "spir_type" in profile.metadata:
                    row["spir_type"] = profile.metadata["spir_type"]

                # Build new_desc
                parts = [row.get("desc"), row.get("mfr_part_no"), row.get("supplier_name")]
                new_parts = [p for p in parts if p]
                if new_parts:
                    row["new_desc"] = ",".join(new_parts)

                rows.append(row)

        return rows

    def _extract_pure_transposed(
        self,
        ws,
        profile: SheetProfile,
        spir_no: str,
    ) -> list[dict[str, Any]]:
        """
        Extract from a purely transposed sheet without a standard header.
        Tags are in column A, data extends right in columns B, C, D...
        We try to infer column meanings from content.
        """
        rows: list[dict[str, Any]] = []
        max_col = min(ws.max_column or 10, 30)

        # Use tag_rows from the profile
        tag_rows = profile.tag_rows if profile.tag_rows else []

        if not tag_rows:
            # Try to find tag rows by scanning column A
            for r in range(1, (ws.max_row or 0) + 1):
                v = ws.cell(r, 1).value
                if v is not None:
                    from spir_dynamic.utils.cell_utils import looks_like_tag
                    if looks_like_tag(v):
                        tag_rows.append(r)

        for r in tag_rows:
            tag_val = str(ws.cell(r, 1).value).strip()
            tags = split_tags(tag_val)

            # Read data from columns to the right
            values: list[Any] = []
            for c in range(2, max_col + 1):
                v = ws.cell(r, c).value
                if v is not None:
                    values.append(clean_str(v) or clean_num(v))

            # Build a basic row from available data
            for tag in tags:
                row: dict[str, Any] = {
                    "sheet": profile.name,
                    "spir_no": spir_no or profile.metadata.get("spir_no"),
                    "tag_no": tag,
                    "manufacturer": profile.metadata.get("manufacturer"),
                }

                # Try to assign values based on position
                if len(values) >= 1:
                    row["desc"] = values[0] if isinstance(values[0], str) else None
                if len(values) >= 2:
                    row["qty_identical"] = clean_num(values[1])
                if len(values) >= 3:
                    row["mfr_part_no"] = values[2] if isinstance(values[2], str) else None

                if "spir_type" in profile.metadata:
                    row["spir_type"] = profile.metadata["spir_type"]

                rows.append(row)

        return rows
