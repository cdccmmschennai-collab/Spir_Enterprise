"""Tests for position number and SPF number generation."""
from spir_dynamic.extraction.post_processor import (
    PositionEngine,
    SheetTracker,
    build_spf_number,
    post_process_rows,
)


class TestPositionEngine:
    def test_first_spare_gets_0010(self):
        engine = PositionEngine()
        assert engine.next("TAG-001", is_spare=True) == "0010"

    def test_increments_by_10(self):
        engine = PositionEngine()
        engine.next("TAG-001", is_spare=True)
        assert engine.next("TAG-001", is_spare=True) == "0020"
        assert engine.next("TAG-001", is_spare=True) == "0030"

    def test_header_always_0010(self):
        engine = PositionEngine()
        engine.next("TAG-001", is_spare=True)  # 0010
        engine.next("TAG-001", is_spare=True)  # 0020
        assert engine.next("TAG-001", is_spare=False) == "0010"
        # Counter should not advance
        assert engine.next("TAG-001", is_spare=True) == "0030"

    def test_new_tag_resets(self):
        engine = PositionEngine()
        engine.next("TAG-001", is_spare=True)  # 0010
        engine.next("TAG-001", is_spare=True)  # 0020
        assert engine.next("TAG-002", is_spare=True) == "0010"

    def test_cross_sheet_continuation(self):
        engine = PositionEngine()
        for _ in range(24):
            engine.next("TAG-001", is_spare=True)
        # 24th spare = 0240
        assert engine.next("TAG-001", is_spare=True) == "0250"


class TestSheetTracker:
    def test_first_sheet_gets_index_1(self):
        tracker = SheetTracker()
        assert tracker.get_sheet_idx("Sheet1") == 1

    def test_second_sheet_gets_index_2(self):
        tracker = SheetTracker()
        tracker.get_sheet_idx("Sheet1")
        assert tracker.get_sheet_idx("Sheet2") == 2

    def test_same_sheet_same_index(self):
        tracker = SheetTracker()
        idx = tracker.get_sheet_idx("Sheet1")
        assert tracker.get_sheet_idx("Sheet1") == idx

    def test_line_numbers_increment(self):
        tracker = SheetTracker()
        assert tracker.next_line("Sheet1") == 1
        assert tracker.next_line("Sheet1") == 2
        assert tracker.next_line("Sheet1") == 3


class TestBuildSpfNumber:
    def test_always_18_chars(self):
        result = build_spf_number("VEN-4460-KAHS-5-43-1002-2", 1, 1)
        assert len(result) == 18

    def test_sheet1_suffix_format(self):
        result = build_spf_number("VEN-4460-KAHS-5-43-1002-2", 1, 1)
        assert result.endswith("-L01")

    def test_sheet1_line10(self):
        result = build_spf_number("VEN-4460-KAHS-5-43-1002-2", 1, 10)
        assert len(result) == 18
        assert "-L10" in result

    def test_sheet2_suffix_format(self):
        result = build_spf_number("VEN-4460-KAHS-5-43-1002-2", 2, 1)
        assert len(result) == 18
        assert "-1L1" in result

    def test_short_spir_number(self):
        result = build_spf_number("1234-5678", 1, 1)
        assert len(result) == 18


class TestPostProcessRows:
    def test_assigns_position_numbers(self):
        from spir_dynamic.extraction.output_schema import CI, make_empty_row

        rows = []
        for i in range(3):
            row = make_empty_row()
            row[CI["TAG NO"]] = "TAG-001"
            row[CI["ITEM NUMBER"]] = i + 1
            row[CI["SHEET"]] = "Sheet1"
            rows.append(row)

        result = post_process_rows(rows, "TEST-SPIR-001")

        assert result[0][CI["POSITION NUMBER"]] == "0010"
        assert result[1][CI["POSITION NUMBER"]] == "0020"
        assert result[2][CI["POSITION NUMBER"]] == "0030"

    def test_assigns_spf_numbers(self):
        from spir_dynamic.extraction.output_schema import CI, make_empty_row

        row = make_empty_row()
        row[CI["TAG NO"]] = "TAG-001"
        row[CI["ITEM NUMBER"]] = 1
        row[CI["SHEET"]] = "Sheet1"

        result = post_process_rows([row], "VEN-4460-KAHS-5-43")

        spf = result[0][CI["OLD MATERIAL NUMBER/SPF NUMBER"]]
        assert spf is not None
        assert len(spf) == 18
