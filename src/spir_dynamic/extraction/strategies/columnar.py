"""
Columnar extraction strategy.

Handles SPIR matrix layout where:
  - Row 1 has equipment TAG numbers as column headers
  - Rows 2-7 have per-tag metadata (model, serial, eqpt_qty)
  - A header row defines item data columns (ITEM NUMBER, DESCRIPTION, etc.)
  - Data rows have item details + "1" markers in tag columns indicating
    which tags use which items (interchangeability flags)

Continuation sheets extend the tag columns horizontally. They share the
same items as the main sheet — only the tag-to-item mapping differs.

For each tag, the output is:
  - 1 header row: equipment metadata (EQPT MAKE, MODEL, SR NO, QTY, equipment desc)
  - N detail rows: one per applicable item with full item data
"""
from __future__ import annotations

import logging
import re
from typing import Any

from spir_dynamic.models.sheet_profile import SheetProfile
from spir_dynamic.utils.cell_utils import (
    clean_str,
    clean_num,
    is_placeholder,
    split_tags,
    looks_like_tag,
)
from spir_dynamic.analysis.header_detector import is_footer_row

log = logging.getLogger(__name__)


def _split_separated_value(val: str, expected_count: int) -> list[str]:
    """
    Split a separated value (serial number, model, etc.) into individual parts.
    Handles: slash "665426 / 665427", comma "A, B, C", "to" range "100 to 102".
    Returns list of parts. If not separable, returns [val] * expected_count.
    """
    s = val.strip()
    # Try slash separator
    if "/" in s:
        parts = [p.strip() for p in s.split("/") if p.strip()]
        if len(parts) == expected_count:
            return parts
    # Try comma separator
    if "," in s:
        parts = [p.strip() for p in s.split(",") if p.strip()]
        if len(parts) == expected_count:
            return parts
    # Try "to" separator
    if " to " in s.lower():
        parts = [p.strip() for p in re.split(r"\s+to\s+", s, flags=re.IGNORECASE)]
        if len(parts) == expected_count:
            return parts
    # Not separable — same value for all
    return [s] * expected_count


class ColumnarStrategy:
    """Extract from sheets with tags as column headers (SPIR matrix)."""

    def extract(
        self,
        ws,
        profile: SheetProfile,
        spir_no: str,
        items_dict: dict[int, dict[str, Any]] | None = None,
        item_col: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Extract data from a columnar/matrix sheet.

        Args:
            ws: worksheet
            profile: analyzed SheetProfile
            spir_no: SPIR document number
            items_dict: Pre-extracted items from the main/item-source sheet.
                        If None, this sheet IS the item source — read items from it.
                        If provided, this is a continuation sheet — use items_dict
                        for item data and only read tag-to-item mappings.
            item_col: Column index for ITEM NUMBER (from main sheet).
                      Passed to continuation sheets that lack their own header row.
        """
        rows: list[dict[str, Any]] = []

        if not profile.tag_columns:
            log.warning("ColumnarStrategy: no tag_columns for '%s'", profile.name)
            return rows

        # Step 1: Read tag values from tag columns (row 1)
        tag_info = self._read_tag_headers(ws, profile)
        if not tag_info:
            log.warning("ColumnarStrategy: no tags found in '%s'", profile.name)
            return rows

        # Filter out false tags that match the SPIR number
        if spir_no:
            spir_clean = spir_no.strip().upper()
            tag_info = {
                col: tags for col, tags in tag_info.items()
                if not any(t.strip().upper() == spir_clean for t in tags)
            }
            if not tag_info:
                log.warning("ColumnarStrategy: all tags were SPIR number in '%s'", profile.name)
                return rows

        # Step 2: Read per-tag metadata (model, serial, qty from rows 2-7)
        tag_metadata = self._read_tag_metadata(ws, profile, tag_info)

        # Step 3: Determine if we need to read items from this sheet
        is_item_source = items_dict is None
        if is_item_source:
            items_dict = self._read_items(ws, profile)

        # Step 4: Read tag-to-item mapping (which tags use which items)
        tag_items_map = self._read_tag_item_mapping(ws, profile, tag_info, item_col=item_col)

        # Step 5: Build output rows — header + details per tag
        global_meta = profile.metadata
        sheet_name = profile.name.upper()

        # EQPT MAKE from global metadata (top-right of SPIR sheet)
        global_mfr = global_meta.get("manufacturer")

        for col, tag_list in tag_info.items():
            for tag in tag_list:
                tmeta = tag_metadata.get(tag, {})

                # Equipment description (from first item or generic)
                eqpt_desc = global_meta.get("equipment") or tmeta.get("equipment")

                # Header row (equipment metadata, no ITEM NUMBER)
                header_row: dict[str, Any] = {
                    "sheet": sheet_name,
                    "spir_no": spir_no or global_meta.get("spir_no"),
                    "tag_no": tag,
                    "manufacturer": global_mfr,
                    "model": tmeta.get("model"),
                    "serial": tmeta.get("serial"),
                    "eqpt_qty": tmeta.get("eqpt_qty"),
                    "desc": eqpt_desc,
                    "spir_type": global_meta.get("spir_type"),
                    # No item_num, no supplier — this is a header row
                }
                rows.append(header_row)

                # Detail rows — one per applicable item
                applicable_items = tag_items_map.get(col, {})
                for item_num, per_tag_qty in applicable_items.items():
                    item = items_dict.get(item_num, {})
                    if not item:
                        continue

                    detail_row: dict[str, Any] = {
                        "sheet": sheet_name,
                        "spir_no": spir_no or global_meta.get("spir_no"),
                        "tag_no": tag,
                        # EQPT fields — eqpt_qty only on header rows
                        "manufacturer": global_mfr,
                        "model": tmeta.get("model"),
                        "serial": tmeta.get("serial"),
                        # No eqpt_qty on detail rows (reference shows it only on headers)
                        # Item data — per-tag quantity from tag column cell
                        "item_num": item_num,
                        "qty_identical": per_tag_qty,
                        "desc": item.get("desc"),
                        "dwg_no": item.get("dwg_no"),
                        "mfr_part_no": item.get("mfr_part_no"),
                        "material_spec": item.get("material_spec"),
                        "supplier_name": item.get("supplier_name"),
                        "currency": item.get("currency"),
                        "unit_price": item.get("unit_price"),
                        "delivery": item.get("delivery"),
                        "min_max": item.get("min_max"),
                        "uom": item.get("uom"),
                        "sap_no": item.get("sap_no"),
                        "classification": item.get("classification"),
                        "spir_type": global_meta.get("spir_type"),
                    }

                    # Build NEW DESCRIPTION = desc + part_no + supplier
                    parts = [
                        detail_row.get("desc"),
                        detail_row.get("mfr_part_no"),
                        detail_row.get("supplier_name"),
                    ]
                    new_parts = [p for p in parts if p]
                    if new_parts:
                        detail_row["new_desc"] = " | ".join(new_parts)

                    rows.append(detail_row)

        log.info(
            "ColumnarStrategy: %d rows from '%s' (%d tags, %d items, source=%s)",
            len(rows), profile.name,
            sum(len(t) for t in tag_info.values()),
            len(items_dict) if items_dict else 0,
            "self" if is_item_source else "external",
        )
        return rows

    def read_items(
        self, ws, profile: SheetProfile
    ) -> dict[int, dict[str, Any]]:
        """Public method to read items from this sheet (used by unified_extractor)."""
        return self._read_items(ws, profile)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_tag_headers(
        self, ws, profile: SheetProfile
    ) -> dict[int, list[str]]:
        """
        Read tag values from tag columns in the top rows.
        Returns {column_index: [tag1, tag2, ...]} (handles multi-tag cells).
        """
        result: dict[int, list[str]] = {}

        # Label keywords that should NOT be treated as tags
        _SKIP_KEYWORDS = frozenset({
            "equipment", "tag no", "tag number", "equip", "mfr", "model",
            "manufacturer", "supplier", "serial", "ser no", "no. of units",
            "item number", "description", "spare parts", "note", "or tag",
        })

        _ANNEXURE_PAT = re.compile(
            r"(?i)(?:refer\s+)?annexure[\s\-_]*(?:\([^)]*\)[\s\-_]*)?\d+"
        )

        for col in profile.tag_columns:
            for r in range(1, min(9, (ws.max_row or 0) + 1)):
                v = ws.cell(r, col).value
                if v is None or is_placeholder(v):
                    continue
                raw = str(v).strip()
                if not raw:
                    continue

                # Skip known labels, column numbers, and long text
                raw_lower = raw.lower()
                if any(kw in raw_lower for kw in _SKIP_KEYWORDS):
                    continue
                if raw.isdigit() and len(raw) <= 2:
                    continue
                if len(raw) > 50:
                    continue
                if raw == "_":
                    continue

                # Check for annexure reference
                if _ANNEXURE_PAT.match(raw):
                    result[col] = [raw]
                    break

                # Accept as tag — split if comma/slash separated
                tags = split_tags(raw)
                if tags:
                    result[col] = tags
                break

        return result

    def _read_tag_metadata(
        self,
        ws,
        profile: SheetProfile,
        tag_info: dict[int, list[str]],
    ) -> dict[str, dict[str, Any]]:
        """
        Read per-tag metadata from rows between row 1 and the header row.
        Looks for model, serial, eqpt_qty by scanning label cells in cols 1-3.
        """
        metadata: dict[str, dict[str, Any]] = {}
        header_row = profile.header_row or 8

        meta_keywords = {
            "model": ["model", "type", "mfr type", "type or model"],
            "serial": ["serial", "ser no", "mfr ser", "ser'l", "serial no"],
            "eqpt_qty": ["no. of units", "no of units", "units", "qty"],
            "manufacturer": ["manufacturer", "make", "mfr name", "mfr"],
        }

        for r in range(1, min(header_row + 4, 15)):
            # Check cols 1-3 for label text
            row_field = None
            for c in range(1, min(4, (ws.max_column or 3) + 1)):
                v = ws.cell(r, c).value
                if v is None:
                    continue
                cl = str(v).lower().strip()
                for field, kws in meta_keywords.items():
                    if any(kw in cl for kw in kws):
                        row_field = field
                        break
                if row_field:
                    break

            if not row_field:
                continue

            # Read the value from each tag column
            for col, tags in tag_info.items():
                v = ws.cell(r, col).value
                if row_field == "eqpt_qty":
                    val = clean_num(v)
                    if val is not None:
                        val = int(val)
                else:
                    val = clean_str(v)

                # Don't store annexure references as metadata values
                if val and re.match(r"(?i)(?:refer\s+)?annexure", str(val)):
                    val = None

                if val is None:
                    continue

                # Serial number separation matching:
                # If tag column has multiple tags and serial contains separators,
                # split and assign 1:1 (tag count == serial count)
                if row_field == "serial" and len(tags) > 1:
                    serials = _split_separated_value(str(val), len(tags))
                    for i, tag in enumerate(tags):
                        if tag not in metadata:
                            metadata[tag] = {}
                        if i < len(serials):
                            metadata[tag]["serial"] = serials[i]
                    continue

                for tag in tags:
                    if tag not in metadata:
                        metadata[tag] = {}
                    metadata[tag][row_field] = val

        # Fallback: when eqpt_qty is missing but column has multiple tags,
        # use the tag count as eqpt_qty (e.g., 8 comma-separated tags → eqpt_qty=8)
        for col, tags in tag_info.items():
            if len(tags) > 1:
                for tag in tags:
                    if tag and tag in metadata and metadata[tag].get("eqpt_qty") is None:
                        metadata[tag]["eqpt_qty"] = len(tags)
                    elif tag and tag not in metadata:
                        metadata.setdefault(tag, {})["eqpt_qty"] = len(tags)

        return metadata

    def _read_items(
        self, ws, profile: SheetProfile
    ) -> dict[int, dict[str, Any]]:
        """
        Read all items from this sheet (descriptions, prices, part numbers).
        Returns {item_number: {desc, dwg_no, mfr_part_no, ...}}.
        Only the item-source sheet (main sheet) has actual item data.
        """
        items: dict[int, dict[str, Any]] = {}

        start_row = profile.data_start_row or (
            (profile.header_row + 1) if profile.header_row else 8
        )
        end_row = profile.data_end_row or (ws.max_row or 0)

        for r in range(start_row, end_row + 1):
            shared = self._read_shared_fields(ws, r, profile)

            # Need at least item_number to be valid
            item_num_raw = shared.get("item_number")
            if item_num_raw is None:
                continue

            try:
                item_num = int(float(item_num_raw))
            except (ValueError, TypeError):
                continue

            # Check for footer
            desc = shared.get("description") or ""
            if desc and is_footer_row(desc):
                break

            items[item_num] = {
                "desc": shared.get("description"),
                "qty_identical": shared.get("quantity"),
                "dwg_no": shared.get("dwg_no"),
                "mfr_part_no": shared.get("part_number"),
                "material_spec": shared.get("material_spec"),
                "supplier_name": shared.get("supplier"),
                "currency": shared.get("currency"),
                "unit_price": shared.get("unit_price"),
                "delivery": shared.get("delivery_weeks"),
                "min_max": shared.get("min_max"),
                "uom": shared.get("uom"),
                "sap_no": shared.get("sap_number"),
                "classification": shared.get("classification"),
            }

        log.debug("Read %d items from '%s'", len(items), profile.name)
        return items

    def _read_tag_item_mapping(
        self,
        ws,
        profile: SheetProfile,
        tag_info: dict[int, list[str]],
        item_col: int | None = None,
    ) -> dict[int, dict[int, int]]:
        """
        Read which items each tag column applies to, with per-tag quantities.

        The cell value in a tag column is the per-tag spare quantity (e.g. 2
        means "2 of this item per equipment unit"). The TOTAL column sums these
        across all tags.

        Returns {column_index: {item_num: per_tag_qty, ...}}.
        """
        mapping: dict[int, dict[int, int]] = {col: {} for col in tag_info}

        start_row = profile.data_start_row or (
            (profile.header_row + 1) if profile.header_row else 8
        )
        end_row = profile.data_end_row or (ws.max_row or 0)

        # Find item number column — use override if provided (from main sheet)
        if item_col is None:
            item_col = profile.column_map.get("item_number")

        # If no mapped item_number column, try to find it by header text
        if item_col is None:
            for r in range(max(1, (profile.header_row or 6) - 1), min(10, (ws.max_row or 0) + 1)):
                for c in range(1, min(10, (ws.max_column or 10) + 1)):
                    v = ws.cell(r, c).value
                    if v and "item" in str(v).lower() and "number" in str(v).lower():
                        item_col = c
                        break
                if item_col:
                    break

        # Fallback: find column with sequential integers (1, 2, 3...) in data rows.
        # Continuation sheets often lack an "ITEM NUMBER" header and may have
        # data_start_row set too early. Scan a wider range to find the actual
        # item number column and adjust start_row accordingly.
        if item_col is None:
            tag_col_set = set(tag_info.keys())
            for search_start in range(start_row, min(start_row + 6, end_row)):
                for c in range(1, min(10, (ws.max_column or 10) + 1)):
                    if c in tag_col_set:
                        continue
                    try:
                        v1 = ws.cell(search_start, c).value
                        v2 = ws.cell(search_start + 1, c).value
                        if (v1 is not None and v2 is not None
                                and int(float(v1)) == 1 and int(float(v2)) == 2):
                            item_col = c
                            start_row = search_start  # adjust to actual data start
                            break
                    except (ValueError, TypeError):
                        continue
                if item_col:
                    break

        for r in range(start_row, end_row + 1):
            # Get item number for this row
            item_num = None
            if item_col:
                raw = ws.cell(r, item_col).value
                if raw is not None:
                    try:
                        item_num = int(float(raw))
                    except (ValueError, TypeError):
                        continue
            if item_num is None:
                continue

            # Check each tag column — read the per-tag quantity
            for col in tag_info:
                v = ws.cell(r, col).value
                if v is not None and not is_placeholder(v):
                    try:
                        per_tag_qty = int(float(v))
                    except (ValueError, TypeError):
                        per_tag_qty = 1

                    # When column has multiple tags and the cell value equals
                    # the tag count, the value is the TOTAL across all tags
                    # (not per-tag). Distribute evenly: qty_per_tag = total / count.
                    tag_count = len(tag_info.get(col, []))
                    if tag_count > 1 and per_tag_qty > 0 and per_tag_qty % tag_count == 0:
                        per_tag_qty = per_tag_qty // tag_count

                    mapping[col][item_num] = per_tag_qty

        return mapping

    def _read_shared_fields(
        self, ws, row: int, profile: SheetProfile
    ) -> dict[str, Any]:
        """Read shared (non-tag) fields from a data row using column_map."""
        item: dict[str, Any] = {}
        for field, col in profile.column_map.items():
            if field == "tag":
                continue
            raw = ws.cell(row, col).value
            if field in ("quantity", "unit_price", "total_price", "delivery_weeks", "eqpt_qty", "min_max"):
                item[field] = clean_num(raw)
            else:
                item[field] = clean_str(raw)
        return item
