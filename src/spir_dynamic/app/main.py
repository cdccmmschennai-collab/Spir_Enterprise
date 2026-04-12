"""
FastAPI application factory.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from spir_dynamic.app.auth import auth_router
from spir_dynamic.app.batch_router import batch_router
from spir_dynamic.app.config import get_settings
from spir_dynamic.app.routes import router
from spir_dynamic.utils.logging import setup_logging


def create_app() -> FastAPI:
    cfg = get_settings()
    setup_logging(cfg.log_level)

    app = FastAPI(
        title=cfg.app_name,
        version=cfg.app_version,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_router, prefix="/auth")
    app.include_router(router, prefix="/api")
    app.include_router(batch_router, prefix="/api/batch")

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
