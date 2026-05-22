"""
FastAPI application factory.
"""
from __future__ import annotations
import os
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from prometheus_client import make_asgi_app
from spir_dynamic.app.auth import auth_router
from spir_dynamic.app.batch_router import batch_router
from spir_dynamic.app.config import get_settings
from spir_dynamic.app.routes import router
from spir_dynamic.utils.logging import setup_logging

log = structlog.stdlib.get_logger(__name__)


class RequestIDMiddleware:
    """Attach a unique request ID to every request.

    Pure ASGI middleware — avoids BaseHTTPMiddleware's response-buffering behaviour
    which breaks mounted ASGI sub-apps (e.g. prometheus_client.make_asgi_app()).

    - Reads X-Request-ID from the incoming request if present.
    - Generates a UUID4 otherwise.
    - Binds the ID to structlog contextvars so every log line carries request_id.
    - Echoes the ID back in the X-Request-ID response header.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        raw_id = headers.get(b"x-request-id", b"")
        request_id = raw_id.decode() if raw_id else str(uuid.uuid4())

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        async def send_with_request_id(message):
            if message["type"] == "http.response.start":
                new_headers = list(message.get("headers", []))
                new_headers.append((b"x-request-id", request_id.encode()))
                message = dict(message, headers=new_headers)
            await send(message)

        await self.app(scope, receive, send_with_request_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the database on startup (if DATABASE_URL is configured)."""
    cfg = get_settings()
    if cfg.database_url:
        from spir_dynamic.db.init_db import initialize
        ok = await initialize(cfg.database_url, cfg.app_user, cfg.app_pass)
        if ok:
            log.info("Database initialized — full audit logging enabled")
        else:
            log.warning("Database init failed — running in no-DB (legacy) mode")
    else:
        log.info("DATABASE_URL not set — running in no-DB (legacy) mode")

    # Ensure row storage directory exists before accepting requests.
    # cfg.rows_storage_path is always absolute (resolved in config.py).
    from pathlib import Path as _Path
    rows_dir = _Path(cfg.rows_storage_path)
    try:
        rows_dir.mkdir(parents=True, exist_ok=True)
        log.info("storage.ready", path=str(rows_dir))
    except OSError as exc:
        log.error("storage.unavailable", path=str(rows_dir), exc_message=str(exc))

    # Ensure batch upload staging directory exists.
    from spir_dynamic.services.cleanup import cleanup_stale_uploads as _sweep
    upload_dir = _Path(cfg.batch_upload_dir)
    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
        log.info("upload_dir.ready", path=str(upload_dir))
        stale = _sweep(upload_dir, max_age_seconds=86400)
        if stale:
            log.info("startup.cleanup", removed=stale)
    except OSError as exc:
        log.error("upload_dir.unavailable", path=str(upload_dir), exc_message=str(exc))

    # Sweep stale single-file temp uploads left by crashed extractions.
    import tempfile as _tf
    import time as _time
    _tmp_dir = _Path(_tf.gettempdir())
    _stale_cutoff = _time.time() - 3600
    for _p in _tmp_dir.glob("spir_upload_*"):
        try:
            if _p.stat().st_mtime < _stale_cutoff:
                _p.unlink(missing_ok=True)
                log.info("tempfile.cleaned", path=str(_p))
        except Exception:
            pass

    yield
    # Shutdown: close DB engine if open
    from spir_dynamic.db.database import is_db_enabled, get_engine
    if is_db_enabled():
        await get_engine().dispose()


def create_app() -> FastAPI:
    cfg = get_settings()
    setup_logging(cfg.log_level, cfg.log_format)

    app = FastAPI(
        title=cfg.app_name,
        version=cfg.app_version,
        docs_url=None if not cfg.debug else "/api/docs",
        redoc_url=None if not cfg.debug else "/api/redoc",
        lifespan=lifespan,
    )


    # Middleware stack (outermost first in request order):
    # RequestIDMiddleware → CORSMiddleware → routes
    # Both are registered here; Starlette applies them in reverse-add order,
    # making the last-added the outermost.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )
    app.add_middleware(RequestIDMiddleware)

    app.include_router(auth_router, prefix="/auth")
    app.include_router(router, prefix="/api")          # register first so /api/me wins
    app.include_router(auth_router, prefix="/api")     # /api/login + /api/logout aliases
    app.include_router(batch_router, prefix="/api/batch")

    # Admin + user history endpoints (require DB — gracefully disabled when unavailable)
    from spir_dynamic.app.admin_router import admin_router as _admin
    app.include_router(_admin, prefix="/api/admin", tags=["admin"])

    return app


app = create_app()

metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

if __name__ == "__main__":
    import uvicorn

    cfg = get_settings()
    uvicorn.run(
        "spir_dynamic.app.main:app",
        host=cfg.host,
        port=cfg.port,
        reload=cfg.debug,
    )


port = int(os.getenv("PORT", 8000))
