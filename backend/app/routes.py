"""
app/routes.py
──────────────
FastAPI route handlers. Pure inline processing — no Celery dependency needed.

Endpoints:
  POST /extract          — upload + extract (always inline, no Redis needed)
  GET  /status/{job_id}  — poll job status
  GET  /download/{file_id} — stream result XLSX
  GET  /health           — fast health check (no Redis/Celery blocking)
  GET  /currencies       — exchange rates
  GET  /formats          — registered format list
"""
from __future__ import annotations
import io
import logging
import traceback
import uuid
from typing import Optional

import openpyxl
from fastapi import APIRouter, File, UploadFile, HTTPException, Depends
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.security import OAuth2PasswordBearer

from app.config import get_settings, Settings
from app.core.auth import get_current_user, User
from app.pipeline import (
    run_pipeline as run_extraction_pipeline,
    get_progress,
    retrieve_result,
    _set_progress as set_job_progress,
)
from extraction.spir_detector import validate_file, ValidationError
from services.currency_service import conversion_summary
from services.storage import get_storage
from models.spir_schema import ExtractResponse, JobStatusResponse, HealthResponse

log    = logging.getLogger(__name__)
router = APIRouter()


# ── Auth dependency ────────────────────────────────────────────────────────────

def _cfg() -> Settings:
    return get_settings()


class _AuthGate:
    async def __call__(
        self,
        cfg:   Settings      = Depends(_cfg),
        token: Optional[str] = Depends(
            OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)
        ),
    ) -> Optional[User]:
        if not cfg.auth_enabled:
            return None
        if token is None:
            raise HTTPException(
                status_code=401,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await get_current_user(token)


_auth = _AuthGate()


def _jsonify(v):
    if v is None:
        return None
    if isinstance(v, (int, float, bool, str)):
        return v
    return str(v)


# ── POST /extract ──────────────────────────────────────────────────────────────

@router.post(
    "/extract",
    response_model=ExtractResponse,
    response_model_exclude_none=True,
    summary="Upload and extract a SPIR file",
)
async def extract(
    file:         UploadFile      = File(...),
    cfg:          Settings        = Depends(_cfg),
    current_user: Optional[User]  = Depends(_auth),
):
    raw   = await file.read()
    fname = file.filename or "upload.xlsx"
    user  = current_user.username if current_user else "anonymous"
    log.info("Extract: file='%s' size=%.2fMB user='%s'",
             fname, len(raw) / 1_048_576, user)

    try:
        validate_file(fname, raw, max_mb=cfg.max_file_size_mb)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    try:
        d = run_extraction_pipeline(raw, fname)
        return ExtractResponse(
            job_id       = str(uuid.uuid4()),
            status       = "done",
            background   = False,
            **{k: d[k] for k in d if k != "preview_rows"},
            preview_rows = [[_jsonify(v) for v in row]
                            for row in d.get("preview_rows", [])],
        )
    except Exception as exc:
        log.error("Extraction failed: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail={"error": str(exc), "trace": traceback.format_exc()},
        )


# ── GET /status/{job_id} ───────────────────────────────────────────────────────

@router.get(
    "/status/{job_id}",
    response_model=JobStatusResponse,
    response_model_exclude_none=True,
    summary="Poll background job status",
)
async def job_status(
    job_id:       str,
    current_user: Optional[User] = Depends(_auth),
):
    prog = get_progress(job_id)
    if prog is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    return JobStatusResponse(
        job_id   = job_id,
        status   = prog.get("status", "unknown"),
        progress = prog.get("progress", 0),
        message  = prog.get("message", ""),
    )


# ── GET /download/{file_id} ───────────────────────────────────────────────────

@router.get("/download/{file_id}", summary="Download extracted XLSX")
async def download(
    file_id:      str,
    #current_user: Optional[User] = Depends(_auth),
):
    result = retrieve_result(file_id)
    if result is None:
        raise HTTPException(status_code=404, detail="File not found or expired.")

    xlsx_bytes, filename = result
    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── GET /health ────────────────────────────────────────────────────────────────

@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check — no auth required",
)
async def health():
    """Fast health check. Does NOT ping Redis or Celery — returns instantly."""
    cfg = get_settings()
    return HealthResponse(
        status  = "healthy",
        version = cfg.app_version,
        redis   = "not checked",
        workers = "not checked",
    )


# ── GET /currencies ────────────────────────────────────────────────────────────

@router.get("/currencies", summary="List exchange rates")
async def currencies(current_user: Optional[User] = Depends(_auth)):
    return conversion_summary()


# ── GET /formats ───────────────────────────────────────────────────────────────

@router.get("/formats", summary="List registered SPIR format parsers")
async def formats(current_user: Optional[User] = Depends(_auth)):
    from extraction.formats import get_all_parsers
    return {
        "formats": [
            {"name": p.FORMAT_NAME, "priority": i}
            for i, p in enumerate(get_all_parsers())
        ]
    }
