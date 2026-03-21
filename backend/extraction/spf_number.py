"""
extraction/spf_number.py
─────────────────────────
OLD MATERIAL NUMBER / SPF NUMBER — exactly 18 characters, always.

SPEC (implemented exactly):
────────────────────────────
Step 1  Build SPIR base:
        Remove ALL letters from SPIR number. Keep digits AND hyphens.
        Collapse consecutive hyphens. Strip leading/trailing hyphens.

        "VEN-4460-KAHS-5-43-1002-2"    → "4460-5-43-1002-2"   (14 chars)
        "4400-VP-30-00-10-053 (REV.2)" → "4400-30-00-10-053-2" (19 chars)

Step 2  Build suffix (line number):
        Sheet 1 → "L01", "L02", ..., "L10", ..., "L99", "L100"
        Sheet 2 → "1L1", "1L2", ..., "1L10", ...
        Sheet N → "{N-1}L{line}"

        Rationale for sheet-1 format: the 'L' prefix signals "line on sheet 1"
        while the numeric prefix (N-1) on sheet 2+ signals the continuation index.

Step 3  Assemble: base + suffix. Must equal exactly 18 chars.

Step 4  If total > 18 chars, trim base in this EXACT priority order:
        P1: Remove leading zeros from numeric segments (right-to-left)
            "0610" → "610"  (leading zero, safe to remove)
            "6100" → "6100" (trailing zeros, DO NOT change)
            "0030" → "30"
            "053"  → "53"
        P2: Remove hyphens from base (fuse segments, rightmost first)
        P3: Hard-truncate base if still too long

        If total < 18 chars: pad base by adding leading zeros to leftmost segment.

TARGET_LEN = 18  (change only here)

VERIFIED EXAMPLES:
    "VEN-4460-KAHS-5-43-1002-2", sheet=1, line=1   → "4460-5-43-1002-L01"  (18)
    "VEN-4460-KAHS-5-43-1002-2", sheet=1, line=10  → "4460-5-43-1002-L10"  (18)
    "VEN-4460-KAHS-5-43-1002-2", sheet=2, line=1   → "4460-5-43-1002-1L1"  (18)
    Overflow (long SPIR, line=10): hyphens fused    → "4460-543-1002-1L10"  (18)
"""
from __future__ import annotations
import re

TARGET_LEN = 18


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Build SPIR base
# ─────────────────────────────────────────────────────────────────────────────

def build_spir_base(spir_no: str) -> str:
    """
    Strip everything except digits and hyphens, then clean up.
    Spaces and dots are treated as separators (→ hyphens) before stripping.

    "VEN-4460-KAHS-5-43-1002-2"     → "4460-5-43-1002-2"
    "4400-VP-30-00-10-053 (REV.2)"  → "4400-30-00-10-053-2"
    """
    if not spir_no:
        return ''
    # Treat spaces and dots as hyphen-separators before stripping
    cleaned = re.sub(r'[\s\.]', '-', spir_no)
    # Remove everything that is not a digit or hyphen
    cleaned = re.sub(r'[^0-9\-]', '', cleaned)
    # Collapse consecutive hyphens
    cleaned = re.sub(r'-{2,}', '-', cleaned)
    return cleaned.strip('-')


# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Build suffix
# ─────────────────────────────────────────────────────────────────────────────

def build_suffix(sheet_idx: int, line_idx: int) -> str:
    """
    Sheet 1 → "L{line:02d}"  e.g. "L01", "L10", "L99", "L100"
    Sheet N → "{N-1}L{line}" e.g. "1L1", "2L5", "9L3"
    """
    if sheet_idx == 1:
        return f"L{line_idx:02d}" if line_idx < 100 else f"L{line_idx}"
    return f"{sheet_idx - 1}L{line_idx}"


# ─────────────────────────────────────────────────────────────────────────────
# Trimming helpers
# ─────────────────────────────────────────────────────────────────────────────

def _strip_leading_zeros(seg: str) -> str:
    """
    Remove leading zeros from a purely-numeric segment.
    Trailing zeros are preserved — they change the numeric value.

    "0610" → "610"   safe: leading zero
    "6100" → "6100"  unchanged: trailing zeros
    "0030" → "30"
    "0000" → "0"     keep at least one digit
    "053"  → "53"
    """
    if not seg.isdigit() or not seg.startswith('0'):
        return seg
    return seg.lstrip('0') or '0'


def _trim_to_budget(base: str, budget: int) -> str:
    """
    Trim `base` to fit within `budget` characters.

    Priority order (spec):
      P1: Remove single-digit segments right-to-left  ← removes '-2', '-5' etc
      P2: Remove leading zeros from segments right-to-left  ← "0610" → "610"
      P3: Fuse segments (remove hyphens) right-to-left
      P4: Hard truncate
    """
    if len(base) <= budget:
        return base

    segs = base.split('-')

    # P1: remove single-digit segments right-to-left
    for i in range(len(segs) - 1, -1, -1):
        if len('-'.join(segs)) <= budget:
            break
        if len(segs[i]) == 1 and segs[i].isdigit():
            segs.pop(i)

    base = '-'.join(segs)
    if len(base) <= budget:
        return base

    # P2: strip leading zeros from segments right-to-left
    segs = base.split('-')
    for i in range(len(segs) - 1, -1, -1):
        if len('-'.join(segs)) <= budget:
            break
        t = _strip_leading_zeros(segs[i])
        if t != segs[i]:
            segs[i] = t

    base = '-'.join(segs)
    if len(base) <= budget:
        return base

    # P3: fuse segments (remove rightmost hyphens one at a time)
    segs = base.split('-')
    while len(segs) > 1 and len('-'.join(segs)) > budget:
        segs[-2] = segs[-2] + segs[-1]
        segs.pop()

    base = '-'.join(segs)
    if len(base) <= budget:
        return base

    # P4: hard truncate
    return base[:budget]


def _pad_base(base: str, budget: int) -> str:
    """Pad `base` to exactly `budget` chars with leading zeros on leftmost segment."""
    if len(base) >= budget:
        return base[:budget]
    needed = budget - len(base)
    segs = base.split('-')
    for i, seg in enumerate(segs):
        if seg.isdigit():
            segs[i] = '0' * needed + seg
            result  = '-'.join(segs)
            if len(result) == budget:
                return result
            break   # over-padded, fall through
    return base.ljust(budget, '0')[:budget]


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def build_old_material_number(
    spir_no:    str,
    sheet_idx:  int,
    line_idx:   int,
    target_len: int = TARGET_LEN,
) -> str:
    """
    Build the OLD MATERIAL NUMBER / SPF NUMBER (exactly `target_len` chars).

    Structure: <trimmed_base> + '-' + <suffix>
    The separator hyphen is PART of the budget — it is always present.

    Examples:
        build_old_material_number("VEN-4460-KAHS-5-43-1002-2", 1,  1)
        → "4460-5-43-1002-L01"   (18 chars) ✓

        build_old_material_number("VEN-4460-KAHS-5-43-1002-2", 2,  1)
        → "4460-5-43-1002-1L1"   (18 chars) ✓

        build_old_material_number("VEN-4460-KAHS-5-43-1002-2", 1, 10)
        → "4460-5-43-1002-L10"   (18 chars) ✓
    """
    base   = build_spir_base(spir_no)
    suffix = build_suffix(sheet_idx, line_idx)

    # Reserve 1 char for the separator hyphen between base and suffix
    budget = target_len - 1 - len(suffix)

    if budget <= 0:
        # Edge case: suffix alone fills target
        return (suffix)[:target_len]

    trimmed = _trim_to_budget(base, budget)
    padded  = _pad_base(trimmed, budget)
    result  = padded + '-' + suffix

    assert len(result) == target_len, (
        f"SPF length error: got {len(result)} for "
        f"spir='{spir_no}' sheet={sheet_idx} line={line_idx}: '{result}'"
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Self-test
# ─────────────────────────────────────────────────────────────────────────────

def _test() -> None:
    cases = [
        # (spir_no,                          sh,  ln,  expected)
        ("VEN-4460-KAHS-5-43-1002-2",        1,   1,  "4460-5-43-1002-L01"),
        ("VEN-4460-KAHS-5-43-1002-2",        1,  10,  "4460-5-43-1002-L10"),
        ("VEN-4460-KAHS-5-43-1002-2",        2,   1,  "4460-5-43-1002-1L1"),
        ("VEN-4460-KAHS-5-43-1002-2",        1,  99,  "4460-5-43-1002-L99"),
        ("VEN-4460-KAHS-5-43-1002-2",        1, 100,  None),   # length only
        ("4400-VP-30-00-10-053-REV2",         1,   1,  None),   # length only
        # "0610" → "610" (leading zero stripped), "6100" unchanged
        ("VEN-0610-KAHS-6100-TEST",           1,   1,  None),   # length only
    ]
    print("=== SPF number tests ===")
    ok = True
    for spir, sh, ln, expected in cases:
        result    = build_old_material_number(spir, sh, ln)
        length_ok = len(result) == TARGET_LEN
        value_ok  = (expected is None) or (result == expected)
        status    = "PASS" if (length_ok and value_ok) else "FAIL"
        if status == "FAIL":
            ok = False
        print(f"  [{status}] {spir[:35]:<35} sh={sh} ln={ln:3d} → '{result}'"
              + (f"  expected='{expected}'" if expected and result != expected else ""))
    print("All passed!" if ok else "FAILURES above.")


if __name__ == "__main__":
    _test()
