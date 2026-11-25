"""
Output sheets of the SPIR comparison (user-approved layout):

  LATEST            the comparison file's extraction (same columns as the
                    Extraction tab) + STATUS; source-only rows shown INLINE
                    under their tag; all borders.
  ADDED & MODIFIED  how each spare changed, with PART NUMBER + ITEM NUMBER.
  SUMMARY           built by comparison.summary.
"""
from __future__ import annotations

import io
import re
from collections import defaultdict

import openpyxl
from openpyxl.styles import Border, Side as BSide
from openpyxl.utils import get_column_letter

from spir_dynamic.comparison.matching import (
    BLUE, DELETED_STATUSES, EQPT, GREEN, ORANGE, RED, YELLOW, norm, show, split_spir,
)

_T = BSide(style="thin", color="000000")
BORDER = Border(left=_T, right=_T, top=_T, bottom=_T)


def _merge_deleted_inline(res):
    """
    Comparison rows in order, with each source-only row slotted in right after
    the last comparison row of the same tag. A whole source-only tag goes after
    the block of the tag that preceded it in the source file.
    Returns [(row, status, changed_fields)].
    """
    C, N, P = res["C"], res["N"], res["P"]
    last_curr_of_tag = {}
    for i, r in enumerate(N.rows):
        last_curr_of_tag[norm(r[C["TAG NO"]])] = i

    anchor = defaultdict(list)            # comparison index (-1 = top) -> source rows
    prev_anchor = -1
    deleted_by_prev_idx = {j: (st, r) for st, r, j in res["deleted_rows"]}
    for j, r in enumerate(P.rows):
        tag = norm(r[C["TAG NO"]])
        if tag in last_curr_of_tag:
            prev_anchor = last_curr_of_tag[tag]
        if j in deleted_by_prev_idx:
            st, row = deleted_by_prev_idx[j]
            anchor[prev_anchor].append((row, st, []))

    out = list(anchor.get(-1, []))
    for i, r in enumerate(N.rows):
        out.append((r, res["row_status"][i], res["row_diffs"][i]))
        out.extend(anchor.get(i, []))
    return out


def _border_all(ws, first_row=1):
    for row in ws.iter_rows(min_row=first_row, max_row=ws.max_row, max_col=ws.max_column):
        for cell in row:
            cell.border = BORDER


def build_workbook(cols, res, src_meta, cmp_meta) -> openpyxl.Workbook:
    from spir_dynamic.services.excel_builder import build_xlsx
    C, P, N = res["C"], res["P"], res["N"]

    # -- LATEST: one sheet, everything in it --
    merged = _merge_deleted_inline(res)
    wb = openpyxl.load_workbook(io.BytesIO(build_xlsx([m[0] for m in merged], cmp_meta["spir_no"])))
    ws = wb.active
    ws.title = "LATEST"
    sc = len(cols) + 2
    for rr, label in ((1, "STATUS"), (2, ""), (3, "")):
        ws.cell(row=rr, column=sc, value=label).style = ws.cell(row=rr, column=sc - 1).style
    ws.column_dimensions[get_column_letter(sc)].width = 18
    ws.auto_filter.ref = f"A1:{get_column_letter(sc)}1"
    price_col = C["UNIT PRICE"] + 2
    tag_col = C["TAG NO"] + 2

    def spare_key(r):
        if norm(r[C["ITEM NUMBER"]]) in ("", "NONE"):
            return EQPT
        pn, dwg = norm(r[C["MANUFACTURER PART NUMBER"]]), norm(r[C["DWG NO INCL POSN NO"]])
        return "PN:" + pn if pn else ("DWG:" + dwg if dwg else None)

    remaining = set(N.spare_tags)          # spares still in the comparison file
    tag_keeps_spare = {norm(r[C["TAG NO"]]) for r, st, _ in merged
                       if st == "TAG DELETED" and spare_key(r) in remaining}
    for k, (row, st, diffs) in enumerate(merged):
        xr = k + 4
        ws.cell(row=xr, column=sc, value=st)
        sk = spare_key(row) if st == "TAG DELETED" else None
        if st in ("ADDED", "ADDED TO TAG"):
            fill_cols, fill = range(1, sc + 1), YELLOW
        elif st == "TAG DELETED" and (sk in remaining or (sk == EQPT and norm(row[C["TAG NO"]]) in tag_keeps_spare)):
            # tag deleted but the spare remains (on other tags) -> only the TAG cell
            fill_cols, fill = [tag_col], RED
        elif st in DELETED_STATUSES:
            fill_cols, fill = range(1, sc + 1), RED
        elif st == "SIMILAR":            # SPIR TYPE mode: row in both files
            fill_cols, fill = range(1, sc + 1), GREEN
        elif st == "MODIFIED":
            fill_cols, fill = [C[f] + 2 for f in diffs] + [sc], BLUE
        elif st in ("CHECK MANUALLY", "CHECK EXTRACTION"):
            fill_cols, fill = [sc], ORANGE
        else:
            fill_cols, fill = [], None
        for c in fill_cols:
            ws.cell(row=xr, column=c).fill = fill
        pc = ws.cell(row=xr, column=price_col)
        if isinstance(pc.value, float):
            pc.value = show(pc.value)
    _border_all(ws, first_row=1)

    # -- ADDED & MODIFIED: what changed and how --
    sb, srev = split_spir(src_meta["spir_no"])
    cb, crev = split_spir(cmp_meta["spir_no"])
    s = wb.create_sheet("ADDED & MODIFIED")
    headers = ["EQUIPMENT TAG", "PART NUMBER", "ITEM NUMBER", "DWG NO", "FIELD NAME",
               f"{sb} REV-{srev}" if srev else sb, f"{cb} REV-{crev}" if crev else cb, "TYPE"]
    for c, (h, w) in enumerate(zip(headers, [21, 28, 12, 45, 30, 45, 45, 12]), 1):
        s.cell(row=1, column=c, value=h).style = "spir_hdr"
        s.column_dimensions[get_column_letter(c)].width = w
    s.freeze_panes = "A2"

    curr_rows_of = defaultdict(list)      # spare -> [comparison rows]
    for k, r in zip(N.keys, N.rows):
        if k and k[1] != EQPT:
            curr_rows_of[k[1]].append(r)

    def tags_label(tags):
        tags = sorted(t for t in tags if t)      # no tag -> left blank
        if not tags:
            return ""
        return tags[0] if len(tags) == 1 else f"{len(tags)} TAG(S)"

    def ident(spare, tags=None):
        rows = [r for r in curr_rows_of[spare] if tags is None or norm(r[C["TAG NO"]]) in tags] or curr_rows_of[spare]
        # Unique item code = sheet + line from the SPF number ("DGEN-5430116-01L02"
        # -> "01L02"), so "item 1" on Main Sheet 1 and Main Sheet 2 never collide.
        # Falls back to the raw ITEM NUMBER only if the SPF code is missing.
        def code(r):
            m = re.search(r"(\d+L\d+)\s*$", str(r[C["OLD MATERIAL NUMBER/SPF NUMBER"]] or ""))
            return m.group(1) if m else (str(show(r[C["ITEM NUMBER"]])) if r[C["ITEM NUMBER"]] not in (None, "") else "")
        items = sorted({code(r) for r in rows} - {""})
        r = rows[0]
        return ", ".join(items), r[C["MANUFACTURER PART NUMBER"]], r[C["DWG NO INCL POSN NO"]], r[C["DESCRIPTION OF PARTS"]]

    out = []
    for t in sorted(res["tags_added"]):
        out.append([t, "", "", "", "(NEW TAG)", "", "", "ADDED", YELLOW])
    for sp in sorted(res["spares_added"]):
        item, pn, dwg, desc = ident(sp)
        out.append([tags_label(N.spare_tags[sp]), item, pn, dwg, "(NEW SPARE)", "", desc, "ADDED", YELLOW])
    # SPIR TYPE mode: source-only spares/tags are UNCHANGED -> not listed here
    tag_changes = defaultdict(set)
    for (sp, f, o, n), tags in res["changes"].items():
        if sp.startswith("TAG:"):
            tag_changes[(f, o, n)] |= tags
    for (f, o, n), tags in sorted(tag_changes.items(), key=lambda x: -len(x[1])):
        out.append([tags_label(tags), "", "(EQUIPMENT)", "", f, o, n, "MODIFIED", BLUE])
    for (sp, f, o, n), tags in sorted(((k, v) for k, v in res["changes"].items() if not k[0].startswith("TAG:")),
                                      key=lambda x: (x[0][0], x[0][1])):
        item, pn, dwg, _ = ident(sp, tags)
        out.append([tags_label(tags), item, pn, dwg, f, o, n, "MODIFIED", BLUE])
    for sp, (gone, new) in sorted(res["moved"].items()):
        item, pn, dwg, _ = ident(sp)
        out.append([tags_label(gone | new), item, pn, dwg, "EQUIPMENT TAG",
                    ", ".join(sorted(gone)) or "-", ", ".join(sorted(new)) or "-", "MOVED", BLUE])
    # Real source-file edits the extraction did not explain (e.g. an annexure cell)
    for oc in res.get("other_changes", []):
        sp = "PN:" + norm(oc["row_id"] or "")
        if oc["row_id"] and curr_rows_of.get(sp):         # the row is a known spare -> PART NUMBER column
            item, pn, dwg, _ = ident(sp)
            out.append([tags_label(N.spare_tags[sp]), item, pn, dwg, oc["field"], oc["old"], oc["new"],
                        "MODIFIED", BLUE])
        else:
            out.append([oc["row_id"] or f"{oc['sheet']} {oc['cell']}", "", "", "", oc["field"],
                        oc["old"], oc["new"], "MODIFIED", BLUE])
    for row in out or [["(NO ADDED OR MODIFIED RECORDS)", "", "", "", "", "", "", "", None]]:
        tag, item, pn, *rest = row[:-1]
        s.append([tag, pn, item, *rest])          # approved order: TAG, PART NO, ITEM NO, ...
        if row[-1]:
            for c in range(1, 9):
                s.cell(row=s.max_row, column=c).fill = row[-1]
    s.auto_filter.ref = f"A1:H{s.max_row}"
    _border_all(s)
    return wb
