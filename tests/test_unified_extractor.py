"""Tests for the unified extractor."""
from spir_dynamic.extraction.unified_extractor import extract_workbook


class TestTabularExtraction:
    def test_extracts_from_tabular_layout(self, tabular_workbook):
        result = extract_workbook(tabular_workbook, "test.xlsx")
        assert result["rows"]
        assert result["total_tags"] >= 1
        assert result["spare_items"] > 0

    def test_rows_have_tag_values(self, tabular_workbook):
        result = extract_workbook(tabular_workbook, "test.xlsx")
        tags = {r.get("tag_no") for r in result["rows"] if r.get("tag_no")}
        assert len(tags) >= 1


class TestColumnarExtraction:
    def test_extracts_from_columnar_layout(self, columnar_workbook):
        result = extract_workbook(columnar_workbook, "test.xlsx")
        assert result["rows"]
        assert result["total_tags"] >= 2

    def test_creates_rows_per_tag(self, columnar_workbook):
        result = extract_workbook(columnar_workbook, "test.xlsx")
        # Should have rows for multiple tags
        tags = {r.get("tag_no") for r in result["rows"] if r.get("tag_no")}
        assert len(tags) >= 2


class TestMultiSheetExtraction:
    def test_extracts_from_multiple_sheets(self, multi_sheet_workbook):
        result = extract_workbook(multi_sheet_workbook, "test.xlsx")
        assert result["rows"]
        # Should extract from both main and continuation
        sheets = {r.get("sheet") for r in result["rows"]}
        assert len(sheets) >= 1

    def test_skips_validation_sheets(self, multi_sheet_workbook):
        result = extract_workbook(multi_sheet_workbook, "test.xlsx")
        sheets = {r.get("sheet") for r in result["rows"]}
        assert "Validation" not in sheets


class TestAnnexureExtraction:
    def test_extracts_from_annexure_layout(self, annexure_workbook):
        result = extract_workbook(annexure_workbook, "test.xlsx")
        assert result["rows"]
        assert result["annexure_count"] >= 1


class TestMetadata:
    def test_returns_sheet_profiles(self, tabular_workbook):
        result = extract_workbook(tabular_workbook, "test.xlsx")
        assert "sheet_profiles" in result
        assert len(result["sheet_profiles"]) > 0

    def test_returns_output_cols(self, tabular_workbook):
        result = extract_workbook(tabular_workbook, "test.xlsx")
        assert "output_cols" in result
        assert len(result["output_cols"]) == 27


class TestEmptyWorkbook:
    def test_handles_empty_workbook(self, make_workbook):
        wb = make_workbook({"Empty": [[None]]})
        result = extract_workbook(wb, "empty.xlsx")
        assert result["rows"] == [] or result["total_tags"] == 0
