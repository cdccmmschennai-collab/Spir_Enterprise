"""
SPIR comparison of two SPIR files: SOURCE file -> COMPARISON file.

Both files go through the ONE extraction engine (pipeline.extract_output_rows,
the same steps the Extraction and Batch Extraction tabs run). The rows are then
matched (comparison.matching), every change is checked against the original
workbooks (comparison.verify) and written as LATEST / ADDED & MODIFIED /
SUMMARY (user-approved layout).

Rules (user-approved):
  * Source = the OLDER file by the value next to "ISSUE DATE:" (day-first);
    on the same date the lower revision is the source.
  * Same SPIR number + same SPIR type  -> REVISION comparison:
      spares matched on part number -> ADDED / DELETED / MODIFIED.
  * Anything else                       -> SPIR TYPE comparison: nothing is
      deleted or modified; rows in both files = SIMILAR (green), only in the
      comparison file = ADDED (yellow), only in the source file = UNCHANGED.
"""
from __future__ import annotations

import datetime as dt
import difflib
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import structlog
from openpyxl.styles import Alignment, Font

from spir_dynamic.comparison import verify as verify_mod
from spir_dynamic.comparison.matching import (
    BLUE, DELETED_STATUSES, GREEN, ORANGE, RED, YELLOW, compare, norm, split_spir,
)
from spir_dynamic.comparison.workbook import BORDER, build_workbook
from spir_dynamic.extraction.output_schema import OUTPUT_COLS

log = structlog.stdlib.get_logger(__name__)

REVISION = "REVISION"
SPIR_TYPE = "SPIR TYPE"


@dataclass
class ComparisonInput:
    path: Path          # file on disk
    filename: str       # name the user uploaded (drives SPIR no / revision fallbacks)


# -- helpers -----------------------------------------------------------------------

def canonical_type(v) -> str:
    v = (v or "").upper()
    for kw, label in (("COMMISSION", "COMMISSIONING SPARES"), ("INITIAL", "INITIAL SPARES"),
                      ("NORMAL", "NORMAL OPERATING SPARES"), ("LIFE", "LIFE CYCLE SPARES")):
        if kw in v:
            return label
    return "NOT DETECTED"


def project_code(base: str) -> str:
    """Project / package = SPIR No without its last segment (the document number).
    'VEN-MEWTP-5-43-0033' -> 'VEN-MEWTP-5-43'."""
    return base.rsplit("-", 1)[0] if "-" in base else "-"


_DATE_RE = re.compile(r"^\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\s*$")
_ISO_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")


def _as_date(v) -> Optional[dt.date]:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = str(v or "")
    m = _ISO_RE.match(s)
    if m:
        try:
            return dt.date(*map(int, m.groups()))
        except ValueError:
            return None
    m = _DATE_RE.match(s)
    if m:                                   # SPIR forms are day-first (21/01/2025)
        d, mo, y = map(int, m.groups())
        try:
            return dt.date(y + 2000 if y < 100 else y, mo, d)
        except ValueError:
            return None
    return None


def issue_date(path: Path, filename: str = "") -> Optional[dt.date]:
    """The value next to the 'ISSUE DATE' label (signature block), first sheet that has one."""
    wb = verify_mod.open_values(path, filename)
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                for c, v in enumerate(row):
                    if isinstance(v, str) and "ISSUE DATE" in v.upper():
                        for nxt in row[c + 1:c + 8]:
                            if nxt not in (None, ""):
                                d = _as_date(nxt)
                                if d:
                                    return d
                                break
    finally:
        wb.close()
    return None


def _rev_key(v: str):
    return (0, int(v)) if v.isdigit() else (1, v)


def comparison_mode(sm: dict, cm: dict) -> str:
    """REVISION = same SPIR number + same SPIR type (only the revision differs)."""
    same_no = split_spir(sm["spir_no"])[0] == split_spir(cm["spir_no"])[0]
    same_type = canonical_type(sm["spir_type"]) == canonical_type(cm["spir_type"])
    return REVISION if same_no and same_type else SPIR_TYPE


def combine_spir_types(res: dict) -> dict:
    """
    SPIR TYPE comparison: nothing is deleted or modified.
      row in both files            -> SIMILAR (green, shown once = comparison file's row)
      row only in comparison file  -> ADDED (yellow)
      row only in source file      -> UNCHANGED (no colour, kept in LATEST)
    """
    res["mode"] = SPIR_TYPE
    res["row_status"] = ["ADDED" if st == "ADDED TO TAG" else ("SIMILAR" if st in ("MODIFIED", "UNCHANGED") else st)
                         for st in res["row_status"]]
    res["row_diffs"] = [[] for _ in res["row_diffs"]]
    res["deleted_rows"] = [("UNCHANGED" if st in DELETED_STATUSES else st, r, j)
                           for st, r, j in res["deleted_rows"]]
    res["tags_src_only"], res["tags_deleted"] = set(res["tags_deleted"]), set()
    res["spares_src_only"], res["spares_deleted"] = set(res["spares_deleted"]), set()
    res["changes"] = {}
    res["spares_modified"], res["tags_modified"], res["moved"] = set(), set(), {}
    res["other_changes"] = []
    res["spares_unchanged"] = set(res["P"].spare_tags) & set(res["N"].spare_tags)
    return res


def _models(rows, C) -> dict:
    out = {}
    for r in rows:
        if r[C["ITEM NUMBER"]] in (None, "") and r[C["TAG NO"]]:
            out[norm(r[C["TAG NO"]])] = norm(r[C["EQPT MODEL"]])
    return out


def possible_renames(res, prev_rows, curr_rows) -> list[tuple[str, str, str]]:
    """
    Added/deleted tag pairs that may be the same equipment -> MANUAL REVIEW, never auto-mapped:
    similar tag number, same spare parts, or the source 'tag' is the comparison tag's model.
    """
    C, P, N = res["C"], res["P"], res["N"]
    sp_p = {t: {s for s, tags in P.spare_tags.items() if t in tags} for t in res["tags_deleted"]}
    sp_n = {t: {s for s, tags in N.spare_tags.items() if t in tags} for t in res["tags_added"]}
    m_p, m_n = _models(prev_rows, C), _models(curr_rows, C)
    out = []
    for d in sorted(res["tags_deleted"]):
        for a in sorted(res["tags_added"]):
            reasons = []
            if difflib.SequenceMatcher(None, d, a).ratio() >= 0.85:
                reasons.append("SIMILAR TAG NUMBER")
            if sp_p[d] and sp_p[d] == sp_n[a]:
                reasons.append("SAME SPARE PARTS")
            if d == m_n.get(a) or a == m_p.get(d) or (m_p.get(d) and m_p.get(d) == m_n.get(a)):
                reasons.append("SAME EQUIPMENT MODEL" if m_p.get(d) == m_n.get(a) else "SOURCE TAG IS THE MODEL OF THIS TAG")
            if reasons:
                out.append((d, a, ", ".join(reasons)))
    return out


# -- SUMMARY sheet -----------------------------------------------------------------

def build_summary(wb, src_name, cmp_name, sm, cm, res, renames, manual, check_extr):
    """SUMMARY sheet exactly as the approved SPIR_COMPARISON_0033-REV-4_VS_0031-REV-A.xlsx."""
    P, N = res["P"], res["N"]
    sb, srev = split_spir(sm["spir_no"])
    cb, crev = split_spir(cm["spir_no"])
    hdr_style = wb["ADDED & MODIFIED"].cell(row=1, column=1).style
    s = wb.create_sheet("SUMMARY")
    for col, w in (("A", 55.1), ("B", 40.6), ("C", 40.7), ("G", 33.4)):
        s.column_dimensions[col].width = w

    def cell(r, c, v=None, fill=None):
        x = s.cell(row=r, column=c, value=v)
        x.border = BORDER
        if fill:
            x.fill = fill
        return x

    def title(r, text, c0=1, span=3):
        cell(r, c0, text).font = Font(bold=True, size=12)
        s.cell(row=r, column=c0).alignment = Alignment(horizontal="center")
        for c in range(c0 + 1, c0 + span):
            cell(r, c)
        s.row_dimensions[r].height = 15.75

    def head(r, *labels):
        for c, v in enumerate(labels, 1):
            cell(r, c, v).style = hdr_style

    # FILE DETAILS (A1:C9)
    title(1, "FILE DETAILS")
    head(2, "ATTRIBUTE", "SOURCE FILE", "COMPARISON FILE")
    r = 3
    for name, x, y, check in (
        ("FILE NAME", src_name, cmp_name, False),
        ("PROJECT / PACKAGE (SPIR NO PREFIX)", project_code(sb), project_code(cb), True),
        ("SPIR TYPE", canonical_type(sm["spir_type"]), canonical_type(cm["spir_type"]), True),
        ("SPIR NUMBER", sb, cb, True),
        ("REVISION", f"REV-{srev}" if srev else "-", f"REV-{crev}" if crev else "-", True),
        ("EQUIPMENT TAGS", len(P.tags), res.get("tags_in_file", len(N.tags)), True),
        ("SPARE PARTS (DISTINCT)", len(P.spare_tags), res.get("spares_in_file", len(N.spare_tags)), True),
    ):
        fill = BLUE if check and str(x).strip().upper() != str(y).strip().upper() else None
        cell(r, 1, name)
        cell(r, 2, x, fill)
        cell(r, 3, y, fill)
        r += 1

    # COLOUR KEY (G1:H6): every highlight colour in every file, used or not.
    # First three rows = the approved G2:G4. No CHECK MANUALLY in the key.
    title(1, "COLOUR KEY", c0=7, span=2)
    merge = res.get("mode") == SPIR_TYPE
    key = [("ADDED (ONLY IN COMPARISON FILE)", YELLOW), ("DELETED (ONLY IN SOURCE FILE)", RED),
           ("MODIFIED (CHANGED CELLS)", BLUE), ("SIMILAR (IN BOTH FILES - DIFFERENT SPIR TYPE)", GREEN),
           ("UNCHANGED (NO COLOUR)", None)]
    for i, (label, fill) in enumerate(key, 2):
        cell(i, 7, label)
        cell(i, 8, None, fill)

    def table(r0, heading, items):
        title(r0, heading, span=2)
        head(r0 + 1, "ITEM", "COUNT")
        for i, (label, v, fill) in enumerate(items, r0 + 2):
            f = fill if fill and v not in (0, "SAME") else None
            cell(i, 1, label, f)
            cell(i, 2, v, f)
        return r0 + 2 + len(items)

    # EQUIPMENT SCOPE (A11:B17)
    src_tags = res.get("tags_src_only", set())
    scope_same = not res["tags_added"] and not res["tags_deleted"] and not src_tags
    end = table(11, "EQUIPMENT SCOPE", [
        ("SIMILAR EQUIPMENT TAGS (IN BOTH FILES)", len(P.tags & N.tags), GREEN) if merge else
        ("COMMON EQUIPMENT TAGS (IN BOTH FILES)", len(P.tags & N.tags), None),
        ("TAGS ADDED (ONLY IN COMPARISON FILE)", len(res["tags_added"]), YELLOW),
        ("TAGS UNCHANGED (ONLY IN SOURCE FILE)", len(src_tags), None) if merge else
        ("TAGS DELETED (ONLY IN SOURCE FILE)", len(res["tags_deleted"]), RED),
        ("EQUIPMENT SCOPE", "SAME" if scope_same else "DIFFERENT", None),
        ("POSSIBLE TAG RENAMES (CHECK MANUALLY)", len(renames), ORANGE),
    ])
    s.cell(row=end - 2, column=2).alignment = Alignment(horizontal="right")

    # CHANGES IN THE COMPARISON FILE (A19:B29)
    table(end + 1, "COMBINED SPARES (DIFFERENT SPIR TYPE)" if merge else "CHANGES IN THE COMPARISON FILE", [
        ("SPARE PARTS ADDED (ONLY IN COMPARISON FILE)" if merge else "SPARE PARTS ADDED",
         len(res["spares_added"]), YELLOW),
        ("SPARE PARTS UNCHANGED (ONLY IN SOURCE FILE)", len(res.get("spares_src_only", ())), None) if merge else
        ("SPARE PARTS DELETED", len(res["spares_deleted"]), RED),
        ("SPARE PARTS MODIFIED", len(res["spares_modified"]), BLUE),
        ("SPARE PARTS SIMILAR (IN BOTH FILES)", len(res["spares_unchanged"]), GREEN) if merge else
        ("SPARE PARTS UNCHANGED", len(res["spares_unchanged"]), None),
        ("SPARE PARTS MOVED TO ANOTHER TAG", len(res["moved"]), BLUE),
        ("EQUIPMENT TAGS MODIFIED (MAKE / MODEL / SERIAL / QTY)", len(res["tags_modified"]), BLUE),
        ("OTHER CELL CHANGES IN THE FILE", len(res.get("other_changes", [])), BLUE),
        ("ROWS TO CHECK MANUALLY (NO PART NO / DWG NO)", manual, ORANGE),
        ("ROWS IN FILE BUT NOT EXTRACTED (CHECK EXTRACTION)", check_extr, ORANGE),
    ])
    return s


# -- entry point -------------------------------------------------------------------

def _extract(inp: ComparisonInput) -> tuple[dict, list[list]]:
    """The same engine as the Extraction / Batch tabs (no plant stamp, no currency step)."""
    from spir_dynamic.app.pipeline import extract_output_rows
    result, rows, _ = extract_output_rows(inp.path, inp.filename)
    return {"spir_no": result.get("spir_no", "") or "", "spir_type": result.get("spir_type")}, rows


def _short(base: str, rev: Optional[str]) -> str:
    doc = base.rsplit("-", 1)[-1] if base else "SPIR"
    return f"{doc}-REV-{rev}" if rev else doc


def compare_files(first: ComparisonInput, second: ComparisonInput, *,
                  keep_order: bool = False, force_mode: Optional[str] = None) -> dict[str, Any]:
    """
    Compare two SPIR files. `first` is the user's SOURCE choice and `second` the
    COMPARISON choice; unless keep_order, the OLDER file (ISSUE DATE, then lower
    revision) always becomes the source.

    Returns {"xlsx": bytes, "filename": str, "summary": {...}}.
    """
    src, cmp_ = first, second
    ds, dc = issue_date(src.path, src.filename), issue_date(cmp_.path, cmp_.filename)
    swapped = False
    if ds and dc and ds > dc and not keep_order:
        src, cmp_, ds, dc = cmp_, src, dc, ds
        swapped = True

    sm, sr = _extract(src)
    cm, cr = _extract(cmp_)
    srev, crev = split_spir(sm["spir_no"])[1], split_spir(cm["spir_no"])[1]
    if ds and ds == dc and srev and crev and not keep_order and _rev_key(srev) > _rev_key(crev):
        src, cmp_, sm, cm, sr, cr = cmp_, src, cm, sm, cr, sr
        swapped = not swapped
    if not sr or not cr:
        empty = src.filename if not sr else cmp_.filename
        raise ValueError(f"No rows could be extracted from {empty} - check the file in the Extraction tab first.")

    cols = OUTPUT_COLS
    res = compare(cols, sr, cr)
    res = verify_mod.verify(res, cols, sr, cr,
                            verify_mod.RawBook(src.path, src.filename), verify_mod.RawBook(cmp_.path, cmp_.filename))

    sb, srev = split_spir(sm["spir_no"])
    cb, crev = split_spir(cm["spir_no"])
    mode = force_mode or comparison_mode(sm, cm)
    if mode == SPIR_TYPE:
        combine_spir_types(res)
    else:
        res["mode"] = REVISION

    wb = build_workbook(cols, res, sm, cm)
    renames = possible_renames(res, sr, cr)
    manual = res["row_status"].count("CHECK MANUALLY") + sum(1 for st, *_ in res["deleted_rows"] if st == "CHECK MANUALLY")
    check_extr = sum(1 for st, *_ in res["deleted_rows"] if st == "CHECK EXTRACTION")
    build_summary(wb, src.filename, cmp_.filename, sm, cm, res, renames, manual, check_extr)

    buf = io.BytesIO()
    wb.save(buf)
    merge = mode == SPIR_TYPE
    P, N = res["P"], res["N"]
    summary = {
        "mode": mode,
        "swapped": swapped,
        "source": {"filename": src.filename, "spir_no": sb, "revision": srev, "issue_date": ds.isoformat() if ds else None,
                   "spir_type": canonical_type(sm["spir_type"]), "tags": len(P.tags), "spares": len(P.spare_tags)},
        "comparison": {"filename": cmp_.filename, "spir_no": cb, "revision": crev, "issue_date": dc.isoformat() if dc else None,
                       "spir_type": canonical_type(cm["spir_type"]), "tags": res.get("tags_in_file", len(N.tags)),
                       "spares": res.get("spares_in_file", len(N.spare_tags))},
        "tags_common": len(P.tags & N.tags),
        "tags_added": len(res["tags_added"]),
        "tags_deleted": len(res["tags_deleted"]),
        "tags_source_only": len(res.get("tags_src_only", ())),
        "tags_modified": len(res["tags_modified"]),
        "spares_added": len(res["spares_added"]),
        "spares_deleted": len(res["spares_deleted"]),
        "spares_source_only": len(res.get("spares_src_only", ())),
        "spares_modified": len(res["spares_modified"]),
        "spares_similar" if merge else "spares_unchanged": len(res["spares_unchanged"]),
        "spares_moved": len(res["moved"]),
        "other_changes": len(res.get("other_changes", [])),
        "check_manually": manual,
        "check_extraction": check_extr,
        "possible_renames": len(renames),
    }
    filename = f"SPIR_COMPARISON_{_short(sb, srev)}_VS_{_short(cb, crev)}.xlsx"
    log.info("comparison.done", mode=mode, source=src.filename, comparison=cmp_.filename, swapped=swapped)
    return {"xlsx": buf.getvalue(), "filename": filename, "summary": summary}
