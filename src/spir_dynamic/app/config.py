"""
Centralized configuration via environment variables / .env file.
Simplified: no Redis, no Celery, no S3, no auth.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Application
    app_name: str = "SPIR Dynamic Extraction"
    app_version: str = "0.1.0"
    debug: bool = False
    log_level: str = "INFO"

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # File handling
    max_file_size_mb: int = 2048
    preview_row_count: int = 12

    # CORS
    allowed_origins: list[str] = ["*"]

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
