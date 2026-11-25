"""
Source-file verification layer for the SPIR comparison.

The extraction can be wrong (0301 Rev 3: 709 tags "deleted" that are still in
the file). So every change the extraction-based comparison reports is checked
against the ORIGINAL workbooks before it is shown to the user:

  * tag / spare DELETED  -> only if its identifier is no longer in the current file
  * tag / spare ADDED    -> only if its identifier was not in the previous file
  * field MODIFIED       -> only if the old value is in the previous file's rows for
                            that tag/spare AND the new value is in the current file's rows
  * spare moved on/off a tag -> only if the source rows of that tag or spare really changed

Unconfirmed changes are dropped (row shown as UNCHANGED) and counted in the
summary. A cell-by-cell diff of same-named sheets then lists real file changes
the extraction did not explain (OTHER FILE CHANGES) and header changes
(DOCUMENT CHANGES: revision, date, SPIR number).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path

import openpyxl

from spir_dynamic.comparison.matching import EQPT, norm

_SPLIT = re.compile(r"\s*&\s*|[,;\n]+")      # "&" joins two tags / two serials (2026-11-24)
_HDR_WORDS = ("ITEM", "DESCRIPTION", "PART", "DWG", "TAG", "SERIAL", "MODEL", "QUANTITY", "PRICE",
              "CLASSIFICATION", "MATERIAL", "CURRENCY", "DELIVERY", "MEASURE", "SAP", "SUPPLIER",
              "VALVE TYPE", "SIZE", "CLASS", "ANNEXURE")
_DOC_RE = re.compile(r"\bREV\b|\bREV\.|^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$|^\d{4}-\d{2}-\d{2}", re.I)


def open_values(path, filename: str = ""):
    """Workbook with cell VALUES: openpyxl read-only for .xlsx/.xlsm, the engine's
    xlrd adapter for legacy .xls (same loader the extraction uses)."""
    name = filename or str(path)
    if name.lower().endswith(".xls"):
        from spir_dynamic.extraction.workbook_loader import load_workbook_any
        return load_workbook_any(Path(path), name)
    return openpyxl.load_workbook(path, read_only=True, data_only=True)


class RawBook:
    """Normalised cell values of every sheet + an identifier index."""

    def __init__(self, path, filename: str = ""):
        wb = open_values(path, filename)
        self.sheets: dict[str, dict[int, dict[int, str]]] = {}
        self.index: dict[str, set[tuple[str, int]]] = defaultdict(set)   # token -> {(sheet,row)}
        for ws in wb.worksheets:
            rows = {}
            for r, row in enumerate(ws.iter_rows(values_only=True), 1):
                cells = {}
                for c, v in enumerate(row, 1):
                    if v in (None, ""):
                        continue
                    nv = norm(v)
                    if not nv:
                        continue
                    cells[c] = nv
                    self.index[nv].add((ws.title, r))
                    for part in _SPLIT.split(str(v)):
                        p = norm(part)
                        if len(p) >= 3:
                            self.index[p].add((ws.title, r))
                if cells:
                    rows[r] = cells
            self.sheets[ws.title] = rows
        wb.close()
        self._hdr = {s: self._header_row(rows) for s, rows in self.sheets.items()}

    def has(self, token: str) -> bool:
        return bool(token) and token in self.index

    def rows_with(self, token: str) -> list[dict[int, str]]:
        return [self.sheets[s][r] for s, r in sorted(self.index.get(token, ()))]

    def values_with(self, token: str) -> Counter:
        return Counter(v for row in self.rows_with(token) for v in row.values())

    @staticmethod
    def _header_row(rows):
        best, best_r = 0, None
        for r in sorted(rows)[:40]:
            hits = sum(1 for v in rows[r].values() if any(w in v for w in _HDR_WORDS) and len(v) < 60)
            if hits > best:
                best, best_r = hits, r
        return best_r

    def header_of(self, sheet: str, col: int) -> str:
        r = self._hdr.get(sheet)
        return self.sheets[sheet].get(r, {}).get(col, "") if r else ""

    def label_of(self, sheet: str, col: int) -> str:
        """Column header plus its group heading above (merged cell: nearest heading to the left),
        e.g. 'INITIAL SPARE PARTS - RECOMMENDED BY MANUFACTURER'."""
        r = self._hdr.get(sheet)
        h = re.sub(r"[.\s…]+", " ", self.header_of(sheet, col)).strip(" :")
        if not r:
            return h
        for up in range(1, 3):
            row = self.sheets[sheet].get(r - up, {})
            for c in range(col, max(col - 4, 0), -1):
                g = row.get(c)
                if g and not re.fullmatch(r"[\d.]+", g) and len(g) < 60:
                    return f"{g} - {h}" if h else g
        return h


def _ident_token(spare: str) -> str:
    return spare.split(":", 1)[1] if ":" in spare else spare


def verify(res: dict, cols, prev_rows, curr_rows, P: RawBook, N: RawBook) -> dict:
    C = res["C"]
    ignored = Counter()

    # Values the OTHER side's extraction already explains as a non-identity field
    # (model, serial, make, description). A token found in the file only because it
    # is, say, the equipment model there is not evidence that it is a tag / spare
    # there (seen: Rev 1 used the pump model as the tag; a cabinet model listed
    # later as a spare part).
    def other_field_values(rows):
        out = set()
        for r in rows:
            for f in ("EQPT MODEL", "EQPT SR NO", "EQPT MAKE", "DESCRIPTION OF PARTS"):
                v = r[C[f]]
                if v in (None, ""):
                    continue
                out.add(norm(v))
                out.update(norm(p) for p in _SPLIT.split(str(v)) if len(norm(p)) >= 3)
        return out
    p_other, n_other = other_field_values(prev_rows), other_field_values(curr_rows)
    n_ids = res["N"].tags | {_ident_token(s) for s in res["N"].spare_tags}
    p_ids = res["P"].tags | {_ident_token(s) for s in res["P"].spare_tags}

    def gone_from(book, tok, other, ids):
        return not book.has(tok) or (tok in other and tok not in ids)

    # -- tags / spares added & deleted --
    tags_deleted = {t for t in res["tags_deleted"] if gone_from(N, t, n_other, n_ids)}
    tags_added = {t for t in res["tags_added"] if gone_from(P, t, p_other, p_ids)}
    spares_deleted = {s for s in res["spares_deleted"] if gone_from(N, _ident_token(s), n_other, n_ids)}
    spares_added = {s for s in res["spares_added"] if gone_from(P, _ident_token(s), p_other, p_ids)}
    ignored["TAG DELETED (TAG STILL IN FILE)"] = len(res["tags_deleted"] - tags_deleted)
    ignored["TAG ADDED (TAG ALREADY IN PREVIOUS FILE)"] = len(res["tags_added"] - tags_added)
    ignored["SPARE DELETED (PART STILL IN FILE)"] = len(res["spares_deleted"] - spares_deleted)
    ignored["SPARE ADDED (PART ALREADY IN PREVIOUS FILE)"] = len(res["spares_added"] - spares_added)

    # -- field modifications: old value in previous source rows, new value in current --
    confirmed = {}
    for key, tags in res["changes"].items():
        ident, f, o, n = key
        tok = ident[4:] if ident.startswith("TAG:") else _ident_token(ident)
        pv, nv = P.values_with(tok), N.values_with(tok)
        o_ok = (norm(o) == "") or (norm(o) in pv)
        n_ok = (norm(n) == "") or (norm(n) in nv)
        if not (pv and nv):          # identifier not literally in the files (e.g. expanded tag range)
            o_ok = n_ok = True       # cannot verify -> keep extraction result
        if not (o_ok and n_ok) and ident.startswith("TAG:"):
            # Equipment attributes (model / serial / make) often sit in a header row away
            # from the tag cell: accept when the FILE really changed for that value.
            on, nn = norm(o), norm(n)
            o_ok = on == "" or P.has(on)
            n_ok = nn == "" or N.has(nn)
            o_ok = o_ok and n_ok and (on == "" or not N.has(on) or nn == "" or not P.has(nn))
            n_ok = o_ok
        if o_ok and n_ok:
            confirmed[key] = tags
        else:
            ignored["FIELD CHANGE NOT IN FILE"] += 1
    ok_fields = defaultdict(set)     # (ident) -> {fields}
    for (ident, f, _, _) in confirmed:
        ok_fields[ident].add(f)

    def explained_values(ident):
        old = Counter(); new = Counter()
        for (i, f, o, n), _ in confirmed.items():
            if i == ident:
                old[norm(o)] += 10 ** 6; new[norm(n)] += 10 ** 6
        return old, new

    def source_changed(tok, ident=None):
        """Did the source rows carrying this identifier change (beyond confirmed field edits)?"""
        a, b = P.values_with(tok), N.values_with(tok)
        if not a and not b:
            return True              # cannot verify -> trust extraction
        if ident:
            eo, en = explained_values(ident)
            a = Counter({k: v for k, v in a.items() if k not in eo})
            b = Counter({k: v for k, v in b.items() if k not in en})
        return a != b

    link_cache = {}

    def link_confirmed(tag, spare):
        k = (tag, spare)
        if k not in link_cache:
            # an empty tag has no source rows of its own -> only the spare's rows can confirm
            tag_changed = source_changed(tag) if tag else False
            link_cache[k] = tag_changed or source_changed(_ident_token(spare), spare)
        return link_cache[k]

    # -- rebuild row statuses from confirmed facts only --
    new_status, new_diffs = [], []
    for key, st, d in zip(res["N"].keys, res["row_status"], res["row_diffs"]):
        tag, spare = (key[0], key[1]) if key else (None, None)
        ident = ("TAG:" + tag) if spare == EQPT else spare
        if st == "ADDED":
            if (spare == EQPT and tag in tags_added) or (spare != EQPT and (spare in spares_added or tag in tags_added)):
                new_status.append(st); new_diffs.append(d); continue
            ignored["ROW ADDED (NOT CONFIRMED)"] += 1
            st, d = "UNCHANGED", []
        elif st == "ADDED TO TAG":
            if not link_confirmed(tag, spare):
                ignored["SPARE ADDED TO TAG (NOT CONFIRMED)"] += 1
                st = "UNCHANGED"
            d = [f for f in d if f in ok_fields.get(ident, ())]
        elif st == "MODIFIED":
            d = [f for f in d if f in ok_fields.get(ident, ())]
            if not d:
                st = "UNCHANGED"
        new_status.append(st); new_diffs.append(d)

    new_deleted = []
    for st, row, j in res["deleted_rows"]:
        key = res["P"].keys[j]
        tag, spare = key[0], key[1]
        keep = (st == "TAG DELETED" and tag in tags_deleted) or \
               (st == "DELETED" and spare in spares_deleted) or \
               (st == "REMOVED FROM TAG" and tag not in tags_deleted and link_confirmed(tag, spare)) or \
               (st == "CHECK MANUALLY")
        if st == "TAG DELETED" and tag not in tags_deleted:
            keep = False
        if keep:
            new_deleted.append((st, row, j))
        else:
            # Still in the current FILE but missing from the current EXTRACTION:
            # show it in place (orange) so the user sees it - never as deleted.
            new_deleted.append(("CHECK EXTRACTION", row, j))
            ignored[f"ROW {st} (NOT CONFIRMED)"] += 1

    moved = {}
    for s, (gone, new) in res["moved"].items():
        g = {t for t in gone if link_confirmed(t, s)}
        n_ = {t for t in new if link_confirmed(t, s)}
        if g or n_:
            moved[s] = (g, n_)
        else:
            ignored["SPARE MOVED BETWEEN TAGS (NOT CONFIRMED)"] += 1
    spares_modified = {i for (i, *_ ) in confirmed if not i.startswith("TAG:")}
    tags_modified = {i[4:] for (i, *_ ) in confirmed if i.startswith("TAG:")}
    common = (set(res["P"].spare_tags) & set(res["N"].spare_tags))

    out = dict(res)
    out.update(row_status=new_status, row_diffs=new_diffs, deleted_rows=new_deleted, changes=confirmed,
               tags_added=tags_added, tags_deleted=tags_deleted, tags_modified=tags_modified,
               spares_added=spares_added, spares_deleted=spares_deleted, spares_modified=spares_modified,
               spares_unchanged=common - spares_modified, moved=moved,
               ignored={k: v for k, v in ignored.items() if v})
    # Counts as they are in the current FILE (extraction-missed items still count)
    out["tags_in_file"] = len(res["N"].tags | (res["tags_deleted"] - tags_deleted))
    out["spares_in_file"] = len(set(res["N"].spare_tags) | (res["spares_deleted"] - spares_deleted))
    idents = (p_ids | n_ids) - {""}
    out["doc_changes"], out["other_changes"] = file_changes(P, N, confirmed, idents)
    return out


def file_changes(P: RawBook, N: RawBook, confirmed, idents=frozenset()):
    """
    Cell diff of same-named sheets, minus what the confirmed changes already explain.
    A data cell is reported only when its row carries the SAME tag / part number in
    both files: two different documents (or a restructured revision) put different
    spares on the same row, and a position-by-position diff of those is meaningless.
    Rows without any identifier (titles, project, dates) are document changes.
    """
    explained = {(norm(o), norm(n)) for (_, _, o, n) in confirmed}
    doc, other = Counter(), []

    def ids_of(cells):
        found = {v for v in cells.values() if v in idents}
        for v in cells.values():
            for p in _SPLIT.split(v):
                if norm(p) in idents:
                    found.add(norm(p))
        return found
    common = [s for s in N.sheets if s in P.sheets]
    for s in common:
        a, b = P.sheets[s], N.sheets[s]
        hdr = N._hdr.get(s) or P._hdr.get(s)
        for r in sorted(set(a) | set(b)):
            ra, rb = a.get(r, {}), b.get(r, {})
            ia, ib = ids_of(ra), ids_of(rb)
            data_row = bool(ia or ib)
            same_row = bool(ia & ib)
            for c in sorted(set(ra) | set(rb)):
                o, n = ra.get(c, ""), rb.get(c, "")
                if o == n or (o, n) in explained or re.sub(r"[^A-Z0-9]", "", o) == re.sub(r"[^A-Z0-9]", "", n):
                    continue                            # same value / only spacing or punctuation differs
                label_only = (not o or not n) and any(w in (o or n) for w in _HDR_WORDS) \
                    and len(o or n) < 40 and not _row_identity(rb or ra)
                if _DOC_RE.search(o) or _DOC_RE.search(n) or (hdr and r <= hdr) or label_only or not data_row:
                    doc[(o[:60], n[:60])] += 1          # header/footer: revision, date, title
                    continue
                if not same_row:
                    continue                            # different spare/tag on this row -> not comparable
                header = N.label_of(s, c) or P.label_of(s, c) or f"COLUMN {openpyxl.utils.get_column_letter(c)}"
                other.append({"sheet": s, "cell": f"{openpyxl.utils.get_column_letter(c)}{r}",
                              "field": header, "old": o, "new": n,
                              "row_id": sorted(ia & ib)[0]})
    return doc, other


def _row_identity(cells: dict[int, str]) -> str:
    """Best human handle for a source row: a tag-like or part-like value."""
    for v in cells.values():
        if re.search(r"\d{2,}-[A-Z0-9]+-[A-Z]+-\d{3,}", v) or re.search(r"^[A-Z]{2,}-\d{3,}", v):
            return v
    return ""
