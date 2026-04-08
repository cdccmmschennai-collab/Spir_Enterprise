"""
extraction/position_number.py
───────────────────────────────
POSITION NUMBER assignment — continues across sheets for same tag.

SPEC (implemented exactly):
────────────────────────────
Format:    4-digit zero-padded, increments of 10.
           "0010", "0020", ..., "0240", "0250", ...

SAME TAG across multiple sheets → CONTINUE counting (do NOT reset):
    Sheet 1  TAG1 → 0010, 0020, ..., 0240
    Sheet 2  TAG1 → 0250, 0260, ...        ← continues from 0240

NEW TAG (first time seen in any sheet) → reset to 0010:
    Sheet 1  TAG2 → 0010, 0020, ..., 0080
    Sheet 2  TAG2 → 0090, 0100, ...        ← continues from 0080
    Sheet 2  TAG3 → 0010, ...              ← new tag, reset

Equipment HEADER rows (ITEM NUMBER is None, TAG NO is set):
    Always show "0010". Do NOT advance the spare counter.
    A header row is a section marker, not a counted spare item.

Constants:
    POSITION_START = 10
    POSITION_STEP  = 10
    POSITION_WIDTH = 4   → "0010"
"""
from __future__ import annotations
from typing import Optional

POSITION_START = 10
POSITION_STEP  = 10
POSITION_WIDTH = 4


class PositionNumberEngine:
    """
    Stateful engine that assigns position numbers across ALL sheets.

    One engine instance must be created per extraction job and used
    for ALL rows in order — it maintains the cross-sheet state.

    Usage:
        engine = PositionNumberEngine()
        for row in all_rows:            # rows from ALL sheets, in order
            pos = engine.next(
                tag_no   = row[CI['TAG NO']],
                is_spare = row[CI['ITEM NUMBER']] is not None,
            )
            row[CI['POSITION NUMBER']] = pos
    """

    def __init__(self) -> None:
        # tag_no → last assigned position value (for spare rows)
        # We store the LAST assigned value so we can peek without advancing.
        self._last: dict[str, int] = {}

    def next(self, tag_no: Optional[str], is_spare: bool) -> str:
        """
        Return the position string for this row.

        Args:
            tag_no:   Equipment tag string (None → treated as global).
            is_spare: True for spare-item rows (ITEM NUMBER is set).
                      False for equipment-header rows (ITEM NUMBER is None).

        Returns:
            Zero-padded string e.g. "0010", "0250".
        """
        key = tag_no or '__GLOBAL__'

        if not is_spare:
            # Header row: always 0010, counter unchanged
            return str(POSITION_START).zfill(POSITION_WIDTH)

        # Spare row: advance this tag's counter
        if key not in self._last:
            # First spare row for this tag → start value
            self._last[key] = POSITION_START
        else:
            self._last[key] += POSITION_STEP

        return str(self._last[key]).zfill(POSITION_WIDTH)

    def current(self, tag_no: Optional[str]) -> str:
        """Return the last assigned position for a tag (without advancing)."""
        key = tag_no or '__GLOBAL__'
        return str(self._last.get(key, 0)).zfill(POSITION_WIDTH)


def assign_position_numbers(
    rows:      list[list],
    tag_col:   int,
    item_col:  int,
    pos_col:   int,
) -> list[list]:
    """
    Assign POSITION NUMBER to every row in-place.
    Rows must be in the correct final order (all sheets merged).

    Args:
        rows:     Extracted row lists (OUTPUT_COLS-length each).
        tag_col:  Index of TAG NO column.
        item_col: Index of ITEM NUMBER column.
        pos_col:  Index of POSITION NUMBER column.
    """
    engine = PositionNumberEngine()
    for row in rows:
        tag_no   = row[tag_col]
        item_num = row[item_col]
        # is_spare = has an item number
        is_spare = item_num is not None
        # Skip rows that are neither header nor spare
        if tag_no is None and item_num is None:
            continue
        row[pos_col] = engine.next(tag_no=tag_no, is_spare=is_spare)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Self-test
# ─────────────────────────────────────────────────────────────────────────────

def _test() -> None:
    engine = PositionNumberEngine()
    print("=== Position number tests ===")

    # TAG1, 24 spares on sheet 1 → 0010 to 0240
    results = [engine.next("TAG1", True) for _ in range(24)]
    assert results[0]  == "0010", f"First: {results[0]}"
    assert results[-1] == "0240", f"Last:  {results[-1]}"
    print(f"  TAG1 sheet1: {results[0]} → {results[-1]} ✓")

    # TAG2, 8 spares on sheet 1 → 0010 to 0080
    results2 = [engine.next("TAG2", True) for _ in range(8)]
    assert results2[0]  == "0010", f"TAG2 first: {results2[0]}"
    assert results2[-1] == "0080", f"TAG2 last:  {results2[-1]}"
    print(f"  TAG2 sheet1: {results2[0]} → {results2[-1]} ✓")

    # TAG1 on sheet 2 → continues from 0240 → 0250, 0260
    r1 = engine.next("TAG1", True)
    r2 = engine.next("TAG1", True)
    assert r1 == "0250", f"TAG1 sheet2 first: {r1}"
    assert r2 == "0260", f"TAG1 sheet2 second: {r2}"
    print(f"  TAG1 sheet2: {r1}, {r2} ✓  (continues from 0240)")

    # TAG3, new tag on sheet 2 → starts at 0010
    r3 = engine.next("TAG3", True)
    assert r3 == "0010", f"TAG3 new: {r3}"
    print(f"  TAG3 sheet2: {r3} ✓  (new tag resets)")

    # Header rows always return 0010, don't advance counter
    hdr = engine.next("TAG1", False)
    assert hdr == "0010", f"Header: {hdr}"
    # TAG1 counter should still be at 0260
    assert engine.current("TAG1") == "0260", f"After header: {engine.current('TAG1')}"
    print(f"  Header row: {hdr} ✓  (counter unchanged at {engine.current('TAG1')})")

    print("All passed!")


if __name__ == "__main__":
    _test()
