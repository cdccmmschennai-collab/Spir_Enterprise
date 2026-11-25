"""
SPIR Comparison tab: upload a SOURCE file and a COMPARISON file, get the
comparison workbook (LATEST / ADDED & MODIFIED / SUMMARY).

Both files run through the same extraction engine as the Extraction and
Batch Extraction tabs (pipeline.extract_output_rows), on the same bounded
extraction thread pool. The workbook is stored like an extraction result, so
the existing GET /api/download/{file_id} serves it.
"""
from __future__ import annotations

import asyncio
import uuid
import zipfile
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from openpyxl.utils.exceptions import InvalidFileException

from spir_dynamic.app.auth import TokenData, get_current_user
from spir_dynamic.app.batch_router import extension_or_422
from spir_dynamic.app.config import get_settings
from spir_dynamic.app.routes import _get_executor, _get_semaphore, _stream_to_temp
from spir_dynamic.comparison import ComparisonInput, compare_files
from spir_dynamic.extraction.file_validator import ValidationError
from spir_dynamic.services.storage import get_storage

log = structlog.stdlib.get_logger(__name__)

comparison_router = APIRouter()


@comparison_router.post("/compare")
async def compare(
    source_file: UploadFile = File(...),
    comparison_file: UploadFile = File(...),
    td: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """Compare two SPIR files. The older file (ISSUE DATE) is always used as the source."""
    cfg = get_settings()
    src_name = source_file.filename or "source.xlsx"
    cmp_name = comparison_file.filename or "comparison.xlsx"
    extension_or_422(src_name)
    extension_or_422(cmp_name)
    structlog.contextvars.bind_contextvars(user_id=td.user_id or "", source=src_name, comparison=cmp_name)

    paths: list[Path] = []
    try:
        src_path, _ = await _stream_to_temp(source_file, cfg.max_file_size_mb, cfg.upload_chunk_size)
        paths.append(src_path)
        cmp_path, _ = await _stream_to_temp(comparison_file, cfg.max_file_size_mb, cfg.upload_chunk_size)
        paths.append(cmp_path)

        loop = asyncio.get_event_loop()
        async with _get_semaphore():
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        _get_executor(), compare_files,
                        ComparisonInput(src_path, src_name), ComparisonInput(cmp_path, cmp_name),
                    ),
                    # two extractions + the comparison itself
                    timeout=cfg.extraction_timeout_seconds * 2,
                )
            except asyncio.TimeoutError:
                raise HTTPException(status_code=504, detail="Comparison timed out. The files may be too large.")
            except (ValidationError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            except zipfile.BadZipFile as exc:
                raise HTTPException(status_code=422, detail=f"File appears corrupted or unreadable: {exc}")
            except InvalidFileException as exc:
                raise HTTPException(status_code=422, detail=f"This workbook format cannot be read: {exc}")
            except MemoryError:
                raise HTTPException(status_code=507, detail="Server ran out of memory comparing these files.")
            except Exception as exc:
                log.exception("comparison.failed", exc_type=type(exc).__name__, exc_message=str(exc))
                raise HTTPException(status_code=500, detail=f"Comparison failed: {exc}")

        file_id = str(uuid.uuid4())
        get_storage().put(file_id, result["xlsx"], result["filename"])
        log.info("comparison.stored", file_id=file_id, filename=result["filename"], mode=result["summary"]["mode"])
        return {"status": "done", "file_id": file_id, "filename": result["filename"], **result["summary"]}
    finally:
        for p in paths:
            p.unlink(missing_ok=True)
