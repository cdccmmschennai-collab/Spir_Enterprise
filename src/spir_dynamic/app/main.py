"""
FastAPI application factory.
"""
from __future__ import annotations
import os

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from spir_dynamic.app.auth import auth_router
from spir_dynamic.app.batch_router import batch_router
from spir_dynamic.app.config import get_settings
from spir_dynamic.app.routes import router
from spir_dynamic.utils.logging import setup_logging

log = logging.getLogger(__name__)


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
    yield
    # Shutdown: close DB engine if open
    from spir_dynamic.db.database import is_db_enabled, get_engine
    if is_db_enabled():
        await get_engine().dispose()


def create_app() -> FastAPI:
    cfg = get_settings()
    setup_logging(cfg.log_level)

    app = FastAPI(
        title=cfg.app_name,
        version=cfg.app_version,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_router, prefix="/auth")
    app.include_router(auth_router, prefix="/api")   # /api/login + /api/logout aliases
    app.include_router(router, prefix="/api")
    app.include_router(batch_router, prefix="/api/batch")

    # Admin + user history endpoints (require DB — gracefully disabled when unavailable)
    from spir_dynamic.app.admin_router import admin_router as _admin
    app.include_router(_admin, prefix="/api/admin", tags=["admin"])

    return app


app = create_app()


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