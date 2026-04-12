"""
ZIP archive builder for batch download.
"""
from __future__ import annotations

import io
import zipfile


def build_zip(file_results: list[tuple[bytes, str]]) -> bytes:
    """
    Assemble a ZIP archive from (xlsx_bytes, filename) pairs.
    Returns the ZIP as bytes.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for data, name in file_results:
            zf.writestr(name, data)
    return buf.getvalue()
