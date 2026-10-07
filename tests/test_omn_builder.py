"""
Regression tests for OLD MATERIAL NUMBER/SPF NUMBER (OMN) generation.

Type-identifier SPIRs (numeric project + non-numeric type token, e.g.
VEN-4460-DGTYP-5-43-1202-A) produce a type-led OMN of exactly 18 chars:
  {TYPE}[-]{DISC}{SUBG}[-]{SEQ}-{sheet:02d}L{line:02d}
Non-type SPIRs (VP class-code format, no type token) keep the existing logic.
"""
from __future__ import annotations

import logging
import re

import pytest

from spir_dynamic.extraction import post_processor as pp
from spir_dynamic.extraction.post_processor import build_omn

_SPIR_TOKEN_RE = re.compile(r"[A-Z0-9][A-Z0-9]*(?:-[A-Z0-9]+){2,}", re.IGNORECASE)


@pytest.fixture(autouse=True)
def _clear_warn_cache():
    pp._warn_invalid_type_omn.cache_clear()
    yield


def _seq_significant(spir: str) -> str:
    """Significant digits of the sequence segment (after DISC-SUBG)."""
    m = re.search(r"-\d-\d{2}-(\d+)", spir)
    assert m, spir
    return m.group(1).lstrip("0")


# ---------------------------------------------------------------------------
# Required production cases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spir, expected", [
    ("VEN-4460-DGTYP-5-43-1202-A", "DGTYP5431202-01L01"),
    ("VEN-4460-DGTYP-4-43-3001-1", "DGTYP4433001-01L01"),
    ("VEN-4142-RLCSF3-4-43-0500-A", "RLCSF3443500-01L01"),
    ("VEN-4460-DGEN-5-43-0115-1", "DGEN-5430115-01L01"),
])
def test_required_cases(spir, expected):
    omn = build_omn(spir, 1, 1)
    assert omn == expected
    assert len(omn) == 18


def test_required_case_with_upstream_underscore_trim():
    # _resolve_spir_no's token regex stops at "_" — that behaviour is kept.
    raw = "VEN-4568-RLCSF4-4-43-0027_2_1"
    spir = _SPIR_TOKEN_RE.search(raw).group(0)
    assert spir == "VEN-4568-RLCSF4-4-43-0027"
    omn = build_omn(spir, 1, 1)
    assert omn == "RLCSF4443027-01L01"
    assert len(omn) == 18


# ---------------------------------------------------------------------------
# Structural rule across 3–6 char type identifiers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spir, expected", [
    # 3-char type: core 10 → hyphens at TYPE|DISC and SUBG|SEQ
    ("VEN-4391-MTY-5-43-0115", "MTY-543-0115-01L01"),
    ("VEN-4391-MTY-5-43-1202", "MTY-543-1202-01L01"),
    # 4-char type: core 11 → one hyphen, leading zero kept (not required)
    ("VEN-4391-M4TY-2-43-0001", "M4TY-2430001-01L01"),
    ("VEN-4460-DGEN-5-43-0115", "DGEN-5430115-01L01"),
    # 5-char type: core 12 → no hyphen, leading zero kept
    ("VEN-4460-DGTYP-4-43-0613-B", "DGTYP4430613-01L01"),
    ("VEN-4460-DGTYP-5-43-1202", "DGTYP5431202-01L01"),
    # 6-char type: core 13 → strip exactly one non-significant leading zero
    ("VEN-4460-DGTYP3-5-43-0115", "DGTYP3543115-01L01"),
    ("VEN-4142-RLCSF3-4-43-0500", "RLCSF3443500-01L01"),
    ("VEN-4568-RLCSF4-4-43-0027", "RLCSF4443027-01L01"),
])
def test_structural_rule(spir, expected):
    omn = build_omn(spir, 1, 1)
    assert omn == expected
    assert len(omn) == 18
    type_id = spir.split("-")[2]
    assert omn.startswith(type_id)
    # Significant sequence digits survive at the end of the body.
    body = omn.rsplit("-", 1)[0].replace("-", "")
    assert body.endswith(_seq_significant(spir))


def test_leading_zero_stripped_only_as_needed():
    # 0027 under a 6-char type loses exactly one zero, not both.
    assert build_omn("VEN-4568-RLCSF4-4-43-0027", 1, 1).startswith("RLCSF4443027")
    # Under a 4/5-char type no zero is removed at all.
    assert build_omn("VEN-4568-DGEN-4-43-0027", 1, 1) == "DGEN-4430027-01L01"
    assert build_omn("VEN-4568-DGTYP-4-43-0027", 1, 1) == "DGTYP4430027-01L01"


def test_suffix_uses_sheet_and_line():
    omn = build_omn("VEN-4460-DGTYP-5-43-1202-A", 2, 12, total_main_sheets=2)
    assert omn == "DGTYP5431202-02L12"
    assert len(omn) == 18


@pytest.mark.parametrize("spir", [
    "VEN-4391-MTY-5-43-0115", "VEN-4391-M4TY-2-43-0001",
    "VEN-4460-DGEN-5-43-0115-1", "VEN-4460-DGTYP-5-43-1202-A",
    "VEN-4460-DGTYP3-5-43-0115", "VEN-4142-RLCSF3-4-43-0500-A",
    "VEN-4142-RLCSF3-4-43-1500-A", "VEN-4568-RLCSF4-4-43-0027",
])
@pytest.mark.parametrize("sheet, line", [(1, 1), (2, 12), (1, 99), (3, 100)])
def test_fitting_invariants(spir, sheet, line):
    omn = build_omn(spir, sheet, line, total_main_sheets=3)
    segs = spir.split("-")
    project, type_id, disc, subg = segs[1], segs[2], segs[3], segs[4]
    if omn == "":
        # Only the genuinely unrepresentable combination may be blank.
        assert (type_id, line) == ("RLCSF3", 100) and segs[5] == "1500"
        return
    assert len(omn) == 18
    assert omn.startswith(type_id)                     # type preserved
    assert not omn.startswith(project)                 # no project fallback
    suffix = f"{sheet:02d}L{line:02d}"
    assert omn.endswith(suffix)                        # suffix preserved
    body = omn[len(type_id):-len(suffix)].replace("-", "")
    assert body.startswith(disc + subg)                # disc/subgroup preserved
    assert body[3:].lstrip("0") == _seq_significant(spir)  # no significant digit lost


# ---------------------------------------------------------------------------
# Over-length cases — drop the formatting hyphen, never blank / truncate
# ---------------------------------------------------------------------------

def test_six_char_type_without_leading_zero_drops_suffix_hyphen():
    # No non-significant zero to strip → the suffix hyphen is the only
    # formatting character left to remove.
    omn = build_omn("VEN-4142-RLCSF3-4-43-1500-A", 1, 1)
    assert omn == "RLCSF3443150001L01"
    assert len(omn) == 18


def test_line_over_99_drops_suffix_hyphen():
    omn = build_omn("VEN-4460-DGTYP-5-43-1202-A", 1, 100)
    assert omn == "DGTYP543120201L100"
    assert len(omn) == 18


def test_line_over_99_keeps_hyphen_when_it_fits():
    # DGEN core 11 + "-01L100" is exactly 18; the T|D hyphen is not needed.
    omn = build_omn("VEN-4460-DGEN-5-43-0115-1", 1, 100)
    assert omn == "DGEN5430115-01L100"
    assert len(omn) == 18


def test_line_over_99_strips_zero_before_dropping_hyphen():
    # 6-char type: strip 0500→500 first, then the suffix hyphen still must go.
    omn = build_omn("VEN-4142-RLCSF3-4-43-0500-A", 1, 100)
    assert omn == "RLCSF344350001L100"
    assert len(omn) == 18


def test_fused_suffix_ref_is_read_correctly():
    from spir_dynamic.services.duplicate_checker import _ref_from_omn
    assert _ref_from_omn("RLCSF3443150001L01") == "01L01"
    assert _ref_from_omn("DGTYP543120202L100") == "02L100"
    assert _ref_from_omn("DGTYP5431202-02L12") == "02L12"


def test_unrepresentable_is_blank_with_single_warning(caplog):
    # 6-char type, no zero to strip, 6-char suffix: 19 chars even with no
    # hyphens at all — the only case left blank.
    with caplog.at_level(logging.WARNING, logger=pp.__name__):
        for _ in range(3):
            assert build_omn("VEN-4142-RLCSF3-4-43-1500", 1, 100) == ""
    assert caplog.text.count("OMN left blank") == 1


# ---------------------------------------------------------------------------
# Accidental repeated hyphens in the source SPIR
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dirty", [
    "VEN-4460-DGTYP-4-43--0613",
    "VEN--4460-DGTYP-4-43-0613",
])
def test_double_hyphen_omn_matches_clean(dirty):
    clean = "VEN-4460-DGTYP-4-43-0613"
    assert build_omn(dirty, 1, 1) == build_omn(clean, 1, 1) == "DGTYP4430613-01L01"


@pytest.mark.parametrize("cell", [
    "VEN-4460-DGTYP-4-43--0613",
    "VEN--4460-DGTYP-4-43-0613",
])
def test_resolve_spir_no_collapses_double_hyphen(cell):
    from types import SimpleNamespace
    from spir_dynamic.extraction.unified_extractor import _resolve_spir_no
    profile = SimpleNamespace(metadata={"spir_no": cell})
    assert _resolve_spir_no([profile], "") == "VEN-4460-DGTYP-4-43-0613"


# ---------------------------------------------------------------------------
# Unchanged non-type paths
# ---------------------------------------------------------------------------

def test_vp_class_code_format_unchanged():
    assert build_omn("4400-VP-30-00-10-053", 1, 1) == "4400-3010053-L01"


def test_no_type_identifier_unchanged():
    assert build_omn("VEN-4460-5-43-1202", 1, 1) == "4460-5431202-01L01"


def test_location_drop_helper_unchanged():
    assert pp._maybe_drop_location_segment(
        ["4460", "DGTYP", "5", "43", "1202"]
    ) == ["4460", "5", "43", "1202"]
    assert pp._maybe_drop_location_segment(
        ["4400", "VP", "30", "00", "10", "053"]
    ) == ["4400", "VP", "30", "00", "10", "053"]
