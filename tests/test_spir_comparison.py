"""
SPIR Comparison tab + the extraction-engine fixes it needed (shared by every tab).

Synthetic data only — no customer files.
"""
from __future__ import annotations

import datetime as dt
import io

import openpyxl
import pytest

from spir_dynamic.analysis.workbook_analyzer import _exclude_foreign_spir_sheets
from spir_dynamic.app.pipeline import _drop_empty_spare_lines
from spir_dynamic.comparison.matching import compare, split_spir
from spir_dynamic.comparison.service import (
    REVISION, SPIR_TYPE, _as_date, build_summary, combine_spir_types, comparison_mode,
)
from spir_dynamic.comparison.workbook import build_workbook
from spir_dynamic.extraction.output_schema import CI, OUTPUT_COLS, make_empty_row
from spir_dynamic.extraction.strategies.columnar import _split_separated_value
from spir_dynamic.extraction.unified_extractor import _resolve_spir_no
from spir_dynamic.models.sheet_profile import SheetProfile, SheetRole
from spir_dynamic.services.vendor_extractor import _extract_contact_numbers
from spir_dynamic.utils.cell_utils import split_tags


# ── Engine fixes ──────────────────────────────────────────────────────────────

class TestSplitTags:
    def test_full_tags_with_slash_joined_by_ampersand(self):
        assert split_tags("143-21-FE/FT-0001A & \n143-21-FE/FT-0001B") == [
            "143-21-FE/FT-0001A", "143-21-FE/FT-0001B"]

    def test_full_tags_with_slash_joined_by_comma(self):
        assert split_tags("143-21-FE/FT-0002A, 143-21-FE/FT-0002B") == [
            "143-21-FE/FT-0002A", "143-21-FE/FT-0002B"]

    @pytest.mark.parametrize("raw, want", [
        ("P-3425 A/B", ["P-3425 A", "P-3425 B"]),
        ("23V01-A/B", ["23V01-A", "23V01-B"]),
        ("89-BC-001A/B", ["89-BC-001A", "89-BC-001B"]),
        ("30-GV-146, 171, 169", ["30-GV-146", "30-GV-171", "30-GV-169"]),
        ("TAG-001", ["TAG-001"]),
    ])
    def test_existing_patterns_unchanged(self, raw, want):
        assert split_tags(raw) == want

    def test_suffix_lists_are_not_taken_as_full_tags(self):
        # each "/" part is a short suffix code -> the existing expansion handles it
        assert "23V01-A/B" not in split_tags("23V01-A/B, 23V01-C/D")


class TestSerialPerTag:
    def test_ampersand(self):
        assert _split_separated_value("N1U3248018326 & N1U3248018327", 2) == ["N1U3248018326", "N1U3248018327"]

    def test_line_break(self):
        assert _split_separated_value("665426\n665427", 2) == ["665426", "665427"]

    def test_count_mismatch_keeps_value(self):
        assert _split_separated_value("A1 & B2 & C3", 2) == ["A1 & B2 & C3"] * 2

    def test_slash_still_works(self):
        assert _split_separated_value("665426 / 665427", 2) == ["665426", "665427"]


def test_phone_has_no_empty_brackets_after_email_removed():
    out = _extract_contact_numbers("Shabin C.K\nInstrumentation Engineer\n+974 66938286 (shabin@example.com)")
    assert "(" not in out and ")" not in out
    assert "66938286" in out


class TestForeignSheets:
    def _p(self, name, spir):
        p = SheetProfile(name=name, role=SheetRole.DATA)
        p.metadata["spir_no"] = spir
        return p

    def test_file_name_typo_excludes_nothing(self):
        profiles = [self._p("MAIN SHEET", "VEN-4568-RCLSF4-4-43-0035"), self._p("CONT", "VEN-4568-RCLSF4-4-43-0035")]
        _exclude_foreign_spir_sheets(profiles, "VEN-4568-RLCSF4-4-43-0035-0.xlsm")
        assert [p.role for p in profiles] == [SheetRole.DATA, SheetRole.DATA]

    def test_embedded_other_document_still_excluded(self):
        profiles = [self._p("MAIN SHEET", "VEN-4460-DGTYP-4-43-0001"), self._p("OTHER", "VEN-4460-DGTYP-5-43-0002")]
        _exclude_foreign_spir_sheets(profiles, "VEN-4460-DGTYP-4-43-0001.xlsx")
        assert [p.role for p in profiles] == [SheetRole.DATA, SheetRole.UTILITY]


class TestRevisionFromFileName:
    def _profiles(self, revision=None):
        p = SheetProfile(name="MAIN SHEET", role=SheetRole.DATA)
        p.metadata["spir_no"] = "VEN-4568-RCLSF4-4-43-0035"
        if revision:
            p.metadata["revision"] = revision
        return [p]

    @pytest.mark.parametrize("filename, want", [
        ("VEN-4568-RLCSF4-4-43-0035-0.xlsm", "VEN-4568-RCLSF4-4-43-0035-REV-0"),
        ("VEN-4568-RLCSF4-4-43-0035_1.xlsm", "VEN-4568-RCLSF4-4-43-0035-REV-1"),
        ("VEN-4568-RCLSF4-4-43-0035.xlsm", "VEN-4568-RCLSF4-4-43-0035"),
    ])
    def test_file_name_fallback(self, filename, want):
        assert _resolve_spir_no(self._profiles(), filename) == want

    def test_issue_letter_wins_over_file_name(self):
        assert _resolve_spir_no(self._profiles("REV-B"), "VEN-4568-RCLSF4-4-43-0035-0.xlsm").endswith("REV-B")


def _row(tag=None, item=None, pn=None, desc=None, dwg=None, **extra):
    r = make_empty_row()
    r[CI["TAG NO"]], r[CI["ITEM NUMBER"]] = tag, item
    r[CI["MANUFACTURER PART NUMBER"]], r[CI["DESCRIPTION OF PARTS"]] = pn, desc
    r[CI["DWG NO INCL POSN NO"]] = dwg
    for k, v in extra.items():
        r[CI[k.replace("_", " ")]] = v
    return r


def test_empty_spare_lines_dropped_equipment_rows_kept():
    rows = [_row("T-1", desc="PUMP"), _row("T-1", 1, "PN-1", "SEAL"), _row("T-1", 2), _row("T-1", 3, dwg="D-9")]
    kept = _drop_empty_spare_lines(rows)
    assert [r[CI["ITEM NUMBER"]] for r in kept] == [None, 1, 3]


# ── Comparison ────────────────────────────────────────────────────────────────

def _eq(tag, model="M-1", serial="S-1"):
    return _row(tag, None, model, "PUMP", EQPT_MODEL=model, EQPT_SR_NO=serial)


SRC = [_eq("T-1"), _row("T-1", 1, "PN-1", "SEAL"), _row("T-1", 2, "PN-2", "GASKET"),
       _eq("T-2"), _row("T-2", 1, "PN-1", "SEAL")]
CMP = [_eq("T-1", serial="S-9"), _row("T-1", 1, "PN-1", "SEAL", UNIT_OF_MEASURE="SET"),
       _row("T-1", 3, "PN-3", "BEARING")]


class TestRevision:
    res = compare(OUTPUT_COLS, SRC, CMP)

    def test_counts(self):
        r = self.res
        assert r["tags_deleted"] == {"T-2"} and not r["tags_added"]
        assert r["spares_added"] == {"PN:PN-3"} and r["spares_deleted"] == {"PN:PN-2"}
        assert r["spares_modified"] == {"PN:PN-1"} and r["tags_modified"] == {"T-1"}

    def test_row_statuses(self):
        assert self.res["row_status"] == ["MODIFIED", "MODIFIED", "ADDED"]
        assert sorted(st for st, *_ in self.res["deleted_rows"]) == ["DELETED", "TAG DELETED", "TAG DELETED"]


class TestSpirType:
    def test_similar_added_unchanged(self):
        res = combine_spir_types(compare(OUTPUT_COLS, SRC, CMP))
        assert res["row_status"] == ["SIMILAR", "SIMILAR", "ADDED"]
        assert {st for st, *_ in res["deleted_rows"]} == {"UNCHANGED"}
        assert not res["spares_deleted"] and not res["tags_deleted"] and not res["changes"]
        assert res["spares_src_only"] == {"PN:PN-2"} and res["tags_src_only"] == {"T-2"}

    def test_workbook_colours_and_summary(self):
        res = combine_spir_types(compare(OUTPUT_COLS, SRC, CMP))
        sm = {"spir_no": "VEN-X-1-43-0006-REV-A", "spir_type": "normal operating spares"}
        cm = {"spir_no": "VEN-X-1-43-0005-REV-B", "spir_type": "initial spares"}
        wb = build_workbook(OUTPUT_COLS, res, sm, cm)
        build_summary(wb, "a.xlsx", "b.xlsx", sm, cm, res, [], 0, 0)
        wb = openpyxl.load_workbook(io.BytesIO(_save(wb)))
        assert wb.sheetnames == ["LATEST", "ADDED & MODIFIED", "SUMMARY"]
        ws = wb["LATEST"]
        st_col = ws.max_column
        fills = {ws.cell(r, st_col).value: (ws.cell(r, 1).fill.fgColor.rgb if ws.cell(r, 1).fill.fill_type else None)
                 for r in range(4, ws.max_row + 1)}
        assert fills["SIMILAR"].endswith("92D050") and fills["ADDED"].endswith("FFFF00") and fills["UNCHANGED"] is None
        s = wb["SUMMARY"]
        legend = [s.cell(r, 7).value for r in range(2, 8)]
        assert legend == ["ADDED (ONLY IN COMPARISON FILE)", "DELETED (ONLY IN SOURCE FILE)", "MODIFIED (CHANGED CELLS)",
                          "SIMILAR (IN BOTH FILES - DIFFERENT SPIR TYPE)", "UNCHANGED (NO COLOUR)", None]
        a = wb["ADDED & MODIFIED"]
        assert [a.cell(r, 8).value for r in range(2, a.max_row + 1)] == ["ADDED"]   # comparison-only spare only


def _save(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_mode_rules():
    m = lambda no, ty: {"spir_no": no, "spir_type": ty}
    assert comparison_mode(m("X-0001-REV-1", "normal"), m("X-0001-REV-2", "NORMAL OPERATING")) == REVISION
    assert comparison_mode(m("X-0006-REV-A", "normal"), m("X-0005-REV-B", "initial")) == SPIR_TYPE
    assert comparison_mode(m("X-0001-REV-1", "normal"), m("X-0001-REV-2", "life cycle")) == SPIR_TYPE


def test_issue_date_is_day_first():
    assert _as_date("21/01/2025") == dt.date(2025, 1, 21)
    assert _as_date(dt.datetime(2025, 9, 24)) == dt.date(2025, 9, 24)
    assert _as_date("2026-07-02 00:00:00") == dt.date(2026, 7, 2)


def test_split_spir():
    assert split_spir("VEN-4568-RCLSF4-4-43-0035-REV-0") == ("VEN-4568-RCLSF4-4-43-0035", "0")
    assert split_spir("VEN-4568-RCLSF4-4-43-0035") == ("VEN-4568-RCLSF4-4-43-0035", None)
