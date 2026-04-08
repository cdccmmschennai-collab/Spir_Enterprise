"""
app/main.py
────────────
FastAPI application entry point.

Run:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

Swagger UI:  http://localhost:8000/api/docs
Health:      http://localhost:8000/health
"""
from __future__ import annotations
import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.routes import router
from app.api.auth_routes import router as auth_router

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")


def create_app() -> FastAPI:
    cfg = get_settings()

    app = FastAPI(
        title       = cfg.app_name,
        version     = cfg.app_version,
        description = "SPIR Enterprise Extraction API — FORMAT1-FORMAT8 + adaptive.",
        docs_url    = "/api/docs",
        redoc_url   = "/api/redoc",
        openapi_url = "/api/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins     = ["*"],   # tighten in production
        allow_credentials = True,
        allow_methods     = ["*"],
        allow_headers     = ["*"],
    )

    # Auth routes: /auth/login  /auth/me  etc.
    app.include_router(auth_router)

    # Main API routes: /extract  /download  /health  /formats  /currencies
    app.include_router(router, tags=["SPIR Extraction"])

    # Root → JSON (React dev server runs on :3000 separately)
    @app.get("/", include_in_schema=False)
    async def root():
        return JSONResponse({
            "message": "SPIR Enterprise API",
            "version": cfg.app_version,
            "docs":    "/api/docs",
            "status":  "running",
        })

    @app.on_event("startup")
    async def startup():
        os.makedirs(cfg.upload_dir, exist_ok=True)
        log.info("SPIR API started — v%s — docs at /api/docs", cfg.app_version)

    return app


app = create_app()
