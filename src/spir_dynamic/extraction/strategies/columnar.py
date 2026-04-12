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


_NUMERIC_LIKE_CURRENCY_TOKENS = (
    "rs",
    "inr",
    "usd",
    "eur",
    "gbp",
    "aed",
    "sar",
    "sgd",
    "₹",
    "€",
    "£",
)


def _is_numeric_like_cell(raw: Any) -> bool:
    """
    Reject values that are "numeric" after stripping punctuation but are actually text.
    Used to prevent swapped column mappings (e.g., unit_price from description column).
    """
    if raw is None or is_placeholder(raw):
        return False
    s = str(raw).strip()
    if not s:
        return False
    if clean_num(raw) is None:
        return False

    # If letters exist, only allow common currency tokens at the end.
    if re.search(r"[A-Za-z]", s):
        # Allow only digits/punctuation/spaces + optional trailing currency token.
        allowed_currency = "|".join(_NUMERIC_LIKE_CURRENCY_TOKENS).replace(".", r"\.")
        return bool(
            re.match(
                rf"^\s*[\d\.,\-\(\)\s/]+(?:\s*(?:{allowed_currency}))?\s*$",
                s,
                flags=re.IGNORECASE,
            )
        )
    return True


def _is_text_heavy_cell(raw: Any) -> bool:
    """Description-like cells should contain alphabetic content."""
    if raw is None or is_placeholder(raw):
        return False
    s = str(raw).strip()
    if len(s) < 3:
        return False
    return bool(re.search(r"[A-Za-z]", s))



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
        print(f"DEBUG _columnar.extract: sheet={profile.name!r} is_item_source={'YES' if items_dict is None else 'NO'} items_dict_len={len(items_dict) if items_dict else 0}")

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
        global_supplier = global_meta.get("supplier")

        # PHASE 4 FIX: Extract currency from unit price column header if not
        # in a separate column. E.g., "UNIT PRICE (USD)" → currency="USD"
        global_currency = global_meta.get("currency")
        if not global_currency and profile.column_map.get("unit_price"):
            up_col = profile.column_map["unit_price"]
            # Scan rows around the header row to find currency text
            # (currency may be in row above or below the detected header)
            header_row_idx = profile.header_row
            if header_row_idx:
                for scan_r in range(max(1, header_row_idx - 3), header_row_idx + 3):
                    up_header = str(ws.cell(scan_r, up_col).value or "").upper()
                    if not up_header.strip():
                        continue
                    # Look for currency codes in parentheses: "UNIT PRICE (USD)"
                    currency_match = re.search(r"\(([A-Z]{3})\)", up_header)
                    if currency_match:
                        global_currency = currency_match.group(1)
                        break
                    # Check for ": US $" or similar patterns
                    currency_match = re.search(r":\s*([A-Z\$]+)\s*\$", up_header)
                    if currency_match:
                        global_currency = "USD"
                        break
                    # Check for standalone currency codes
                    for code in ["USD", "QAR", "AED", "EUR", "GBP", "SAR", "INR"]:
                        if code in up_header:
                            global_currency = code
                            break
                    if global_currency:
                        break

        for col in sorted(tag_info):
            tag_list = tag_info[col]
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
                    "supplier": global_supplier,
                    "model": tmeta.get("model"),
                    "serial": tmeta.get("serial"),
                    "eqpt_qty": tmeta.get("eqpt_qty"),
                    "desc": eqpt_desc,
                    "spir_type": global_meta.get("spir_type"),
                }
                rows.append(header_row)

                # Detail rows — one per applicable item
                applicable_items = tag_items_map.get(col, {})
                print(f"DEBUG _columnar.extract: tag={tag!r} col={col} applicable_items={len(applicable_items)} is_item_source={is_item_source}")

                # For continuation sheets (items_dict was passed in from outside),
                # if a tag column has zero applicable items AND its cells are genuinely
                # blank (not explicit zeros — those are already filtered by Bug 2 fix),
                # inherit all items from items_dict with qty=1.
                if not applicable_items and not is_item_source and items_dict:
                    col_start = profile.data_start_row or (
                        (profile.header_row + 1) if profile.header_row else 8
                    )
                    col_end = profile.data_end_row or (ws.max_row or 0)
                    has_any_data = any(
                        ws.cell(r, col).value is not None
                        and not is_placeholder(ws.cell(r, col).value)
                        for r in range(col_start, col_end + 1)
                    )
                    if not has_any_data:
                        applicable_items = {num: 1 for num in items_dict}

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
                        "supplier": global_supplier,
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
                        # PHASE 4 FIX: Use global currency as fallback
                        "currency": item.get("currency") or global_currency,
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
                        detail_row["new_desc"] = ",".join(new_parts)

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

        PHASE 1 FIX: Expanded skip keywords to reject header text, column
        labels, and SPIR metadata that was being incorrectly extracted as tags.
        """
        result: dict[int, list[str]] = {}

        # Label keywords that should NOT be treated as tags
        # Expanded to catch all known SPIR header/column text patterns
        _SKIP_KEYWORDS = frozenset({
            # Equipment/tag labels
            "equipment", "tag no", "tag number", "equip", "mfr", "model",
            "manufacturer", "supplier", "serial", "ser no", "no. of units",
            "item number", "description", "spare parts", "note", "or tag",
            # Column header labels (PHASE 1: added)
            "dwg no", "drawing no", "part number", "material spec",
            "material specification", "material certification",
            "supplier/ocm", "ocm name", "unit price", "currency",
            "delivery", "lead time", "uom", "unit of measure",
            "sap number", "sap no", "classification", "min max",
            "stock level", "identical parts", "total no",
            "qty identical", "quantity", "spare parts list",
            "interchangeability", "remarks", "spir number",
            "ref indicator", "authority block", "required on site",
            "purchase order", "po number", "project", "contract",
            "engineering", "reminder", "technical data", "signature",
            "requisition", "prepared by", "checked by", "approved by",
            "revision", "end of", "company",
            # Section labels
            "manufacturers/suppliers data", "vendor data",
            "note 1", "note 2", "note 3", "note 4", "note 5",
            "see note", "attach", "attachments",
            # Row numbering labels
            "mfr type", "mfr ser", "type or model",
            "no. of parts", "parts per unit",
            # SPIR metadata labels
            "qatarenergy", "buyer", "purchaser", "contractor",
            "sub-contractor", "vendor", "fabricator",
        })

        # Patterns that look like column header references (e.g., "10A", "10B", "11A")
        _COLUMN_REF_PAT = re.compile(r"^\d+[A-Z]$")

        # Pattern for annexure references (including Roman numerals)
        _ANNEXURE_PAT = re.compile(
            r"(?i)(?:refer\s+)?annexure[\s\-_]*(?:\([^)]*\)[\s\-_]*)?(?:\d+|[IVX]+)\b"
        )

        # Pattern for actual equipment tags (must have at least one separator or be alphanumeric with structure)
        # FIX: Changed {2,} to {1,} to accept single-letter prefix tags like "V-8943", "E-8925"
        _TAG_LIKE_PAT = re.compile(r"[A-Z0-9]{1,}[-/][A-Z0-9]", re.IGNORECASE)

        def _normalize_tag_candidate(raw_value: Any) -> str:
            """Normalise packed multi-tag cells to separators preprocessing can expand."""
            raw_text = str(raw_value).strip()
            if not raw_text:
                return ""

            if "\n" in raw_text or "\r" in raw_text:
                nl_parts = [p.strip() for p in re.split(r"[\n\r]+", raw_text) if p.strip()]
                if len(nl_parts) > 1:
                    raw_text = ", ".join(nl_parts)

            # Some files separate full tags by repeated spaces instead of commas.
            # Convert only when we can clearly see 2+ tag-like tokens.
            if not re.search(r"[,/;|]", raw_text):
                tag_tokens = re.findall(r"[A-Z0-9]{1,}(?:[-/][A-Z0-9]+)+", raw_text, re.IGNORECASE)
                if len(tag_tokens) > 1:
                    compact = re.sub(r"\s+", " ", raw_text)
                    if compact == " ".join(tag_tokens):
                        raw_text = ", ".join(tag_tokens)

            return raw_text

        def _is_viable_tag_text(raw_text: str) -> bool:
            """Apply the same tag/header guards used for direct header-cell detection."""
            if not raw_text:
                return False

            raw_lower = raw_text.lower()
            if any(kw in raw_lower for kw in _SKIP_KEYWORDS):
                return False
            if raw_text.isdigit() and len(raw_text) <= 2:
                return False
            looks_like_packed = (
                len(raw_text) > 50
                and re.search(r"[A-Z0-9]{2,}[-/][A-Z0-9]", raw_text, re.IGNORECASE)
                and re.search(r"[,;/|]", raw_text)
            )
            if len(raw_text) > 50 and not looks_like_packed:
                return False
            if raw_text == "_":
                return False
            if _COLUMN_REF_PAT.match(raw_text):
                return False
            if re.search(r"(?i)(see note|pos\.?\s*\d|attachment)", raw_text):
                return False
            return True

        def _find_local_multi_tag_companions(
            anchor_row: int, anchor_col: int
        ) -> list[tuple[int, str]]:
            """
            Search a bounded 2D neighbourhood around an annexure cell for nearby
            real-tag companion columns that should be emitted in addition to the
            annexure reference group.
            """
            best_by_col: dict[int, tuple[int, str]] = {}

            row_start = max(1, anchor_row - 5)
            row_end = min(ws.max_row or anchor_row, anchor_row + 5)
            col_start = max(1, anchor_col - 3)
            col_end = min(ws.max_column or anchor_col, anchor_col + 3)

            for scan_row in range(row_start, row_end + 1):
                for scan_col in range(col_start, col_end + 1):
                    if scan_row == anchor_row and scan_col == anchor_col:
                        continue

                    cell_value = ws.cell(scan_row, scan_col).value
                    if cell_value is None or is_placeholder(cell_value):
                        continue

                    candidate = _normalize_tag_candidate(cell_value)
                    if not _is_viable_tag_text(candidate):
                        continue
                    if _ANNEXURE_PAT.match(candidate):
                        continue

                    split_candidate = split_tags(candidate)
                    has_multi_split = len(split_candidate) > 1
                    has_multi_whitespace = (
                        len(re.findall(r"[A-Z0-9]{1,}(?:[-/][A-Z0-9]+)+", candidate, re.IGNORECASE)) > 1
                    )
                    if not has_multi_split and not has_multi_whitespace:
                        continue
                    if not any(looks_like_tag(tag) for tag in split_candidate):
                        continue

                    distance = abs(scan_row - anchor_row) + abs(scan_col - anchor_col)
                    score = (10 if has_multi_split else 0) + len(split_candidate) - distance
                    current = best_by_col.get(scan_col)
                    if current is None or score > current[0]:
                        best_by_col[scan_col] = (score, candidate)

            return [
                (scan_col, candidate)
                for scan_col, (_, candidate) in sorted(best_by_col.items())
            ]

        for col in profile.tag_columns:
            for r in range(1, min(9, (ws.max_row or 0) + 1)):
                v = ws.cell(r, col).value
                if v is None or is_placeholder(v):
                    continue
                raw = _normalize_tag_candidate(v)
                if not raw:
                    continue

                # Skip known labels, column numbers, and long text
                raw_lower = raw.lower()
                if any(kw in raw_lower for kw in _SKIP_KEYWORDS):
                    continue
                if raw.isdigit() and len(raw) <= 2:
                    continue
                # PHASE 2 FIX: Allow long values if they look like packed tags
                # (comma/semicolon/newline-separated tag values from continuation sheets)
                looks_like_packed = (
                    len(raw) > 50
                    and re.search(r"[A-Z0-9]{2,}[-/][A-Z0-9]", raw, re.IGNORECASE)
                    and re.search(r"[,;/|]", raw)
                )
                if len(raw) > 50 and not looks_like_packed:
                    continue
                if raw == "_":
                    continue

                # PHASE 1 FIX: Reject column header references like "10A", "10B", "11A"
                if _COLUMN_REF_PAT.match(raw):
                    continue

                # PHASE 1 FIX: Reject values that look like section headers
                # (contain "see note", "pos", or are purely descriptive)
                if re.search(r"(?i)(see note|pos\.?\s*\d|attachment)", raw):
                    continue

                # Check for annexure reference (numbered: "Annexure 1", "Annexure I", etc.)
                if _ANNEXURE_PAT.match(raw):
                    result[col] = [raw]   # annexure_resolver expands this downstream
                    companion_cols = _find_local_multi_tag_companions(r, col)
                    for companion_col, companion_raw in companion_cols:
                        if companion_col in result:
                            continue
                        companion_tags = split_tags(companion_raw)
                        if not companion_tags:
                            continue
                        log.info(
                            "[COLUMNAR TAG OVERRIDE V2] sheet='%s' annexure_anchor=(r=%d,c=%d) companion_col=%d raw='%s'",
                            profile.name,
                            r,
                            col,
                            companion_col,
                            companion_raw,
                        )
                        result[companion_col] = companion_tags
                    break

                # Accept bare "Refer Annexure" / "Annexure" without a number.
                # _normalize_annexure_ref returns ANNEXURE_ANY for these, and
                # _enrich_equipment_data remaps ANNEXURE_ANY when exactly 1 annexure exists.
                if re.search(r"(?i)annex", raw):
                    result[col] = [raw]
                    break

                # PHASE 1 FIX: Validate that the value looks like an actual tag
                # Tags typically have structure: prefix-number, or annexure refs
                # Reject plain text that doesn't match tag patterns
                if not _TAG_LIKE_PAT.search(raw) and not _ANNEXURE_PAT.match(raw):
                    # Allow short alphanumeric codes that could be tag suffixes
                    # but reject anything that looks like header text
                    if len(raw) > 20 or " " in raw:
                        continue

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
            "model": ["model number", "model no", "model", "mfr type", "type or model"],
            "serial": ["serial number", "serial no", "serial", "ser no", "mfr ser", "ser'l"],
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

        # PHASE 2 FIX: Fallback for eqpt_qty when "No. OF UNITS" is missing.
        # - Multi-tag cell (e.g., "TAG-1, TAG-2, TAG-3") → eqpt_qty = tag count
        # - Single tag cell with no units row → eqpt_qty = 1
        # This handles SPIR files where the units row is absent or in a
        # different position than expected.
        for col, tags in tag_info.items():
            tag_count = len(tags) if tags else 1
            for tag in tags if tags else [None]:
                if tag is None:
                    continue
                if tag in metadata:
                    if metadata[tag].get("eqpt_qty") is None:
                        metadata[tag]["eqpt_qty"] = tag_count
                else:
                    metadata.setdefault(tag, {})["eqpt_qty"] = tag_count

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

                    if per_tag_qty <= 0:
                        continue

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
                # Enforce numeric-like cells for numeric fields.
                if _is_numeric_like_cell(raw):
                    item[field] = clean_num(raw)
                else:
                    item[field] = None
            elif field == "description":
                if _is_text_heavy_cell(raw):
                    item[field] = clean_str(raw)
                else:
                    item[field] = None
            else:
                item[field] = clean_str(raw)
        return item
