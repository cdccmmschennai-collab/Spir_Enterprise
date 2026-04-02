"""Tests for the tag location detection module."""
from spir_dynamic.analysis.tag_locator import locate_tags
from spir_dynamic.models.sheet_profile import TagLayout


class TestTagColumn:
    def test_detects_tag_column(self, make_workbook):
        wb = make_workbook({
            "Sheet1": [
                ["Tag No", "Description", "Quantity"],
                ["30-P-001", "Part 1", 5],
                ["30-P-002", "Part 2", 3],
                ["30-P-003", "Part 3", 1],
            ],
        })
        ws = wb["Sheet1"]
        result = locate_tags(ws, header_row=1, column_map={"tag": 1})
        assert result.layout == TagLayout.TAG_COLUMN
        assert result.tag_column_index == 1

    def test_no_tag_column_when_empty(self, make_workbook):
        wb = make_workbook({
            "Sheet1": [
                ["Tag No", "Description"],
                [None, "Part 1"],
                [None, "Part 2"],
            ],
        })
        ws = wb["Sheet1"]
        result = locate_tags(ws, header_row=1, column_map={"tag": 1})
        # No tag values in the column
        assert result.layout != TagLayout.TAG_COLUMN


class TestColumnHeaders:
    def test_detects_tags_as_column_headers(self, make_workbook):
        wb = make_workbook({
            "Sheet1": [
                [None, None, "30-GV-146", "30-GV-171", "30-GV-169"],
                [None, None, None, None, None],
                ["Description", "Unit Price", "Qty", "Qty", "Qty"],
                ["Bearing", 100, 2, 3, 1],
            ],
        })
        ws = wb["Sheet1"]
        result = locate_tags(ws, header_row=3, column_map={})
        assert result.layout == TagLayout.COLUMN_HEADERS
        assert len(result.tag_columns) >= 2

    def test_single_tag_not_column_headers(self, make_workbook):
        wb = make_workbook({
            "Sheet1": [
                [None, None, "30-GV-146"],
                ["Description", "Quantity", "Qty"],
                ["Bearing", 100, 2],
            ],
        })
        ws = wb["Sheet1"]
        result = locate_tags(ws, header_row=2, column_map={})
        # Single tag column shouldn't trigger COLUMN_HEADERS
        assert result.layout != TagLayout.COLUMN_HEADERS


class TestGlobalTag:
    def test_detects_single_global_tag(self, make_workbook):
        wb = make_workbook({
            "Sheet1": [
                ["SPIR: ABC-123"],
                ["Equipment Tag: 30-P-001"],
                [None],
                ["Description", "Quantity", "Unit Price"],
                ["Bearing", 2, 150],
            ],
        })
        ws = wb["Sheet1"]
        result = locate_tags(ws, header_row=4, column_map={})
        assert result.layout == TagLayout.GLOBAL_TAG
        assert "30-P-001" in result.global_tag


class TestNoTags:
    def test_returns_none_when_no_tags(self, make_workbook):
        wb = make_workbook({
            "Sheet1": [
                ["Description", "Quantity"],
                ["Bearing", 5],
                ["Seal", 3],
            ],
        })
        ws = wb["Sheet1"]
        result = locate_tags(ws, header_row=1, column_map={})
        assert result.layout == TagLayout.NONE
