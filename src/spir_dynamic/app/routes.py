"""
FastAPI route handlers.
"""
from __future__ import annotations

import io
import logging
from typing import Any

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse

from spir_dynamic.app.auth import verify_token
from spir_dynamic.app.pipeline import run_pipeline, retrieve_result
from spir_dynamic.app.config import get_settings
from spir_dynamic.extraction.file_validator import ValidationError
from spir_dynamic.services.currency_service import conversion_summary

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/extract")
async def extract(
    file: UploadFile = File(...),
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    """Upload and extract a SPIR Excel file."""
    cfg = get_settings()

    content = await file.read()
    filename = file.filename or "upload.xlsx"

    # Size check
    size_mb = len(content) / (1024 * 1024)
    if size_mb > cfg.max_file_size_mb:
        raise HTTPException(
            status_code=413,
            detail=f"File is {size_mb:.1f} MB — exceeds {cfg.max_file_size_mb} MB limit",
        )

    try:
        result = run_pipeline(content, filename)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        log.error("Extraction failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Extraction failed: {exc}")

    return result


@router.get("/download/{file_id}")
async def download(file_id: str, _: str = Depends(verify_token)):
    """Download the extracted Excel file."""
    result = retrieve_result(file_id)
    if result is None:
        raise HTTPException(status_code=404, detail="File not found or expired")

    data, filename = result

    # Ensure we have bytes
    if isinstance(data, io.BytesIO):
        data = data.read()

    safe_name = filename.replace('"', "'")

    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@router.get("/health")
async def health() -> dict[str, str]:
    cfg = get_settings()
    return {
        "status": "healthy",
        "version": cfg.app_version,
    }


@router.get("/currencies")
async def currencies() -> dict:
    return conversion_summary()
