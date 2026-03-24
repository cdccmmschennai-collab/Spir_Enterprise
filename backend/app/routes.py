"""
app/routes.py
──────────────
FastAPI route handlers. Pure inline processing — no Celery dependency needed.
 
Endpoints:
  POST /extract            — upload + extract (always inline, no Redis needed)
  GET  /status/{job_id}   — poll job status
  GET  /download/{file_id} — stream result XLSX
  GET  /health             — fast health check (no Redis/Celery blocking)
  GET  /currencies         — exchange rates
  GET  /formats            — registered format list
"""
from __future__ import annotations
import io
import logging
import traceback
import uuid
from typing import Optional
 
from fastapi import APIRouter, File, UploadFile, HTTPException, Depends
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
 
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
from models.spir_schema import ExtractResponse, JobStatusResponse, HealthResponse
 
log    = logging.getLogger(__name__)
router = APIRouter()
 
# ── Auth dependency ────────────────────────────────────────────────────────────
 
def _cfg() -> Settings:
    return get_settings()
 
_bearer = HTTPBearer(auto_error=False)
 
class _AuthGate:
    async def __call__(
        self,
        cfg:         Settings = Depends(_cfg),
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    ) -> Optional[User]:
        if not cfg.auth_enabled:
            return None
        if credentials is None or not credentials.credentials:
            raise HTTPException(
                status_code=401,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await get_current_user(credentials.credentials)
 
_auth = _AuthGate()
 
 
def _jsonify(v):
    """Convert a cell value to something JSON can serialize."""
    if v is None:
        return None
    if isinstance(v, (int, float, bool, str)):
        return v
    return str(v)
 
 
# ── POST /extract ──────────────────────────────────────────────────────────────
 
@router.post("/extract", summary="Upload and extract a SPIR file")
async def extract(
    file:         UploadFile      = File(...),
    cfg:          Settings        = Depends(_cfg),
    current_user: Optional[User]  = Depends(_auth),
):
    raw   = await file.read()
    fname = file.filename or "upload.xlsx"
 
    try:
        validate_file(fname, raw, max_mb=cfg.max_file_size_mb)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
 
    try:
        d = run_extraction_pipeline(raw, fname)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        log.error("Extraction failed: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(exc))
 
    preview_rows_safe = [
        [_jsonify(cell) for cell in row]
        for row in (d.get("preview_rows") or [])
    ]
 
    return {
        "file_id":        d.get("file_id"),
        "filename":       d.get("filename"),
        "status":         "done",
        "total_rows":     d.get("total_rows", 0),
        "spare_items":    d.get("spare_items", 0),
        "total_tags":     d.get("total_tags", 0),
        "dup1_count":     d.get("dup1_count", 0),
        "sap_count":      d.get("sac_count", 0),
        "annexure_count": d.get("annexure_count", 0),
        "format":         d.get("format"),
        "spir_no":        d.get("spir_no"),
        "equipment":      d.get("equipment"),
        "manufacturer":   d.get("manufacturer"),
        "supplier":       d.get("supplier"),
        "spir_type":      d.get("spir_type"),
        "eqpt_qty":       d.get("eqpt_qty"),
        "annexure_stats": d.get("annexure_stats"),
        "preview_cols":   d.get("preview_cols", []),
        "preview_rows":   preview_rows_safe,
    }
 
 
# ── GET /status/{job_id} ──────────────────────────────────────────────────────
 
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
    current_user: Optional[User] = Depends(_auth),
):
    try:
        log.info("Download request: file_id=%s", file_id)
 
        result = retrieve_result(file_id)
        log.info("retrieve_result returned: %s (type=%s)",
                 "None" if result is None else f"tuple len={len(result)}",
                 type(result).__name__)
 
        if result is None:
            raise HTTPException(
                status_code=404,
                detail=f"File '{file_id}' not found. Please re-upload and extract again.",
            )
 
        xlsx_bytes, filename = result
 
        # Ensure bytes type — guard against accidental BytesIO storage
        if hasattr(xlsx_bytes, "read"):
            log.warning("xlsx_bytes was BytesIO, converting to bytes")
            xlsx_bytes = xlsx_bytes.read()
        if not isinstance(xlsx_bytes, (bytes, bytearray)):
            raise HTTPException(
                status_code=500,
                detail=f"Stored data has wrong type: {type(xlsx_bytes).__name__}",
            )
 
        log.info("Sending file: filename=%s size=%d bytes", filename, len(xlsx_bytes))
 
        return StreamingResponse(
            io.BytesIO(xlsx_bytes),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length":      str(len(xlsx_bytes)),
            },
        )
 
    except HTTPException:
        raise
    except Exception as exc:
        log.error("Download failed for file_id=%s: %s\n%s",
                  file_id, exc, traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Download failed: {str(exc)}",
        )
 
 
# ── GET /health ───────────────────────────────────────────────────────────────
 
@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check — no auth required",
)
async def health():
    cfg = get_settings()
    return HealthResponse(
        status  = "healthy",
        version = cfg.app_version,
        redis   = "not checked",
        workers = "not checked",
    )
 
 
# ── GET /currencies ───────────────────────────────────────────────────────────
 
@router.get("/currencies", summary="List exchange rates")
async def currencies(current_user: Optional[User] = Depends(_auth)):
    return conversion_summary()
 
 
# ── GET /formats ──────────────────────────────────────────────────────────────
 
@router.get("/formats", summary="List registered SPIR format parsers")
async def formats(current_user: Optional[User] = Depends(_auth)):
    from extraction.formats import get_all_parsers
    return {
        "formats": [
            {"name": p.FORMAT_NAME, "priority": i}
            for i, p in enumerate(get_all_parsers())
        ]
    }