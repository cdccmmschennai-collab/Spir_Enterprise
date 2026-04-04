"""Tests for position number and OMN/SPF number generation."""
import pytest

from spir_dynamic.extraction.post_processor import (
    SheetTracker,
    build_omn,
    post_process_rows,
    _clean_spir_base,
    _build_suffix,
    _item_to_line_index,
)


# ---------------------------------------------------------------------------
# _clean_spir_base
# ---------------------------------------------------------------------------

class TestCleanSpirBase:
    def test_removes_letters(self):
        assert _clean_spir_base("VEN-4460-KAHS-5-43-1002-2") == "4460-5-43-1002-2"

    def test_removes_brackets(self):
        assert _clean_spir_base("4400-VP-30-00-10-053 (REV.2)") == "4400-3010-53"

    def test_removes_rev_suffix(self):
        result = _clean_spir_base("VEN-4460-DGTYP-4-43-0004 (Normal) Rev. A")
        assert result == "4460-4-43-0004"

    def test_preserves_leading_zeros(self):
        """Step 1 must NOT strip leading zeros — that's only for trimming."""
        result = _clean_spir_base("VEN-4391-MTY-5-43-0006")
        assert "0006" in result

    def test_empty_input(self):
        assert _clean_spir_base("") == ""
        assert _clean_spir_base(None) == ""


# ---------------------------------------------------------------------------
# _build_suffix
# ---------------------------------------------------------------------------

class TestBuildSuffix:
    def test_single_sheet_line1(self):
        assert _build_suffix(1, 1, total_main_sheets=1) == "L01"

    def test_single_sheet_line10(self):
        assert _build_suffix(1, 10, total_main_sheets=1) == "L10"

    def test_single_sheet_line100(self):
        assert _build_suffix(1, 100, total_main_sheets=1) == "L100"

    def test_multi_sheet1_line1(self):
        assert _build_suffix(1, 1, total_main_sheets=3) == "1L1"

    def test_multi_sheet2_line1(self):
        assert _build_suffix(2, 1, total_main_sheets=3) == "2L1"

    def test_multi_sheet3_line24(self):
        assert _build_suffix(3, 24, total_main_sheets=3) == "3L24"


# ---------------------------------------------------------------------------
# _item_to_line_index
# ---------------------------------------------------------------------------

class TestItemToLineIndex:
    def test_0010(self):
        assert _item_to_line_index("0010") == 10

    def test_0020(self):
        assert _item_to_line_index("0020") == 20

    def test_0100(self):
        assert _item_to_line_index("0100") == 100

    def test_0750(self):
        assert _item_to_line_index("0750") == 750

    def test_1000(self):
        assert _item_to_line_index("1000") == 1000

    def test_integer_input(self):
        assert _item_to_line_index(30) == 30

    def test_invalid_fallback(self):
        assert _item_to_line_index("abc") == 1
        assert _item_to_line_index(None) == 1


# ---------------------------------------------------------------------------
# SheetTracker
# ---------------------------------------------------------------------------

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

    def test_continuation_inherits_main_index(self):
        tracker = SheetTracker()
        idx = tracker.get_sheet_idx("Sheet1")
        assert tracker.get_sheet_idx("Continuation Sheet") == idx

    def test_annexure_inherits_main_index(self):
        tracker = SheetTracker()
        idx = tracker.get_sheet_idx("Sheet1")
        assert tracker.get_sheet_idx("Annexure 1") == idx

    def test_total_main_sheets_single(self):
        tracker = SheetTracker(main_sheet_names={"Sheet1"})
        assert tracker.total_main_sheets == 1

    def test_total_main_sheets_multiple(self):
        tracker = SheetTracker(main_sheet_names={"Sheet1", "Sheet2", "Sheet3"})
        assert tracker.total_main_sheets == 3

    def test_total_excludes_continuation(self):
        # Continuation listed alongside main must not increase total_main_sheets.
        tracker = SheetTracker(
            main_sheet_names={"Sheet1", "Continuation Sheet"}
        )
        assert tracker.total_main_sheets == 1

    def test_continuation_in_name_set_still_inherits_main_index(self):
        names = {
            "MAIN SHEET_P1_SH 1",
            "CONTINUATION SHEET_P1_SH 2",
            "MAIN SHEET_P1_SH 3",
        }
        tracker = SheetTracker(main_sheet_names=names)
        assert tracker.total_main_sheets == 2
        assert tracker.get_sheet_idx("MAIN SHEET_P1_SH 1") == 1
        assert tracker.get_sheet_idx("CONTINUATION SHEET_P1_SH 2") == 1
        assert tracker.get_sheet_idx("MAIN SHEET_P1_SH 3") == 2


# ---------------------------------------------------------------------------
# build_omn
# ---------------------------------------------------------------------------

class TestBuildOmn:
    def test_always_18_chars(self):
        result = build_omn("VEN-4460-KAHS-5-43-1002-2", 1, 1, total_main_sheets=1)
        assert len(result) == 18

    def test_single_sheet_suffix_format(self):
        result = build_omn("VEN-4460-KAHS-5-43-1002-2", 1, 1, total_main_sheets=1)
        assert "-L01" in result

    def test_single_sheet_line10(self):
        result = build_omn("VEN-4460-KAHS-5-43-1002-2", 1, 10, total_main_sheets=1)
        assert len(result) == 18
        assert "-L10" in result or "L10" in result

    def test_multi_sheet_suffix_format(self):
        result = build_omn("VEN-4460-KAHS-5-43-1002-2", 1, 1, total_main_sheets=2)
        assert len(result) == 18
        assert "1L1" in result

    def test_multi_sheet2_suffix(self):
        result = build_omn("VEN-4460-KAHS-5-43-1002-2", 2, 1, total_main_sheets=2)
        assert len(result) == 18
        assert "2L1" in result

    def test_short_spir_number(self):
        result = build_omn("1234-5678", 1, 1, total_main_sheets=1)
        assert len(result) <= 18
        assert result == "1234-5678-L01"

    def test_line100(self):
        result = build_omn("VEN-4460-KAHS-5-43-1002-2", 1, 100, total_main_sheets=1)
        assert len(result) == 18
        assert "L100" in result

    def test_mty_spir_multi_main_item1_is_18(self):
        """VEN-4391-MTY-4-43-0001 → base without vendor/MTY; multi-main suffix 1L1."""
        result = build_omn(
            "VEN-4391-MTY-4-43-0001", 1, 1, total_main_sheets=2
        )
        assert len(result) == 18
        assert result == "4391-4-43-0001-1L1"

    def test_over_budget_fuses_hyphens_before_suffix(self):
        """Long base + 1L10 must land at 18 chars after base trim (fuse/LZ path)."""
        result = build_omn(
            "VEN-4391-MTY-4-43-0001", 1, 10, total_main_sheets=2
        )
        assert len(result) == 18
        assert result.endswith("1L10")

    def test_mewtp_single_main_abbrev_project(self):
        result = build_omn(
            "VEN-MEWTP-5-43-0007-3", 1, 1, total_main_sheets=1
        )
        assert len(result) == 18
        assert result == "ME-5-43-0007-3-L01"

    def test_mewtp_multi_main_abbrev_project(self):
        result = build_omn(
            "VEN-MEWTP-5-43-0007-3", 1, 1, total_main_sheets=2
        )
        assert len(result) == 18
        assert result == "ME-5-43-0007-3-1L1"

    def test_mewtp_multi_main_line10_shortens_project(self):
        result = build_omn(
            "VEN-MEWTP-5-43-0007-3", 1, 10, total_main_sheets=2
        )
        assert len(result) == 18
        assert result == "M-5-43-0007-3-1L10"

    def test_dgen_location_dropped_seq_digits_preserved(self):
        """Numeric project + DGEN location: no fake digits; 0116 kept at 18."""
        result = build_omn("VEN-4460-DGEN-5-43-0116", 1, 1, total_main_sheets=1)
        assert result == "4460-5-43-0116-L01"
        assert len(result) == 18


@pytest.mark.parametrize(
    "spir,sheet,line,mains,expected",
    [
        ("4400-VP-30-00-10-053-2", 1, 1, 1, "4400-3010-53-2-L01"),
        ("4400-VP-30-00-10-053-2", 1, 10, 1, "4400-3010-53-2-L10"),
        ("4400-VP-30-00-10-053-2", 1, 10, 2, "4400-3010-532-1L10"),
        ("4400-VP-30-00-10-053-2", 1, 100, 2, "4400-3010-5321L100"),
        ("VEN-MEWTP-5-43-0007-3", 1, 1, 1, "ME-5-43-0007-3-L01"),
        ("VEN-MEWTP-5-43-0007-3", 1, 10, 1, "ME-5-43-0007-3-L10"),
        ("VEN-MEWTP-5-43-0007-3", 1, 1, 2, "ME-5-43-0007-3-1L1"),
        ("VEN-MEWTP-5-43-0007-3", 1, 10, 2, "M-5-43-0007-3-1L10"),
        ("VEN-MEWTP-5-43-0007-3", 1, 100, 2, "M-5-43-007-3-1L100"),
        ("VEN-4391-MTY-5-43-0016-5", 1, 1, 1, "4391-5-43-16-5-L01"),
        ("VEN-4391-MTY-5-43-0016-5", 1, 1, 2, "4391-5-43-16-5-1L1"),
        ("VEN-4391-MTY-5-43-0016-5", 1, 10, 2, "4391-543-16-5-1L10"),
        ("VEN-4391-MTY-5-43-0016-5", 1, 100, 2, "4391-543-16-51L100"),
        ("VEN-4391-M4TY-2-43-0002-A", 1, 1, 1, "4391-2-43-0002-L01"),
        ("VEN-4391-M4TY-2-43-0002-A", 1, 10, 1, "4391-2-43-0002-L10"),
        ("VEN-4391-M4TY-2-43-0002-A", 1, 10, 2, "4391-2-43-002-1L10"),
        ("VEN-4391-M4TY-2-43-0002-A", 1, 100, 2, "4391-2-43-02-1L100"),
        (
            "VEN-4391-MTY-5-43-0001-REV.5_CODED COPY_1",
            1,
            1,
            1,
            "4391-5-43-0001-L01",
        ),
        (
            "VEN-4391-MTY-5-43-0001-REV.5_CODED COPY_1",
            1,
            10,
            1,
            "4391-5-43-0001-L10",
        ),
        (
            "VEN-4391-MTY-5-43-0001-REV.5_CODED COPY_1",
            1,
            1,
            2,
            "4391-5-43-0001-1L1",
        ),
        (
            "VEN-4391-MTY-5-43-0001-REV.5_CODED COPY_1",
            1,
            10,
            2,
            "4391-5-43-001-1L10",
        ),
        (
            "VEN-4391-MTY-5-43-0001-REV.5_CODED COPY_1",
            1,
            100,
            2,
            "4391-543-001-1L100",
        ),
        ("VEN-4142-RLCSF3-2-43-1300-A", 1, 1, 1, "4142-2-43-1300-L01"),
        ("VEN-4142-RLCSF3-2-43-1300-A", 1, 10, 1, "4142-2-43-1300-L10"),
        ("VEN-4142-RLCSF3-2-43-1300-A", 1, 1, 2, "4142-2-43-1300-1L1"),
        ("VEN-4142-RLCSF3-2-43-1300-A", 1, 10, 2, "4142-243-1300-1L10"),
        ("VEN-4142-RLCSF3-2-43-1300-A", 1, 100, 2, "4142-243-13001L100"),
    ],
)
def test_build_omn_user_example_matrix(spir, sheet, line, mains, expected):
    assert build_omn(spir, sheet, line, mains) == expected
    assert len(expected) <= 18


# ---------------------------------------------------------------------------
# post_process_rows (integration)
# ---------------------------------------------------------------------------

class TestPostProcessRows:
    def test_assigns_position_numbers(self):
        from spir_dynamic.extraction.output_schema import CI, make_empty_row

        rows = []
        for i in range(3):
            row = make_empty_row()
            row[CI["TAG NO"]] = "TAG-001"
            row[CI["ITEM NUMBER"]] = (i + 1) * 10  # 10, 20, 30
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
        row[CI["ITEM NUMBER"]] = 10  # item 0010 -> line index 1
        row[CI["SHEET"]] = "Sheet1"

        result = post_process_rows([row], "VEN-4460-KAHS-5-43")

        spf = result[0][CI["OLD MATERIAL NUMBER/SPF NUMBER"]]
        assert spf is not None
        assert len(spf) <= 18

    def test_item_number_determines_line_index(self):
        from spir_dynamic.extraction.output_schema import CI, make_empty_row

        row = make_empty_row()
        row[CI["TAG NO"]] = "TAG-001"
        row[CI["ITEM NUMBER"]] = 50  # item 50 -> line suffix L50
        row[CI["SHEET"]] = "Sheet1"

        result = post_process_rows([row], "VEN-4460-KAHS-5-43")
        spf = result[0][CI["OLD MATERIAL NUMBER/SPF NUMBER"]]
        assert "L50" in spf
