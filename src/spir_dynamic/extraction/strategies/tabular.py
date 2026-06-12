"""
Tabular extraction strategy.

Handles sheets where tags are in a dedicated column (TAG_COLUMN)
or where a single tag applies to the whole sheet (GLOBAL_TAG).

This is the most common layout: a standard data table with a header row,
columns for description/qty/price/etc., and a tag column or global tag.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from spir_dynamic.models.sheet_profile import SheetProfile, TagLayout
from spir_dynamic.utils.cell_utils import clean_str, clean_num, is_placeholder, split_tags
from spir_dynamic.analysis.header_detector import is_footer_row
from spir_dynamic.utils.logging import timed

log = logging.getLogger(__name__)


def _split_serial_for_tags(serial_val: str | None, tag_count: int) -> list:
    """Split a serial range string into one value per tag.

    Mirrors the separator logic in _split_serial_range (unified_extractor),
    applied at the point where multi-tag cells are expanded into rows.
    """
    if not serial_val or tag_count <= 1:
        return [serial_val] * tag_count
    s = str(serial_val).strip()
    # "X to Y"
    parts = re.split(r"\s+to\s+", s, flags=re.IGNORECASE)
    if len(parts) == tag_count:
        return [p.strip() for p in parts]
    # "/" separator
    if "/" in s:
        parts = [p.strip() for p in s.split("/") if p.strip()]
        if len(parts) == tag_count:
            return parts
    # "," separator
    if "," in s:
        parts = [p.strip() for p in s.split(",") if p.strip()]
        if len(parts) == tag_count:
            return parts
    # Numeric hyphen range: "240430-240431" or "240458-240459-240460"
    if "-" in s:
        parts = [p.strip() for p in s.split("-") if p.strip()]
        if len(parts) == tag_count and all(p.isdigit() for p in parts):
            return parts
    return [serial_val] * tag_count


# Fields that should be read as numbers
_NUMERIC_FIELDS = frozenset(
    {"quantity", "unit_price", "total_price", "eqpt_qty", "min_max"}
)


class TabularStrategy:
    """Extract from sheets with tags in a column or a global tag."""

    @timed
    def extract(
        self,
        ws,
        profile: SheetProfile,
        spir_no: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        if not profile.column_map:
            log.warning("TabularStrategy: no column_map for '%s'", profile.name)
            return rows

        start_row = profile.data_start_row or (
            (profile.header_row + 1) if profile.header_row else 2
        )
        end_row = profile.data_end_row or (ws.max_row or 0)

        # Debug: log detected layout and tag column info
        log.info(
            "TabularStrategy: sheet='%s' layout=%s tag_col=%s tag_col_idx=%s "
            "global_tag=%s data_rows=%d-%d",
            profile.name,
            profile.tag_layout.value,
            profile.tag_column_index,
            profile.column_map.get("tag"),
            profile.global_tag,
            start_row, end_row,
        )

        for r in range(start_row, end_row + 1):
            # Skip completely blank rows
            if self._is_blank_row(ws, r, profile.column_map):
                continue

            # Read all mapped fields
            item: dict[str, Any] = {"sheet": profile.name}

            for field, col in profile.column_map.items():
                raw = ws.cell(r, col).value
                if field in _NUMERIC_FIELDS:
                    item[field] = clean_num(raw)
                else:
                    item[field] = clean_str(raw)

            # Discard serial values with no digits — authority-block text
            # fragments ("IT", "UN", "No. OF UNITS") are not real serials.
            sv = item.get("serial") or ""
            if sv and not any(c.isdigit() for c in sv):
                item["serial"] = None

            # Skip column-number stub rows: appear between header and data,
            # contain field-reference numbers ("8", "10A") as description.
            desc_s = item.get("description") or ""
            if (not item.get("item_number") and desc_s and
                    len(desc_s) <= 4 and re.match(r'^[0-9A-Za-z]+$', desc_s)):
                continue

            # Check for footer
            desc = item.get("description") or ""
            if desc and is_footer_row(desc):
                break

            # Skip rows with no description and no part number,
            # UNLESS the row is a tag-header row carrying equipment metadata
            # (model/serial/eqpt_qty). These rows are needed so that
            # _enrich_equipment_data can carry model/serial forward to item rows.
            if not item.get("description") and not item.get("part_number"):
                tag_raw = None
                if profile.tag_layout == TagLayout.TAG_COLUMN and profile.tag_column_index:
                    tag_raw = ws.cell(r, profile.tag_column_index).value
                has_equip_data = any(
                    item.get(f) for f in ("model", "serial", "eqpt_qty", "manufacturer")
                )
                if not (tag_raw and not is_placeholder(tag_raw) and has_equip_data):
                    continue

            # Apply tag
            if profile.tag_layout == TagLayout.GLOBAL_TAG and profile.global_tag:
                if not item.get("tag"):
                    item["tag"] = profile.global_tag
            elif profile.tag_layout == TagLayout.TAG_COLUMN and profile.tag_column_index:
                tag_val = ws.cell(r, profile.tag_column_index).value
                if tag_val is not None and not is_placeholder(tag_val):
                    item["tag"] = str(tag_val).strip()

            # Add SPIR NO
            item["spir_no"] = spir_no or profile.metadata.get("spir_no")

            # Add SPIR type if in metadata
            if "spir_type" in profile.metadata:
                item["spir_type"] = profile.metadata["spir_type"]

            # Expand multi-tag values
            raw_tag = item.get("tag")
            tags = split_tags(raw_tag) if raw_tag else [None]
            serials = _split_serial_for_tags(item.get("serial"), len(tags))

            for i, tag in enumerate(tags):
                row = dict(item)
                row["tag"] = tag
                row["serial"] = serials[i]
                # Map field names to output schema field names
                row["tag_no"] = tag
                row["desc"] = row.pop("description", None)
                row["mfr_part_no"] = row.pop("part_number", None)
                row["qty_identical"] = row.pop("quantity", None)
                row["item_num"] = row.pop("item_number", None)
                row["supplier_name"] = row.pop("supplier", None)
                row["sap_no"] = row.pop("sap_number", None)
                row["delivery"] = row.pop("delivery_weeks", None)

                # Compute total_price if missing
                qty = clean_num(row.get("qty_identical"))
                price = clean_num(row.get("unit_price"))
                if row.get("total_price") is None and qty and price:
                    row["total_price"] = round(qty * price, 4)

                # Build new_desc from desc + part + supplier
                parts = [
                    row.get("desc"),
                    row.get("mfr_part_no"),
                    row.get("supplier_name"),
                ]
                new_desc_parts = [p for p in parts if p]
                if new_desc_parts:
                    row["new_desc"] = ",".join(new_desc_parts)

                rows.append(row)

        # Apply global metadata model/serial as fallback for rows that still lack it.
        # Handles files where model is in the sheet's header area (not the data table).
        global_model = profile.metadata.get("model")
        global_serial = profile.metadata.get("serial")
        if global_model or global_serial:
            for row in rows:
                if global_model and not row.get("model"):
                    row["model"] = global_model
                if global_serial and not row.get("serial"):
                    row["serial"] = global_serial

        # Propagate metadata currency to all rows when the sheet has no currency column.
        # Handles files where currency appears as metadata (e.g. "Currency: US$" at row 7)
        # rather than as a per-row column value.
        if "currency" not in (profile.column_map or {}) and profile.metadata.get("currency"):
            _meta_currency = profile.metadata["currency"]
            # Normalize symbol-style currency values to ISO codes (e.g. "US$" → "USD")
            _CURRENCY_NORM = {"US$": "USD", "$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY"}
            _meta_currency = _CURRENCY_NORM.get(_meta_currency, _meta_currency)
            for row in rows:
                if not row.get("currency"):
                    row["currency"] = _meta_currency

        # Apply eqpt_qty from metadata to spare rows that lack it — but NOT
        # for GLOBAL_TAG sheets. There, eqpt_qty belongs only on the
        # synthesized equipment row (added below), not on each spare row.
        global_eqpt_qty = clean_num(profile.metadata.get("eqpt_qty"))
        if global_eqpt_qty is not None and profile.tag_layout != TagLayout.GLOBAL_TAG:
            for row in rows:
                if row.get("eqpt_qty") is None:
                    row["eqpt_qty"] = global_eqpt_qty

        # For GLOBAL_TAG sheets, prepend one equipment row per tag synthesized
        # from sheet metadata. Equipment rows have no item_num and represent
        # the equipment itself, not a spare part.
        if profile.tag_layout == TagLayout.GLOBAL_TAG and profile.global_tag:
            equip_rows: list[dict[str, Any]] = []
            for tag in split_tags(profile.global_tag):
                eq: dict[str, Any] = {
                    "sheet": profile.name,
                    "tag": tag,
                    "tag_no": tag,
                    "spir_no": spir_no or profile.metadata.get("spir_no"),
                    "manufacturer": profile.metadata.get("manufacturer"),
                    "model": profile.metadata.get("model"),
                    "serial": profile.metadata.get("serial"),
                    "eqpt_qty": clean_num(profile.metadata.get("eqpt_qty")),
                    "desc": profile.metadata.get("equipment"),
                    "supplier_name": profile.metadata.get("supplier"),
                }
                if "spir_type" in profile.metadata:
                    eq["spir_type"] = profile.metadata["spir_type"]
                equip_rows.append(eq)
            rows = equip_rows + rows

        # Debug: log unique tag values seen in output
        unique_tags = {r.get("tag_no") for r in rows if r.get("tag_no")}
        log.info(
            "TabularStrategy: sheet='%s' extracted %d rows, unique_tag_no=%d sample=%s",
            profile.name, len(rows), len(unique_tags), sorted(unique_tags)[:10],
        )
        return rows

    def _is_blank_row(
        self, ws, row: int, column_map: dict[str, int]
    ) -> bool:
        """Check if all mapped columns are blank in this row."""
        for col in column_map.values():
            v = ws.cell(row, col).value
            if v is not None and str(v).strip():
                return False
        return True
