"""Tests for the full extraction pipeline."""
import io
import pytest
import openpyxl

from spir_dynamic.extraction.file_validator import validate_file, ValidationError


class TestFileValidator:
    def test_rejects_unsupported_extension(self):
        with pytest.raises(ValidationError, match="Unsupported"):
            validate_file("test.pdf", b"content")

    def test_rejects_empty_file(self):
        with pytest.raises(ValidationError, match="empty"):
            validate_file("test.xlsx", b"")

    def test_accepts_valid_xlsx(self):
        wb = openpyxl.Workbook()
        wb.active.cell(1, 1, "test")
        buf = io.BytesIO()
        wb.save(buf)
        # Should not raise
        validate_file("test.xlsx", buf.getvalue())

    def test_rejects_corrupt_xlsx(self):
        with pytest.raises(ValidationError, match="Cannot open"):
            validate_file("test.xlsx", b"not an excel file")


class TestCellUtils:
    def test_clean_str(self):
        from spir_dynamic.utils.cell_utils import clean_str
        assert clean_str("  hello  ") == "hello"
        assert clean_str(None) is None
        assert clean_str("n/a") is None
        assert clean_str("-") is None

    def test_clean_num(self):
        from spir_dynamic.utils.cell_utils import clean_num
        assert clean_num(42) == 42.0
        assert clean_num("3.14") == 3.14
        assert clean_num(None) is None
        assert clean_num("n/a") is None
        assert clean_num("$100.50") == 100.50

    def test_split_tags_simple(self):
        from spir_dynamic.utils.cell_utils import split_tags
        assert split_tags("A/B/C") == ["A", "B", "C"]
        assert split_tags("TAG-001") == ["TAG-001"]
        assert split_tags(None) == []
        assert split_tags("n/a") == []

    def test_split_tags_prefix_inheritance(self):
        from spir_dynamic.utils.cell_utils import split_tags
        result = split_tags("30-GV-146, 171, 169")
        assert result == ["30-GV-146", "30-GV-171", "30-GV-169"]

    def test_looks_like_tag(self):
        from spir_dynamic.utils.cell_utils import looks_like_tag
        assert looks_like_tag("30-P-001") is True
        assert looks_like_tag("30-GV-146") is True
        assert looks_like_tag("hello") is False
        assert looks_like_tag(None) is False


class TestOutputSchema:
    def test_output_cols_count(self):
        from spir_dynamic.extraction.output_schema import OUTPUT_COLS
        assert len(OUTPUT_COLS) == 27

    def test_make_empty_row(self):
        from spir_dynamic.extraction.output_schema import make_empty_row, OUTPUT_COLS
        row = make_empty_row()
        assert len(row) == len(OUTPUT_COLS)

    def test_row_from_dict(self):
        from spir_dynamic.extraction.output_schema import row_from_dict, CI
        row = row_from_dict({"spir_no": "TEST-001", "tag_no": "TAG-001"})
        assert row[CI["SPIR NO"]] == "TEST-001"
        assert row[CI["TAG NO"]] == "TAG-001"
