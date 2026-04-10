"""
Batch extraction API — accept multiple files, process concurrently, return ZIP.
"""
from __future__ import annotations

import asyncio
import io
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from spir_dynamic.app.auth import verify_token
from spir_dynamic.app.config import get_settings
from spir_dynamic.app.pipeline import run_pipeline
from spir_dynamic.services.job_store import FileResult, get_job_store
from spir_dynamic.services.storage import get_storage
from spir_dynamic.services.zip_builder import build_zip

log = logging.getLogger(__name__)

batch_router = APIRouter()


@batch_router.post("/extract")
async def batch_extract(
    files: list[UploadFile] = File(...),
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    """
    Accept 1–N files. Launch extraction for each file concurrently.
    Returns job_id immediately — poll GET /api/batch/{job_id} for status.
    """
    cfg = get_settings()
    if len(files) > cfg.batch_max_files:
        raise HTTPException(
            status_code=400,
            detail=f"Max {cfg.batch_max_files} files per batch request",
        )

    job_id = str(uuid.uuid4())
    filenames = [f.filename or f"file_{i}.xlsx" for i, f in enumerate(files)]
    get_job_store().create(job_id, filenames)

    # Read all file contents before launching tasks (UploadFile is not thread-safe)
    file_data: list[tuple[bytes, str]] = [
        (await f.read(), name) for f, name in zip(files, filenames)
    ]

    asyncio.create_task(_process_batch(job_id, file_data))

    return {"job_id": job_id, "total": len(files), "status": "processing"}


@batch_router.get("/{job_id}")
async def batch_status(
    job_id: str,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    """Poll extraction status for a batch job."""
    job = get_job_store().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    return job.to_dict()


@batch_router.get("/{job_id}/download")
async def batch_download(
    job_id: str,
    _: str = Depends(verify_token),
) -> StreamingResponse:
    """Download a ZIP archive of all successfully extracted files."""
    job = get_job_store().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")

    storage = get_storage()
    file_pairs: list[tuple[bytes, str]] = []
    for r in job.results:
        if r.status == "ok" and r.file_id:
            entry = storage.get(r.file_id)
            if entry:
                data, name = entry
                file_pairs.append((data if isinstance(data, bytes) else data.read(), name))

    if not file_pairs:
        raise HTTPException(status_code=404, detail="No completed files available for download")

    zip_bytes = build_zip(file_pairs)
    short_id = job_id[:8]
    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="batch_{short_id}.zip"'},
    )


async def _process_batch(job_id: str, file_data: list[tuple[bytes, str]]) -> None:
    """Background coroutine: run each file through the pipeline in a thread pool."""
    loop = asyncio.get_event_loop()
    store = get_job_store()

    async def _extract_one(idx: int, content: bytes, filename: str) -> None:
        try:
            result = await loop.run_in_executor(None, run_pipeline, content, filename)
            store.update_result(job_id, idx, FileResult(
                filename=filename,
                status="ok",
                total_rows=result.get("total_rows", 0),
                total_tags=result.get("total_tags", 0),
                spir_no=result.get("spir_no", ""),
                file_id=result.get("file_id", ""),
            ))
        except Exception as exc:
            log.error("Batch extraction failed [%s]: %s", filename, exc, exc_info=True)
            store.update_result(job_id, idx, FileResult(
                filename=filename,
                status="error",
                error=str(exc),
            ))

    await asyncio.gather(*[
        _extract_one(i, content, name)
        for i, (content, name) in enumerate(file_data)
    ])
