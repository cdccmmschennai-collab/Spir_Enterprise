"""
Saipem SPIR output profile — the exact, ordered export columns.

Each column has a stable internal id (unique, used in row dicts) and a
three-level header taken from the Saipem form:

    band   – source group heading   (e.g. QUANTITY FOR 2 YEARS OPERATION)
    field  – the field label        (e.g. RECOMMEND. By VENDOR)
    sub    – the source sub-header  (e.g. Quantity for PM)

Repeated labels (the 2-year and capital-spares quantity fields) stay
distinguishable through their band and id. Band labels found in the workbook
replace the defaults below, so a renamed group keeps its source heading.

Only these columns are exported — no Planning Plant, QAR, OMN/SPF, ERROR or
other generic-only fields.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Band keys → default heading (overridden by the workbook's own band label)
BAND_EQUIPMENT = "equipment"
BAND_SPARE = "spare"
BAND_OPERATING = "op2y"
BAND_CAPITAL = "cap"

DEFAULT_BAND_LABELS: dict[str, str] = {
    BAND_EQUIPMENT: "EQUIPMENT IDENTIFICATION",
    BAND_SPARE: "SPARE PARTS IDENTIFICATION",
    BAND_OPERATING: "QUANTITY FOR 2 YEARS OPERATION",
    BAND_CAPITAL: "CAPITAL SPARES",
}


@dataclass(frozen=True)
class SaipemColumn:
    id: str
    field: str
    sub: Optional[str] = None
    band: Optional[str] = None        # band key, see DEFAULT_BAND_LABELS
    identifier: bool = False          # exported as text (leading zeros, codes)
    width: int = 16


_C = SaipemColumn

SAIPEM_COLUMNS: list[SaipemColumn] = [
    # -- Equipment metadata --
    _C("spir_no", "SPIR No.", identifier=True, width=26),
    _C("item_tag_no", "ITEM TAG No. (MAIN ITEM)", band=BAND_EQUIPMENT, identifier=True, width=20),
    _C("free_field", "Free Field (Second TAG Codification or Item Criticality)",
       band=BAND_EQUIPMENT, identifier=True, width=24),
    _C("serial_no", "SERIAL No.", band=BAND_EQUIPMENT, identifier=True, width=18),
    _C("model_or_type", "MODEL OR TYPE", band=BAND_EQUIPMENT, identifier=True, width=20),
    _C("equipment_sub_item_no", "EQUIPMENT SUB ITEM No.", band=BAND_EQUIPMENT, identifier=True, width=18),

    # -- Units and installed quantities (kept apart from every spare quantity) --
    _C("no_of_units", "No. OF UNITS", width=10),
    _C("qty_installed_per_unit",
       "QUANTITY OF PARTS INSTALLED PER ONE UNIT OF EQUIPMENT SHOWN IN COLUMN A-E", width=16),
    _C("total_qty_identical_installed", "TOTAL QUANTITY OF IDENTICAL PARTS INSTALLED", width=16),

    # -- Spare parts identification --
    _C("spir_pos_no", "SPIR POS. No. (from 0001)", band=BAND_SPARE, identifier=True, width=10),
    _C("description_of_parts", "DESCRIPTION OF PARTS", band=BAND_SPARE, width=40),
    # Computed: DESCRIPTION OF PARTS,ORIGINAL MAKERS PART No.,ORIGINAL MAKERS NAME,MATERIAL
    _C("new_description_of_parts", "NEW DESCRIPTION OF PARTS", band=BAND_SPARE, width=60),
    _C("material", "MATERIAL", band=BAND_SPARE, width=28),
    _C("original_makers_name", "ORIGINAL MAKERS NAME", band=BAND_SPARE, width=28),
    _C("original_makers_part_no", "ORIGINAL MAKERS PART No.", band=BAND_SPARE, identifier=True, width=24),
    _C("rel_doc_drawing_no", "RELEVANT DOCUMENT No.", "DRAWING No.", BAND_SPARE, True, 20),
    _C("rel_doc_page", "RELEVANT DOCUMENT No.", "PAGE", BAND_SPARE, True, 8),
    _C("rel_doc_pos_no", "RELEVANT DOCUMENT No.", "POS. No.", BAND_SPARE, True, 12),
    _C("vendor_code_no", "VENDOR CODE No.", band=BAND_SPARE, identifier=True, width=24),
    _C("miap_code", "Business unit codification (Miap code)", band=BAND_SPARE, identifier=True, width=18),

    # -- Quantity for 2 years operation --
    _C("unit_of_meas", "UNIT of MEAS.", band=BAND_OPERATING, width=10),
    # Total = PM + CM (Instruction sheet: "Recommend by Vendor: Total quantity")
    _C("op2y_vendor_total", "RECOMMEND. By VENDOR", "Total quantity", BAND_OPERATING, width=12),
    _C("op2y_vendor_pm", "RECOMMEND. By VENDOR", "Quantity for PM", BAND_OPERATING, width=12),
    _C("op2y_vendor_cm", "RECOMMEND. By VENDOR", "Quantity for CM", BAND_OPERATING, width=12),
    _C("op2y_engineer", "ENGINEER. RECOMMEND.", band=BAND_OPERATING, width=12),
    _C("op2y_operation", "OPERATION RECOMMEND.", band=BAND_OPERATING, width=12),
    _C("op2y_purchased", "PURCHASED QUANTITY (approved)", band=BAND_OPERATING, width=12),

    # -- Capital spares (same labels, different group) --
    _C("cap_vendor", "RECOMMEND. By VENDOR", band=BAND_CAPITAL, width=12),
    _C("cap_engineer", "ENGINEER. RECOMMEND.", band=BAND_CAPITAL, width=12),
    _C("cap_operation", "OPERATION RECOMMEND.", band=BAND_CAPITAL, width=12),
    _C("cap_purchased", "PURCHASED QUANTITY (approved)", band=BAND_CAPITAL, width=12),

    # -- Part attributes --
    _C("weight", "WEIGHT", width=10),
    _C("characteristic_dimension", "CHARACTERISTIC DIMENSION", width=14),
    _C("obsolescence", "OBSOLESCENCE", width=12),
    _C("gs", "G/S", width=6),
    _C("shelf_life", "SHELF LIFE", width=10),
    _C("price_unit", "PRICE", "UNIT", width=12),
    _C("price_amount", "PRICE", "AMOUNT", width=12),
    _C("price_currency", "PRICE", "CURRENCY", width=10),
    _C("delivery_time_weeks", "DELIVERY TIME (IN WEEKS)", width=12),

    # -- Vendor --
    _C("vendor_name", "VENDOR'S NAME", width=30),
    _C("vendor_address", "ADDRESS", width=40),
    _C("vendor_phone_fax", "PHONE No. / FAX No.", identifier=True, width=22),
    _C("vendor_email", "E-MAIL ADDRESS", width=28),
]

COLUMN_BY_ID: dict[str, SaipemColumn] = {c.id: c for c in SAIPEM_COLUMNS}

# Spare-line fields computed from other values rather than read from a column
# (price_currency and the vendor total are read when a column exists).
COMPUTED_FIELD_IDS: frozenset[str] = frozenset(
    {"op2y_vendor_total", "price_currency", "new_description_of_parts"}
)

# Fields read from the spare-part matrix (everything between the equipment
# block and the vendor block, except the computed ones).
MATRIX_FIELD_IDS: frozenset[str] = frozenset(
    c.id for c in SAIPEM_COLUMNS[SAIPEM_COLUMNS.index(COLUMN_BY_ID["spir_pos_no"]):
                                 SAIPEM_COLUMNS.index(COLUMN_BY_ID["vendor_name"])]
) - COMPUTED_FIELD_IDS

# NEW DESCRIPTION OF PARTS = these values joined with ",", in the Standard
# output's NEW DESCRIPTION order (desc, part no, supplier, material);
# blank / "-" / "NA" parts skipped.
NEW_DESCRIPTION_FIELD_IDS = (
    "description_of_parts", "original_makers_part_no", "original_makers_name", "material",
)

EQUIPMENT_FIELD_IDS = ("free_field", "model_or_type", "equipment_sub_item_no")
VENDOR_FIELD_IDS = ("vendor_name", "vendor_address", "vendor_phone_fax", "vendor_email")


def header_parts(col: SaipemColumn, band_labels: dict[str, str]) -> tuple[Optional[str], str, Optional[str]]:
    """(band label, field label, sub label) for one column."""
    band = band_labels.get(col.band) or DEFAULT_BAND_LABELS.get(col.band) if col.band else None
    return band, col.field, col.sub


def flat_label(col: SaipemColumn, band_labels: dict[str, str]) -> str:
    """Single-line unique header for the web preview: 'BAND | FIELD | SUB'."""
    return " | ".join(p for p in header_parts(col, band_labels) if p)
