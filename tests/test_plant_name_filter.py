"""
Vendor-concatenated plant names are stripped from EQPT MAKE / EQPT MODEL.

Covers:
    - the VEN-4460-DGTYP-4-43-0309-5 pattern ("AB' GLM RIO PANEL-KHATIYA")
    - misspelt / short plant forms (KHATIYA -> Khatiyah Main)
    - suffix and prefix joins, parentheses, multi-word plants
    - values that are only a plant are cleared; ordinary models are untouched
    - the in-place column helper only touches the requested columns
"""
from __future__ import annotations

import pytest

from spir_dynamic.extraction.plant_name_filter import (
    is_plant_name,
    strip_plant_names,
    strip_plant_names_in_columns,
)


@pytest.mark.parametrize("raw, expected", [
    ("AB' GLM RIO PANEL-KHATIYA", "AB' GLM RIO PANEL"),
    ("AB' GLM RIO PANEL-FAHAHIL", "AB' GLM RIO PANEL"),
    ("AB' GLM RIO PANEL-JALEHA", "AB' GLM RIO PANEL"),
    ("AB' GLM RIO PANEL-DIYAB", "AB' GLM RIO PANEL"),
    ("AB' GLM RIO PANEL - Khatiyah North", "AB' GLM RIO PANEL"),
    ("RIO PANEL (DUKHAN)", "RIO PANEL"),
    ("RIO PANEL / Ras Laffan Industrial City", "RIO PANEL"),
    ("RIO PANEL HALUL ISLAND", "RIO PANEL"),
    ("KHATIYAH-RIO PANEL", "RIO PANEL"),
    ("RIO PANEL-KHATIYA-NORTH", "RIO PANEL"),
])
def test_concatenated_plant_is_stripped(raw, expected):
    assert strip_plant_names(raw) == expected


@pytest.mark.parametrize("raw", [
    "1794-ACNR15XT",
    "Rockwell Automation",
    "MAIN PUMP",
    "PANEL NORTH",
    "DN 50 PN 16",
    "3500/42M",
    "TYPE: MAIN-NORTH",
])
def test_ordinary_values_untouched(raw):
    assert strip_plant_names(raw) == raw


def test_plant_only_value_is_cleared():
    assert strip_plant_names("KHATIYA") is None
    assert strip_plant_names("Dukhan Fields") is None


def test_non_strings_pass_through():
    assert strip_plant_names(None) is None
    assert strip_plant_names(5700) == 5700


def test_short_forms_and_fuzzy():
    assert is_plant_name("KHATIYA")          # Khatiyah Main, one letter missing
    assert is_plant_name("halul")            # Halul Island
    assert not is_plant_name("MAIN")
    assert not is_plant_name("PANEL")


def test_column_helper_only_touches_given_columns():
    rows = [["X-KHATIYA", "PANEL-KHATIYA", "TAG-KHATIYA"]]
    strip_plant_names_in_columns(rows, (0, 1, None))
    assert rows == [["X", "PANEL", "TAG-KHATIYA"]]
