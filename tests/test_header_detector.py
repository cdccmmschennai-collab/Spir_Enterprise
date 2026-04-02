"""Tests for the header detection module."""
from spir_dynamic.analysis.header_detector import (
    find_header_row,
    find_metadata,
    find_data_end,
    is_footer_row,
)


class TestFindHeaderRow:
    def test_finds_header_in_first_row(self, make_workbook):
        wb = make_workbook({
            "Sheet1": [
                ["Tag No", "Description", "Quantity", "Unit Price"],
                ["A-001", "Part 1", 5, 100],
            ],
        })
        ws = wb["Sheet1"]
        assert find_header_row(ws) == 1

    def test_finds_header_on_later_row(self, make_workbook):
        wb = make_workbook({
            "Sheet1": [
                ["SPIR Document"],
                ["Project: ABC"],
                ["Manufacturer: XYZ"],
                [None],
                [None],
                [None],
                ["Item Number", "Description", "Quantity", "Unit Price", "Part Number"],
                [1, "Part 1", 5, 100, "PN-001"],
            ],
        })
        ws = wb["Sheet1"]
        result = find_header_row(ws)
        assert result == 7

    def test_returns_none_for_empty_sheet(self, make_workbook):
        wb = make_workbook({"Sheet1": [[None]]})
        ws = wb["Sheet1"]
        assert find_header_row(ws) is None

    def test_requires_minimum_two_keywords(self, make_workbook):
        wb = make_workbook({
            "Sheet1": [
                ["Just Description"],
                ["Data here"],
            ],
        })
        ws = wb["Sheet1"]
        # Single keyword shouldn't match
        assert find_header_row(ws) is None


class TestFindMetadata:
    def test_extracts_spir_number(self, make_workbook):
        wb = make_workbook({
            "Sheet1": [
                ["SPIR No:", "VEN-4460-KAHS-5-43"],
                ["Equipment:", "Centrifugal Pump"],
                [None],
                ["Description", "Quantity"],
            ],
        })
        ws = wb["Sheet1"]
        meta = find_metadata(ws, header_row=4)
        assert meta.get("spir_no") == "VEN-4460-KAHS-5-43"

    def test_extracts_equipment(self, make_workbook):
        wb = make_workbook({
            "Sheet1": [
                ["Equipment Description:", "Centrifugal Pump"],
                ["Manufacturer:", "Flowserve"],
                [None],
                ["Description", "Quantity"],
            ],
        })
        ws = wb["Sheet1"]
        meta = find_metadata(ws, header_row=4)
        assert meta.get("equipment") == "Centrifugal Pump"
        assert meta.get("manufacturer") == "Flowserve"


class TestFooterDetection:
    def test_recognizes_footer_markers(self):
        assert is_footer_row("Project Manager: John") is True
        assert is_footer_row("Notes: Important") is True
        assert is_footer_row("Signature") is True
        assert is_footer_row("Bearing assembly") is False
        assert is_footer_row("") is False

    def test_find_data_end_stops_at_footer(self, make_workbook):
        wb = make_workbook({
            "Sheet1": [
                ["Description", "Quantity"],
                ["Part 1", 5],
                ["Part 2", 3],
                ["Notes: End of data"],
            ],
        })
        ws = wb["Sheet1"]
        end = find_data_end(ws, header_row=1)
        assert end == 3  # Row 4 is footer, so data ends at row 3
