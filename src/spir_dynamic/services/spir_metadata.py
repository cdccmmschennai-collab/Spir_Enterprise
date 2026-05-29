"""
Presentation-layer metadata for Excel output columns.

Maps each display column name to its SPIR internal field reference and character
limit, exactly as defined in the QatarEnergy SPIR template specification.

Used exclusively by excel_builder.py.
The extraction pipeline must NOT import this module.

DISPLAY LIMITS vs DATABASE LIMITS
----------------------------------
display_limit  → visual metadata in row 3 of the output sheet (SPIR spec reference)
db_limit       → backend VARCHAR size; use 255 for all text columns EXCEPT
                 OLD MATERIAL NUMBER/SPF NUMBER which stays at 18.
"""
from __future__ import annotations

COLUMN_METADATA: dict[str, dict] = {
    "S.NO": {
        "spir_field": "NA",
        "display_limit": 4,
    },
    "SPIR NO": {
        "spir_field": "CDC_SPIRNUMBER",
        "display_limit": 25,
    },
    "TAG NO": {
        "spir_field": "EQFNR",
        "display_limit": 30,
    },
    "EQPT MAKE": {
        "spir_field": "MFRNR",
        "display_limit": 30,
    },
    "EQPT MODEL": {
        "spir_field": "HERST",
        "display_limit": 35,
    },
    "EQPT SR NO": {
        "spir_field": "SERGE",
        "display_limit": 30,
    },
    "EQPT QTY": {
        "spir_field": "MENGE",
        "display_limit": 50,
    },
    "QUANTITY IDENTICAL PARTS FITTED": {
        "spir_field": "MENGE",
        "display_limit": 50,
    },
    "ITEM NUMBER": {
        "spir_field": "NA",
        "display_limit": "NA",
    },
    "POSITION NUMBER": {
        "spir_field": "POSNR",
        "display_limit": 4,
    },
    "OLD MATERIAL NUMBER/SPF NUMBER": {
        "spir_field": "BISMT",
        "display_limit": 18,
        # db_limit intentionally stays 18 — matches Access field constraint
    },
    "DESCRIPTION OF PARTS": {
        "spir_field": "MAKTX",
        "display_limit": 255,
    },
    "NEW DESCRIPTION OF PARTS": {
        "spir_field": "MAKTX",
        "display_limit": 40,
    },
    "DWG NO INCL POSN NO": {
        "spir_field": "NA",
        "display_limit": 255,
    },
    "MANUFACTURER PART NUMBER": {
        "spir_field": "MFPRN",
        "display_limit": 35,
    },
    "MATERIAL SPECIFICATION": {
        "spir_field": "NA",
        "display_limit": 255,
    },
    "SUPPLIER/ OCM NAME": {
        "spir_field": "MFRNR",
        "display_limit": 30,
    },
    "CURRENCY": {
        "spir_field": "WAERS",
        "display_limit": 3,
    },
    "UNIT PRICE": {
        "spir_field": "VERPR",
        "display_limit": 11,
    },
    "UNIT PRICE (QAR)": {
        "spir_field": "VERPR",
        "display_limit": 11,
    },
    "DELIVERY TIME IN WEEKS": {
        "spir_field": "LEADTIME",
        "display_limit": 40,
    },
    "MIN MAX STOCK LVLS QTY": {
        "spir_field": "EISBE",
        "display_limit": None,
    },
    "UNIT OF MEASURE": {
        "spir_field": "MEINS(T006)",
        "display_limit": 3,
    },
    "SAP NUMBER": {
        "spir_field": "SAP_MATNR",
        "display_limit": 8,
    },
    "CLASSIFICATION OF PARTS": {
        "spir_field": "NA",
        "display_limit": 255,
    },
    "ERROR": {
        "spir_field": "ERROR",
        "display_limit": 255,
    },
    "SHEET": {
        "spir_field": "NA",
        "display_limit": 255,
    },
    "SPIR TYPE": {
        "spir_field": "NA",
        "display_limit": 255,
    },
    "VENDOR NAME": {
        "spir_field": "VENDOR NAME",
        "display_limit": 255,
    },
    "VENDOR EMAIL1": {
        "spir_field": "VENDOR EMAIL1",
        "display_limit": 255,
    },
    "VENDOR EMAIL2": {
        "spir_field": "VENDOR EMAIL2",
        "display_limit": 255,
    },
    "VENDOR CONTACT NO": {
        "spir_field": "VENDOR CONTACT NO",
        "display_limit": 255,
    },
    "VENDOR COUNTRY": {
        "spir_field": "VENDOR COUNTRY",
        "display_limit": 255,
    },
}
