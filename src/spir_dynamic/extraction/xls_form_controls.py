"""
Legacy form-control checkboxes in BIFF8 (.xls) workbooks.

xlrd reads cell data only; the Forms checkboxes some vendor SPIR sheets use for
the SPIR type live in the binary "Workbook" stream as drawing objects. This
module walks that stream and returns, per worksheet, each checkbox's caption
and checked state, read from the structures defined in [MS-XLS]:

  BoundSheet8 (0x0085)  globals substream — worksheet name + stream offset
  BOF (0x0809) / EOF (0x000A)  substream boundaries (embedded charts nest)
  Obj (0x005D)          FtCmo.ot == 0x000B  → checkbox
                        FtCblsData (0x0012).fChecked → 0 unchecked, 1 checked,
                        2 mixed (anything else / missing → malformed: None)
  TxO (0x01B6)          the caption; cchText at offset 10, the characters in
                        the Continue (0x003C) records that follow it

The linked cell (FtCblsFmla) is deliberately not read: a linked cell is only
written when a user toggles the box, and can point at unrelated content, so
the checkbox's own state is the single source of truth.

Never raises for malformed content — callers get None (no information) or
controls whose fields are None where they could not be read.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Iterator, Optional

log = logging.getLogger(__name__)

_BOF = 0x0809
_EOF = 0x000A
_FILEPASS = 0x002F
_BOUNDSHEET = 0x0085
_OBJ = 0x005D
_TXO = 0x01B6
_CONTINUE = 0x003C

_FT_END = 0x0000
_FT_CMO = 0x0015
_FT_CBLS_DATA = 0x0012
_OT_CHECKBOX = 0x000B

_SHEET_TYPE_WORKSHEET = 0x00


@dataclass(frozen=True)
class FormCheckbox:
    """One Forms checkbox: caption text (None if unreadable) and fChecked (None if malformed)."""
    caption: Optional[str]
    checked: Optional[int]


def _records(stream: bytes, start: int = 0) -> Iterator[tuple[int, int, bytes]]:
    """Yield (offset, record id, body) for each BIFF record from `start`."""
    pos, end = start, len(stream)
    while pos + 4 <= end:
        rid, size = struct.unpack_from("<HH", stream, pos)
        body = stream[pos + 4: pos + 4 + size]
        if len(body) != size:
            return  # truncated record
        yield pos, rid, body
        pos += 4 + size


def _worksheet_offsets(stream: bytes) -> Optional[list[int]]:
    """Stream offsets of the worksheet substreams, in sheet order (None when unreadable)."""
    offsets: list[int] = []
    depth = 0
    for _, rid, body in _records(stream):
        if rid == _BOF:
            depth += 1
        elif rid == _EOF:
            depth -= 1
            if depth == 0:
                return offsets  # end of the globals substream
        elif rid == _FILEPASS:
            return None  # encrypted: every later record body is ciphertext
        elif rid == _BOUNDSHEET and depth == 1 and len(body) >= 6:
            lb_ply_pos = struct.unpack_from("<I", body, 0)[0]
            sheet_type = body[5]
            if sheet_type == _SHEET_TYPE_WORKSHEET:
                offsets.append(lb_ply_pos)
    return None


def _checkbox_state(obj: bytes) -> tuple[bool, Optional[int]]:
    """(is_checkbox, fChecked) for one Obj record body."""
    if len(obj) < 22:
        return False, None
    ft, cb = struct.unpack_from("<HH", obj, 0)
    if ft != _FT_CMO or cb != 0x12:
        return False, None
    if struct.unpack_from("<H", obj, 4)[0] != _OT_CHECKBOX:
        return False, None

    checked: Optional[int] = None
    pos = 4 + cb
    while pos + 4 <= len(obj):
        ft, cb = struct.unpack_from("<HH", obj, pos)
        if ft == _FT_END:
            break
        if pos + 4 + cb > len(obj):
            return True, None  # subrecord overruns the record: malformed
        if ft == _FT_CBLS_DATA and cb == 8:
            value = struct.unpack_from("<H", obj, pos + 4)[0]
            checked = value if value in (0, 1, 2) else None
        pos += 4 + cb
    return True, checked


def _txo_caption(txo: bytes, continues: list[bytes]) -> Optional[str]:
    """Decode the TxO text spread over the Continue records that follow it."""
    if len(txo) < 12:
        return None
    cch = struct.unpack_from("<H", txo, 10)[0]
    if cch == 0:
        return ""
    chars: list[str] = []
    remaining = cch
    for frag in continues:
        if remaining <= 0 or not frag:
            break
        high = frag[0] & 0x01
        data = frag[1:]
        if high:
            n = min(remaining, len(data) // 2)
            chars.append(data[: n * 2].decode("utf-16-le", errors="replace"))
        else:
            n = min(remaining, len(data))
            chars.append(data[:n].decode("latin-1"))
        remaining -= n
    if remaining > 0:
        return None  # text shorter than cchText says
    return "".join(chars)


def _sheet_checkboxes(stream: bytes, offset: int) -> list[FormCheckbox]:
    """Checkboxes drawn on the worksheet whose BOF is at `offset`."""
    boxes: list[FormCheckbox] = []
    records = _records(stream, offset)
    depth = 0
    pending: Optional[int] = None        # fChecked of the last checkbox Obj awaiting its TxO
    pending_open = False
    txo_body: Optional[bytes] = None     # TxO whose Continue records are being collected
    continues: list[bytes] = []

    def flush_text() -> None:
        nonlocal pending_open, txo_body, continues
        if pending_open:
            caption = _txo_caption(txo_body, continues) if txo_body is not None else None
            boxes.append(FormCheckbox(caption=caption, checked=pending))
        pending_open, txo_body, continues = False, None, []

    for _, rid, body in records:
        if txo_body is not None:
            if rid == _CONTINUE:
                continues.append(body)
                continue
            flush_text()
        if rid == _BOF:
            depth += 1
            continue
        if rid == _EOF:
            depth -= 1
            if depth == 0:
                break
            continue
        if depth != 1:
            continue  # inside an embedded chart substream
        if rid == _OBJ:
            if pending_open:
                flush_text()  # previous checkbox had no TxO: caption unknown
            is_checkbox, checked = _checkbox_state(body)
            if is_checkbox:
                pending, pending_open = checked, True
        elif rid == _TXO and pending_open:
            txo_body, continues = body, []
    if txo_body is not None or pending_open:
        flush_text()
    return boxes


def read_xls_checkboxes(workbook_stream: bytes) -> Optional[list[list[FormCheckbox]]]:
    """
    Checkboxes per worksheet (same order as the workbook's worksheets), or None
    when the stream cannot be interpreted (not BIFF8, encrypted, truncated).
    """
    try:
        first = next(_records(workbook_stream), None)
        if first is None or first[1] != _BOF or len(first[2]) < 4:
            return None
        if struct.unpack_from("<H", first[2], 0)[0] != 0x0600:  # BIFF8 only
            return None
        offsets = _worksheet_offsets(workbook_stream)
        if offsets is None:
            return None
        result: list[list[FormCheckbox]] = []
        for off in offsets:
            if off + 4 > len(workbook_stream) or struct.unpack_from("<H", workbook_stream, off)[0] != _BOF:
                return None
            result.append(_sheet_checkboxes(workbook_stream, off))
        return result
    except Exception as exc:  # defensive: never let control parsing break extraction
        log.warning("xls form-control parse failed: %s", exc)
        return None
