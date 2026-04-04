"""
extraction/post_processor.py
-----------------------------
Post-processing: adds POSITION NUMBER and OLD MATERIAL NUMBER/SPF NUMBER.

POSITION NUMBER
  - Always "0010" for all rows (header and detail)

OLD MATERIAL NUMBER / SPF NUMBER (OMN)
  - Prefer up to 18 characters (base + hyphen + suffix); may be shorter if the
    real SPIR segments do not fill 18 — never invent digits or cycle project
    characters to pad.
  - Numeric project: include full project number as in the SPIR; keep seq/sheet
    digits as-is until over-length compression runs.
  - Over 18: shorten letter-only project tail, strip leading zeros on numeric
    segments (right to left), fuse hyphens / suffix rules as before.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

TARGET_LEN = 18

# First SPIR segment treated as vendor (never part of OMN) when present.
_VENDOR_PREFIXES = frozenset(
    {"VEN", "DEM", "CON", "EPC", "CTR", "SUB"}
)


# ---------------------------------------------------------------------------
# OMN helpers
# ---------------------------------------------------------------------------

def _normalize_spir_raw(spir_no: str) -> str:
    s = (spir_no or "").strip()
    s = s.split(",")[0].strip()
    s = re.sub(r"(?i)\.pdf\b", "", s)
    s = re.sub(r"\([^)]*\)", "", s)
    # Drop -REV... and anything after (e.g. -REV.5_CODED COPY_1).
    s = re.split(r"(?i)-rev\b", s, maxsplit=1)[0].strip()
    s = re.sub(r"(?i)\brev\.?\s*[a-z0-9.-]*", "", s)
    return s.strip()


def _split_spir_segments(spir_no: str) -> list[str]:
    """Split SPIR on hyphens; keep non-empty trimmed tokens."""
    s = _normalize_spir_raw(spir_no)
    if not s:
        return []
    parts = re.split(r"\s*-+\s*", s)
    return [p.strip() for p in parts if p.strip()]


def _is_digit_only_segment(seg: str) -> bool:
    return bool(seg) and seg.isdigit()


def _maybe_drop_location_segment(segments: list[str]) -> list[str]:
    """
    Numeric project + non-numeric middle (MTY, M4TY, RLCSF3, …) + numeric next
    → drop the middle token. VP is not a location code — keep for VP fusion.
    """
    if len(segments) < 3:
        return segments
    proj, mid, nxt = segments[0], segments[1], segments[2]
    if not _is_digit_only_segment(proj):
        return segments
    if _is_digit_only_segment(mid):
        return segments
    if mid.upper() == "VP":
        return segments
    if not _is_digit_only_segment(nxt):
        return segments
    return [segments[0]] + segments[2:]


def _normalize_vp_4400(body: list[str]) -> list[str]:
    """
    4400-VP-30-00-10-053-2 → 4400, 3010, 53, 2
    4400-VP-30-00-10-053   → 4400, 3010, 53
    """
    if len(body) < 6 or not body[0].isdigit() or body[1].upper() != "VP":
        return body
    rest = body[2:]
    if len(rest) == 5:
        a, b, c, d, e = rest
        if not all(x.isdigit() for x in (a, b, c, d, e)):
            return body
        if b != "00":
            return body
        mid = a + c
        pen = str(int(d))
        return [body[0], mid, pen, e]
    if len(rest) == 4:
        a, b, c, d = rest
        if not all(x.isdigit() for x in (a, b, c, d)):
            return body
        if b != "00":
            return body
        mid = a + c
        pen = str(int(d))
        return [body[0], mid, pen]
    return body


def _drop_trailing_sheet_rev_letter(body: list[str]) -> list[str]:
    """Drop trailing single letter after digits (e.g. sheet rev A)."""
    if (
        len(body) >= 2
        and len(body[-1]) == 1
        and body[-1].isalpha()
        and body[-2].isdigit()
    ):
        return body[:-1]
    return body


def _canonical_omn_body_segments(segments: list[str]) -> tuple[list[str], str]:
    """
    Vendor strip → location drop → VP fusion → digit normalize → drop trailing A.
    Returns (body_segments, project_token after vendor drop for future use).
    """
    if not segments:
        return [], ""
    body = list(segments)
    if len(body[0]) <= 4 and body[0].upper() in _VENDOR_PREFIXES:
        body = body[1:]
    if not body:
        return [], ""
    project_token = body[0]
    body = _maybe_drop_location_segment(body)
    body = _normalize_vp_4400(body)
    body = _drop_trailing_sheet_rev_letter(body)
    return body, project_token


def _omn_segments_after_vendor(segments: list[str]) -> tuple[list[str], str]:
    """Backward-compatible name for tests / callers."""
    return _canonical_omn_body_segments(segments)


def _clean_spir_base(spir_no: str) -> str:
    segs, _ = _omn_segments_after_vendor(_split_spir_segments(spir_no))
    return "-".join(segs)


def _build_suffix(sheet_idx: int, line_idx: int, total_main_sheets: int) -> str:
    if total_main_sheets <= 1:
        if line_idx < 100:
            return f"L{line_idx:02d}"
        return f"L{line_idx}"
    return f"{sheet_idx}L{line_idx}"


def _omn_total_len(segs: list[str], suffix: str, suffix_fused: bool) -> int:
    b = "-".join(segs)
    if suffix_fused:
        return len(b) + len(suffix)
    return len(b) + 1 + len(suffix)


def _omn_render(segs: list[str], suffix: str, suffix_fused: bool) -> str:
    base = "-".join(segs)
    if suffix_fused:
        return base + suffix
    return f"{base}-{suffix}"


def _try_shorten_alpha_project(segs: list[str]) -> bool:
    if not segs or not segs[0].isalpha() or len(segs[0]) <= 1:
        return False
    segs[0] = segs[0][:-1]
    return True


def _try_lz_strip_one_right(segs: list[str]) -> bool:
    for i in range(len(segs) - 1, 0, -1):
        seg = segs[i]
        if len(seg) >= 2 and seg.isdigit() and seg[0] == "0":
            segs[i] = seg[1:]
            return True
    return False


def _try_fuse_vp_4400_tail(segs: list[str]) -> bool:
    """
    4400-VP… produces …-3010-53-2: merge the last two numeric tokens (53+2→532)
    before pulling the multi-main index into the suffix, so we get …-532-1L10
    not …-53-21L10.
    """
    if len(segs) != 4:
        return False
    if segs[0] != "4400":
        return False
    if len(segs[1]) != 4 or not segs[1].isdigit():
        return False
    if not (segs[2].isdigit() and segs[3].isdigit()):
        return False
    segs[2] = segs[2] + segs[3]
    segs.pop(3)
    return True


def _try_fuse_discipline_subgroup(
    segs: list[str], suffix: str, suffix_fused: bool
) -> bool:
    """
    Merge short numeric discipline + subgroup (…-5-43-… → …-543-…) for
    numeric projects. Skipped for letter-only project (MEWTP). Skipped for
    4391-2-43 / 4391-4-43 (M4TY and 4-discipline SPIRs keep 2+43 / 4+43 split).
    Skipped when the base already fits 18 with the suffix, or when merging
    would shorten the base so much that we'd pad the project (corrupts 4391).
    """
    if len(segs) < 4 or not segs[0].isdigit():
        return False
    a, b = segs[1], segs[2]
    if not (a.isdigit() and b.isdigit() and len(a) <= 2 and len(b) <= 2):
        return False
    if segs[0].startswith("4391") and a in ("2", "4") and b == "43":
        return False
    base = "-".join(segs)
    extra = len(suffix) + (0 if suffix_fused else 1)
    if len(base) + extra <= TARGET_LEN:
        return False
    merged_len = len(base) - 1
    if merged_len + extra < TARGET_LEN:
        return False
    segs[1] = a + b
    segs.pop(2)
    return True


def _try_fuse_right_pair(segs: list[str]) -> bool:
    if len(segs) < 2:
        return False
    segs[-2] = segs[-2] + segs[-1]
    segs.pop()
    return True


def _try_fuse_suffix_leading_digits(
    segs: list[str], suffix: str, suffix_fused: bool
) -> tuple[str, bool, bool]:
    if suffix_fused:
        return suffix, suffix_fused, False
    m = re.fullmatch(r"(\d+)(L\d+)", suffix)
    if not m or not segs or not segs[-1].isdigit():
        return suffix, suffix_fused, False
    d, lpart = m.group(1), m.group(2)
    segs[-1] = segs[-1] + d
    return lpart, True, True


def _try_truncate_last_segment(segs: list[str]) -> bool:
    if not segs or len(segs[-1]) <= 1:
        return False
    segs[-1] = segs[-1][:-1]
    return True


def _lz_strip_then_maybe_merge_structure(
    segs: list[str], suffix: str, suffix_fused: bool
) -> bool:
    """
    After stripping one leading zero, optionally merge discipline+subgroup (or
    4400 tail) when the seq token looks like a drawing no. at 00x (e.g. 0001→001).
    Skips chaining when the strip yields 016-style tokens (no leading "00"), so
    0016 can become 16 without forcing 5+43→543 first.
    """
    if not _try_lz_strip_one_right(segs):
        return False
    if (
        len(segs) >= 4
        and segs[3].isdigit()
        and 2 <= len(segs[3]) <= 3
        and segs[3].startswith("00")
    ):
        if not _try_fuse_discipline_subgroup(segs, suffix, suffix_fused):
            _try_fuse_vp_4400_tail(segs)
    else:
        _try_fuse_vp_4400_tail(segs)
    return True


def _fit_omn_body_and_suffix(body_segs: list[str], suffix: str, _project_token: str) -> str:
    """
    Shrink base + suffix to at most TARGET_LEN when over; never pad with
    invented digits or repeated project characters. Result may be shorter than 18.
    """
    segs = list(body_segs)
    if not segs:
        segs = [""]
    suf = suffix
    fused = False

    def tl() -> int:
        return _omn_total_len(segs, suf, fused)

    guard = 0
    while tl() > TARGET_LEN and guard < 500:
        guard += 1
        if _try_shorten_alpha_project(segs):
            continue
        if _lz_strip_then_maybe_merge_structure(segs, suf, fused):
            continue
        if _try_fuse_discipline_subgroup(segs, suf, fused):
            continue
        if _try_fuse_vp_4400_tail(segs):
            continue
        ns, nf, ch = _try_fuse_suffix_leading_digits(segs, suf, fused)
        if ch:
            suf, fused = ns, nf
            continue
        if _try_fuse_right_pair(segs):
            continue
        if _try_shorten_alpha_project(segs):
            continue
        if _lz_strip_then_maybe_merge_structure(segs, suf, fused):
            continue
        if _try_fuse_discipline_subgroup(segs, suf, fused):
            continue
        if _try_fuse_vp_4400_tail(segs):
            continue
        ns, nf, ch = _try_fuse_suffix_leading_digits(segs, suf, fused)
        if ch:
            suf, fused = ns, nf
            continue
        if _try_fuse_right_pair(segs):
            continue
        if _try_truncate_last_segment(segs):
            continue
        break

    out = _omn_render(segs, suf, fused)
    if len(out) > TARGET_LEN:
        out = out[:TARGET_LEN]
    return out


def _item_to_line_index(item_value) -> int:
    try:
        num = int(float(str(item_value).strip()))
        return max(num, 1)
    except (ValueError, TypeError):
        return 1


def build_omn(spir_no: str, sheet_idx: int, line_idx: int,
              total_main_sheets: int = 1) -> str:
    raw_spir = spir_no
    body_segs, project_token = _canonical_omn_body_segments(
        _split_spir_segments(raw_spir)
    )
    suffix = _build_suffix(sheet_idx, line_idx, total_main_sheets)

    print("OMN INPUT:", raw_spir)
    print("MAIN SHEET INDEX:", sheet_idx)
    print("LINE NUMBER:", suffix)

    omn = _fit_omn_body_and_suffix(body_segs, suffix, project_token)

    print("OMN FINAL:", omn)
    return omn


# ---------------------------------------------------------------------------
# Column index helpers
# ---------------------------------------------------------------------------

def _get_ci():
    from spir_dynamic.extraction.output_schema import CI
    return CI


def _col(ci: dict, *names) -> int | None:
    for name in names:
        idx = ci.get(name)
        if idx is not None:
            return idx
    return None


# ---------------------------------------------------------------------------
# Sheet index tracker
# ---------------------------------------------------------------------------

_NON_MAIN_PATTERNS = re.compile(
    r"(?:continuation|cont\.?\s|continued|overflow|annexure|annex\b)",
    re.IGNORECASE,
)


def _sheet_name_is_continuation_or_annexure(name_norm: str) -> bool:
    return bool(_NON_MAIN_PATTERNS.search(name_norm))


def _true_main_sheet_name_count(names_norm: set[str]) -> int:
    return sum(1 for n in names_norm if not _sheet_name_is_continuation_or_annexure(n))


class SheetTracker:
    """
    Maps sheet names to main-sheet indices for OMN suffix generation.

    Only MAIN sheets increment the index.
    Continuation and annexure sheets inherit the index of the most recent
    main sheet — they are part of the same logical block.
    """

    def __init__(self, main_sheet_names: set[str] | None = None):
        self._main_names_raw: set[str] = main_sheet_names or set()
        self._main_names_norm: set[str] = {
            self._norm(name) for name in self._main_names_raw if str(name).strip()
        }
        self._sheet_to_idx: dict[str, int] = {}
        self._main_counter = 0
        self._current_main_idx = 1

        if self._main_names_norm:
            c = _true_main_sheet_name_count(self._main_names_norm)
            self._total_main_sheets = max(1, c)
        else:
            self._total_main_sheets = 1

    @property
    def total_main_sheets(self) -> int:
        return self._total_main_sheets

    def get_sheet_idx(self, sheet: str | None) -> int:
        key = (sheet or "MAIN").strip()
        key_norm = self._norm(key)
        if key in self._sheet_to_idx:
            return self._sheet_to_idx[key]

        is_main = self._is_main(key_norm)

        if is_main:
            self._main_counter += 1
            self._current_main_idx = self._main_counter
            if not self._main_names_norm and self._main_counter > self._total_main_sheets:
                self._total_main_sheets = self._main_counter
            print(f"Sheet: {key}")
            print(f"Type: MAIN")
            print(f"Main Index: {self._current_main_idx}")
        else:
            print(f"Sheet: {key}")
            print(f"Type: CONTINUATION/ANNEXURE")
            print(f"Main Index: {self._current_main_idx}")

        self._sheet_to_idx[key] = self._current_main_idx
        return self._current_main_idx

    @staticmethod
    def _norm(name: str) -> str:
        return str(name).strip().upper()

    def _is_main(self, key_norm: str) -> bool:
        if _sheet_name_is_continuation_or_annexure(key_norm):
            return False
        if self._main_names_norm:
            return key_norm in self._main_names_norm
        return True


# ---------------------------------------------------------------------------
# Main post-processor
# ---------------------------------------------------------------------------

def post_process_rows(
    rows: list[list],
    spir_no: str,
    main_sheet_names: set[str] | None = None,
) -> list[list]:
    if not rows:
        return rows

    ci = _get_ci()

    tag_col = _col(ci, "TAG NO")
    item_col = _col(ci, "ITEM NUMBER")
    pos_col = _col(ci, "POSITION NUMBER")
    spf_col = _col(ci, "OLD MATERIAL NUMBER/SPF NUMBER")
    sheet_col = _col(ci, "SHEET")

    if pos_col is None and spf_col is None:
        return rows

    sheet_tracker = SheetTracker(main_sheet_names)
    spir_no_clean = (spir_no or "").strip()

    pos_counter: dict[str, int] = {}

    for row in rows:
        ncols = len(row)

        tag = row[tag_col] if tag_col is not None and tag_col < ncols else None
        item = row[item_col] if item_col is not None and item_col < ncols else None
        sheet = row[sheet_col] if sheet_col is not None and sheet_col < ncols else None

        tag_key = str(tag or "__NONE__").strip().upper()
        is_spare = item is not None and str(item).strip() not in ("", "None")

        if pos_col is not None and pos_col < ncols:
            if not is_spare:
                row[pos_col] = "0010"
                if tag_key not in pos_counter:
                    pos_counter[tag_key] = 10
            else:
                if tag_key not in pos_counter:
                    pos_counter[tag_key] = 10
                row[pos_col] = str(pos_counter[tag_key]).zfill(4)
                pos_counter[tag_key] += 10

        if spf_col is not None and spf_col < ncols and is_spare and spir_no_clean:
            sheet_idx = sheet_tracker.get_sheet_idx(sheet)
            line_idx = _item_to_line_index(item)
            row[spf_col] = build_omn(
                spir_no_clean, sheet_idx, line_idx,
                total_main_sheets=sheet_tracker.total_main_sheets,
            )

    return rows
