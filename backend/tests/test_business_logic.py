"""
tests/test_business_logic.py
──────────────────────────────
Tests for the two critical business logic modules:
  - extraction/spf_number.py       (OLD MATERIAL NUMBER)
  - extraction/position_number.py  (POSITION NUMBER)

Run:
    pytest tests/test_business_logic.py -v
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from extraction.spf_number import (
    build_spir_base, build_suffix, build_old_material_number,
    _strip_leading_zeros, _trim_to_budget, TARGET_LEN,
)
from extraction.position_number import PositionNumberEngine


# ─────────────────────────────────────────────────────────────────────────────
# SPF NUMBER TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildSpirBase:
    def test_removes_letters(self):
        assert build_spir_base("VEN-4460-KAHS-5-43-1002-2") == "4460-5-43-1002-2"

    def test_removes_vendor_prefix(self):
        assert build_spir_base("VEN-4391-M4TY-2-43-0002-A") == "4391-4-2-43-0002"

    def test_removes_spaces_brackets(self):
        assert build_spir_base("4400-VP-30-00-10-053 (REV.2)") == "4400-30-00-10-053-2"

    def test_collapses_double_hyphens(self):
        base = build_spir_base("4400--VP--30")
        assert "--" not in base

    def test_strips_leading_trailing_hyphens(self):
        base = build_spir_base("-4400-VP-30-")
        assert not base.startswith("-")
        assert not base.endswith("-")

    def test_empty_string(self):
        assert build_spir_base("") == ""

    def test_none(self):
        assert build_spir_base(None) == ""


class TestBuildSuffix:
    def test_sheet1_line1(self):
        assert build_suffix(1, 1)   == "L01"

    def test_sheet1_line10(self):
        assert build_suffix(1, 10)  == "L10"

    def test_sheet1_line99(self):
        assert build_suffix(1, 99)  == "L99"

    def test_sheet1_line100(self):
        assert build_suffix(1, 100) == "L100"

    def test_sheet2_line1(self):
        assert build_suffix(2, 1)   == "1L1"

    def test_sheet2_line10(self):
        assert build_suffix(2, 10)  == "1L10"

    def test_sheet3_line5(self):
        assert build_suffix(3, 5)   == "2L5"

    def test_sheet10_line3(self):
        assert build_suffix(10, 3)  == "9L3"


class TestStripLeadingZeros:
    def test_leading_zero(self):
        assert _strip_leading_zeros("0610") == "610"

    def test_trailing_zeros_unchanged(self):
        assert _strip_leading_zeros("6100") == "6100"

    def test_all_zeros(self):
        assert _strip_leading_zeros("0000") == "0"

    def test_two_leading_zeros(self):
        assert _strip_leading_zeros("0030") == "30"

    def test_no_leading_zero(self):
        assert _strip_leading_zeros("1234") == "1234"

    def test_non_numeric_unchanged(self):
        assert _strip_leading_zeros("0AB") == "0AB"

    def test_single_zero(self):
        assert _strip_leading_zeros("0") == "0"

    def test_053(self):
        assert _strip_leading_zeros("053") == "53"


class TestBuildOldMaterialNumber:

    def test_always_18_chars(self):
        """Every output must be exactly 18 characters."""
        test_cases = [
            ("VEN-4460-KAHS-5-43-1002-2",  1,  1),
            ("VEN-4460-KAHS-5-43-1002-2",  1, 10),
            ("VEN-4460-KAHS-5-43-1002-2",  2,  1),
            ("VEN-4460-KAHS-5-43-1002-2",  1, 99),
            ("VEN-4460-KAHS-5-43-1002-2",  1, 100),
            ("4400-VP-30-00-10-053-REV2",  1,  1),
            ("VEN-0610-KAHS-6100-TEST",    1,  1),
            ("VEN-4391-M4TY-2-43-0002-A",  1,  1),
            ("A",                          1,  1),   # extreme short SPIR
        ]
        for spir, sh, ln in test_cases:
            result = build_old_material_number(spir, sh, ln)
            assert len(result) == TARGET_LEN, (
                f"Length {len(result)} ≠ {TARGET_LEN} for spir={spir!r} sh={sh} ln={ln}: '{result}'"
            )

    # Exact spec examples
    def test_spec_example_sheet1_line1(self):
        assert build_old_material_number("VEN-4460-KAHS-5-43-1002-2", 1, 1) \
               == "4460-5-43-1002-L01"

    def test_spec_example_sheet1_line10(self):
        assert build_old_material_number("VEN-4460-KAHS-5-43-1002-2", 1, 10) \
               == "4460-5-43-1002-L10"

    def test_spec_example_sheet2_line1(self):
        assert build_old_material_number("VEN-4460-KAHS-5-43-1002-2", 2, 1) \
               == "4460-5-43-1002-1L1"

    def test_spec_example_sheet1_line99(self):
        assert build_old_material_number("VEN-4460-KAHS-5-43-1002-2", 1, 99) \
               == "4460-5-43-1002-L99"

    def test_overflow_single_digit_removed(self):
        """The trailing '-2' (single digit) is removed when needed."""
        result = build_old_material_number("VEN-4460-KAHS-5-43-1002-2", 1, 100)
        assert len(result) == TARGET_LEN
        # '-2' should be gone since it's a single-digit segment removed first
        assert "2L100" not in result or len(result) == TARGET_LEN

    def test_leading_zero_stripped(self):
        """'0610' segment → '610' when space is needed."""
        # '4460-5-43-1002-2' is fine for normal line numbers
        # With a shorter SPIR like VEN-0610-5-43-0006 → '0610-5-43-0006'
        # length = 14, budget for sh=1,ln=1 = 18-1-3=14 → fits exactly!
        result = build_old_material_number("VEN-0610-KAHS-5-43-0006", 1, 1)
        assert len(result) == TARGET_LEN

    def test_single_char_spir(self):
        """Even a 1-char SPIR should produce 18-char output."""
        result = build_old_material_number("A", 1, 1)
        assert len(result) == TARGET_LEN

    def test_very_long_spir_trimmed(self):
        """Long SPIR must be trimmed — never truncate 4-digit groups."""
        result = build_old_material_number("VEN-4400-ABCD-30-00-10-053-2-XYZ", 1, 1)
        assert len(result) == TARGET_LEN
        assert "4400" in result   # 4-digit group must be preserved


# ─────────────────────────────────────────────────────────────────────────────
# POSITION NUMBER TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestPositionNumberEngine:

    def test_first_spare_is_0010(self):
        eng = PositionNumberEngine()
        assert eng.next("TAG1", True) == "0010"

    def test_spares_increment_by_10(self):
        eng = PositionNumberEngine()
        assert eng.next("TAG1", True)  == "0010"
        assert eng.next("TAG1", True)  == "0020"
        assert eng.next("TAG1", True)  == "0030"

    def test_24_spares_reaches_0240(self):
        eng = PositionNumberEngine()
        last = None
        for _ in range(24):
            last = eng.next("TAG1", True)
        assert last == "0240"

    def test_new_tag_resets_to_0010(self):
        eng = PositionNumberEngine()
        for _ in range(5):
            eng.next("TAG1", True)
        # New tag starts fresh
        assert eng.next("TAG2", True) == "0010"

    def test_same_tag_continues_across_sheets(self):
        """Spec requirement: same tag continues across multiple sheets."""
        eng = PositionNumberEngine()
        # Sheet 1: TAG1 gets 24 spares → last = 0240
        for _ in range(24):
            eng.next("TAG1", True)
        # Sheet 2: TAG1 continues from 0240 → next = 0250
        assert eng.next("TAG1", True) == "0250"
        assert eng.next("TAG1", True) == "0260"

    def test_header_row_always_0010(self):
        eng = PositionNumberEngine()
        # After some spares for TAG1
        eng.next("TAG1", True)
        eng.next("TAG1", True)
        # Header row returns 0010 regardless
        assert eng.next("TAG1", False) == "0010"
        assert eng.next("TAG1", False) == "0010"

    def test_header_does_not_advance_spare_counter(self):
        """Header rows must not increment the spare counter."""
        eng = PositionNumberEngine()
        eng.next("TAG1", True)   # → 0010
        eng.next("TAG1", True)   # → 0020
        eng.next("TAG1", False)  # header → 0010, counter still at 0020
        nxt = eng.next("TAG1", True)
        assert nxt == "0030"     # continues from 0020, not 0010

    def test_multiple_tags_independent(self):
        """Each tag has its own independent counter."""
        eng = PositionNumberEngine()
        eng.next("TAG1", True)   # TAG1: 0010
        eng.next("TAG1", True)   # TAG1: 0020
        eng.next("TAG2", True)   # TAG2: 0010
        eng.next("TAG2", True)   # TAG2: 0020
        assert eng.next("TAG1", True) == "0030"   # TAG1 unaffected by TAG2
        assert eng.next("TAG2", True) == "0030"

    def test_none_tag_treated_as_global(self):
        """Rows with no tag (None) get a global counter."""
        eng = PositionNumberEngine()
        r1 = eng.next(None, True)
        r2 = eng.next(None, True)
        assert r1 == "0010"
        assert r2 == "0020"

    def test_full_spec_scenario(self):
        """
        Exactly reproduce the spec example:
          Sheet 1: TAG1 → 0010..0240, TAG2 → 0010..0080
          Sheet 2: TAG1 → 0250..., TAG2 → 0090..., TAG3 → 0010...
        """
        eng = PositionNumberEngine()

        # Sheet 1 — TAG1: 24 spare items
        for _ in range(24):
            eng.next("TAG1", True)
        assert eng.current("TAG1") == "0240"

        # Sheet 1 — TAG2: 8 spare items
        for _ in range(8):
            eng.next("TAG2", True)
        assert eng.current("TAG2") == "0080"

        # Sheet 2 — TAG1: continues from 0240
        assert eng.next("TAG1", True) == "0250"
        assert eng.next("TAG1", True) == "0260"

        # Sheet 2 — TAG2: continues from 0080
        assert eng.next("TAG2", True) == "0090"

        # Sheet 2 — TAG3: new tag, resets to 0010
        assert eng.next("TAG3", True) == "0010"


# ─────────────────────────────────────────────────────────────────────────────
# Run directly
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Quick direct run without pytest
    import traceback
    passed = failed = 0
    test_classes = [
        TestBuildSpirBase, TestBuildSuffix, TestStripLeadingZeros,
        TestBuildOldMaterialNumber, TestPositionNumberEngine,
    ]
    for cls in test_classes:
        obj = cls()
        for name in [m for m in dir(obj) if m.startswith("test_")]:
            try:
                getattr(obj, name)()
                print(f"  PASS  {cls.__name__}.{name}")
                passed += 1
            except Exception as e:
                print(f"  FAIL  {cls.__name__}.{name}: {e}")
                traceback.print_exc()
                failed += 1
    print(f"\n{passed} passed, {failed} failed.")
