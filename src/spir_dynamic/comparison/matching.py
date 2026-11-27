"""
Row matching for the SPIR comparison (source file -> comparison file).

Rows come from the ONE extraction engine (pipeline.extract_output_rows).
Matching is per ROW with key (TAG, SPARE) where SPARE = part number, else
drawing number; equipment rows (a TAG with no ITEM NUMBER) use (TAG, <EQPT>).
Spare-level statuses are derived from the row matches, so a spare that only
changes tags is reported as MOVED, never as added + deleted.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from openpyxl.styles import PatternFill

YELLOW = PatternFill("solid", fgColor="FFFF00")   # ADDED
RED = PatternFill("solid", fgColor="FF9999")      # DELETED
BLUE = PatternFill("solid", fgColor="9DC3E6")     # MODIFIED
ORANGE = PatternFill("solid", fgColor="F4B183")   # CHECK MANUALLY / CHECK EXTRACTION
GREEN = PatternFill("solid", fgColor="92D050")    # SIMILAR (in both files, SPIR TYPE mode)

# Compared fields. Excluded on purpose: ITEM NUMBER (restarts per sheet),
# POSITION NUMBER / OLD MATERIAL-SPF NUMBER (computed from order), SHEET
# (layout), UNIT PRICE (QAR) + NEW DESCRIPTION (derived), SPIR NO (has REV).
SPARE_FIELDS = ["DESCRIPTION OF PARTS", "DWG NO INCL POSN NO", "MANUFACTURER PART NUMBER",
                "QUANTITY IDENTICAL PARTS FITTED", "MATERIAL SPECIFICATION", "SUPPLIER/ OCM NAME",
                "CURRENCY", "UNIT PRICE", "DELIVERY TIME IN WEEKS", "MIN MAX STOCK LVLS QTY",
                "UNIT OF MEASURE", "SAP NUMBER", "CLASSIFICATION OF PARTS"]
TAG_FIELDS = ["EQPT MAKE", "EQPT MODEL", "EQPT SR NO", "EQPT QTY", "DESCRIPTION OF PARTS"]
EQPT = "<EQPT>"
DELETED_STATUSES = ("DELETED", "REMOVED FROM TAG", "TAG DELETED")

_NUM_RE = re.compile(r"^[-+]?(0|[1-9][\d,]*)(\.\d+)?$")   # no leading zeros: '0012345' stays a code


def _num(f: float) -> str:
    # full precision: 1316460 and 1316450 must never collapse to '1.31646e+06'
    return str(int(f)) if f.is_integer() else repr(round(f, 6))


def norm(v) -> str:
    """Value used for comparing: case/whitespace-insensitive, 1 == 1.0 == '1' (full precision)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v).upper()
    if isinstance(v, (int, float)):
        return _num(float(v))
    s = re.sub(r"\s+", " ", str(v)).strip().upper()
    if _NUM_RE.match(s):
        try:
            return _num(float(s.replace(",", "")))
        except ValueError:
            pass
    return s


def _compact(v) -> str:
    return re.sub(r"[^A-Z0-9]", "", norm(v))


def same(fld: str, a, b) -> bool:
    """
    Field equality used for MODIFIED. DWG NO is formatting-tolerant:
    punctuation/spacing differences and an added/removed document prefix
    ("X" vs "VEN-...-0102-X") are NOT a change. Anything else is.
    """
    if norm(a) == norm(b):
        return True
    if fld in ("SAP NUMBER", "MANUFACTURER PART NUMBER"):
        return _compact(a) == _compact(b)        # '10619144+10619143' == '10619144 + 10619143'
    if fld == "DWG NO INCL POSN NO":
        ca, cb = _compact(a), _compact(b)
        if not ca or not cb:
            return ca == cb
        short, long_ = sorted((ca, cb), key=len)
        # prefix tolerance only for a real drawing/part code, so "10" vs "0" stays a change
        return ca == cb or (len(short) >= 6 and long_.endswith(short))
    return False


def show(v):
    """Value used for display: drop float noise like 6526.650000000001."""
    if isinstance(v, float):
        return round(v, 2) if v != int(v) else int(v)
    return v


def split_spir(spir_no: str):
    """'VEN-...-0116-REV-3' -> ('VEN-...-0116', '3'); no REV marker -> (spir_no, None)."""
    m = re.match(r"^(.*?)[\s\-_.]*REV[\s\-_.]*([A-Z0-9]+)$", (spir_no or "").strip().upper())
    return (m.group(1), m.group(2)) if m else ((spir_no or "").strip().upper(), None)


@dataclass
class Side:
    rows: list
    keys: list = field(default_factory=list)          # row index -> key | None (unresolved)
    by_key: dict = field(default_factory=dict)        # key -> row index
    tags: set = field(default_factory=set)
    spare_tags: dict = field(default_factory=lambda: defaultdict(set))  # spare -> {tags}
    duplicates: list = field(default_factory=list)


def index(C, rows) -> Side:
    s = Side(rows)
    seen = Counter()
    for i, r in enumerate(rows):
        tag = norm(r[C["TAG NO"]])
        pn = norm(r[C["MANUFACTURER PART NUMBER"]])
        dwg = norm(r[C["DWG NO INCL POSN NO"]])
        # Same equipment/spare split as pipeline._apply_equipment_row_fields:
        # a row with a TAG but no ITEM NUMBER is the equipment row (its PART
        # NUMBER is a copy of EQPT MODEL, so it must not be used as identity).
        is_eqpt = bool(tag) and norm(r[C["ITEM NUMBER"]]) in ("", "NONE")
        if is_eqpt:
            spare = EQPT
        elif pn:
            spare = "PN:" + pn
        elif dwg:
            spare = "DWG:" + dwg
        else:
            s.keys.append(None)          # no identity -> manual check, never guessed
            continue
        key = (tag, spare)
        seen[key] += 1
        if seen[key] > 1:                # same spare twice under one tag
            s.duplicates.append(i)
            key = (tag, spare, seen[key])
        s.keys.append(key)
        s.by_key[key] = i
        if tag:
            s.tags.add(tag)
        if spare != EQPT:
            s.spare_tags[spare].add(tag)
    return s


def compare(cols, prev_rows, curr_rows) -> dict:
    """Row-level comparison of the source (prev) and comparison (curr) rows."""
    C = {c: i for i, c in enumerate(cols)}
    P, N = index(C, prev_rows), index(C, curr_rows)
    tags_added = N.tags - P.tags
    tags_deleted = P.tags - N.tags
    spares_added = set(N.spare_tags) - set(P.spare_tags)
    spares_deleted = set(P.spare_tags) - set(N.spare_tags)

    row_status, row_diffs = [], []        # per current row
    changes = defaultdict(set)            # (spare|tag, field, old, new) -> {tags}
    spares_modified, tags_modified = set(), set()

    # Rows with no identity at all: accept only an EXACT twin from the source
    # file (same tag + every compared field) as UNCHANGED - never a guess.
    def signature(r):
        return (norm(r[C["TAG NO"]]),) + tuple(norm(r[C[f]]) for f in SPARE_FIELDS)
    prev_twins = Counter(signature(prev_rows[j]) for j, k in enumerate(P.keys) if k is None)

    for i, key in enumerate(N.keys):
        if key is None:
            sig = signature(curr_rows[i])
            if prev_twins[sig] > 0:
                prev_twins[sig] -= 1
                row_status.append("UNCHANGED")
            else:
                row_status.append("CHECK MANUALLY")
            row_diffs.append([])
            continue
        tag, spare = key[0], key[1]
        if key not in P.by_key and (tag, spare) in P.by_key and (spare == EQPT or len(key) == 3):
            # The same tag listed again on another sheet (main + continuation,
            # several main sheets): its equipment row repeats, and a spare can be
            # listed under it again. That tag already had the spare in the source
            # file, so compare with that row - it is not ADDED / ADDED TO TAG.
            key = (tag, spare)
        if key not in P.by_key:
            if spare == EQPT:
                st = "ADDED" if tag in tags_added else "CHECK MANUALLY"
            elif spare in spares_added or tag in tags_added:
                st = "ADDED"
            else:
                st = "ADDED TO TAG"       # spare existed before, on other tags
            if st == "ADDED" or spare == EQPT:
                row_status.append(st)
                row_diffs.append([])
                continue
            # spare known from other tags: still compare its attributes so a
            # moved-and-changed spare is reported as MODIFIED too
            ref = next(k for k in P.by_key if k[1] == spare)
            o, n = prev_rows[P.by_key[ref]], curr_rows[i]
            d = [f for f in SPARE_FIELDS if f != "QUANTITY IDENTICAL PARTS FITTED" and not same(f, o[C[f]], n[C[f]])]
            for f in d:
                changes[(spare, f, str(show(o[C[f]])), str(show(n[C[f]])))].add(tag)
                spares_modified.add(spare)
            row_status.append(st)
            row_diffs.append(d)
            continue
        o, n = prev_rows[P.by_key[key]], curr_rows[i]
        flds = TAG_FIELDS if spare == EQPT else SPARE_FIELDS
        d = [f for f in flds if not same(f, o[C[f]], n[C[f]])]
        row_diffs.append(d)
        row_status.append("MODIFIED" if d else "UNCHANGED")
        for f in d:
            changes[(spare if spare != EQPT else "TAG:" + tag, f, str(show(o[C[f]])), str(show(n[C[f]])))].add(tag)
        if d:
            (tags_modified.add(tag) if spare == EQPT else spares_modified.add(spare))

    deleted_rows = []                     # (status, prev row, prev index)
    for key, j in sorted(P.by_key.items(), key=lambda kv: kv[1]):
        if key in N.by_key:
            continue
        tag, spare = key[0], key[1]
        if (spare == EQPT or len(key) == 3) and (tag, spare) in N.by_key:
            continue                      # repeat of a row the tag still has (see above)
        if tag in tags_deleted:
            st = "TAG DELETED"
        elif spare == EQPT:
            st = "CHECK MANUALLY"
        elif spare in spares_deleted:
            st = "DELETED"
        else:
            st = "REMOVED FROM TAG"
        deleted_rows.append((st, prev_rows[j], j))

    # Tag-association change, ignoring tags that are themselves new/deleted
    # (those are already reported as NEW TAG / TAG DELETED).
    moved = {}
    for s in set(P.spare_tags) & set(N.spare_tags):
        gone = P.spare_tags[s] - N.spare_tags[s] - tags_deleted
        new = N.spare_tags[s] - P.spare_tags[s] - tags_added
        if gone or new:
            moved[s] = (gone, new)
    common = set(P.spare_tags) & set(N.spare_tags)
    return dict(C=C, P=P, N=N, row_status=row_status, row_diffs=row_diffs, changes=changes,
                tags_added=tags_added, tags_deleted=tags_deleted, tags_modified=tags_modified,
                spares_added=spares_added, spares_deleted=spares_deleted,
                spares_modified=spares_modified, spares_unchanged=common - spares_modified,
                moved=moved, deleted_rows=deleted_rows)
