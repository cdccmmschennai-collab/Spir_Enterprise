"""
Centralized configuration via environment variables / .env file.
"""
from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings

# config.py lives at src/spir_dynamic/app/config.py — project root is 4 levels up.
# Used to anchor relative storage paths regardless of launch working directory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


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

    # Database (optional — set to enable full audit logging)
    # Format: postgresql+asyncpg://user:pass@host:5432/dbname
    # Railway / Heroku provide DATABASE_URL in the form postgres://... (auto-converted)
    database_url: str = ""

    # Auth
    app_user: str = "admin"
    app_pass: str = "cdc@2026"
    secret_key: str = "insecure-dev-secret-replace-in-production"
    token_expire_hours: int = 8

    # Keywords config
    keywords_config_path: str = "config/keywords.yaml"
    omn_target_length: int = 18
    min_column_map_score: int = 30
    discovery_min_score: int = 15

    # Extraction safety
    extraction_timeout_seconds: int = 600   # env: EXTRACTION_TIMEOUT_SECONDS
    upload_chunk_size: int = 1_048_576      # 1 MB chunks; env: UPLOAD_CHUNK_SIZE
    max_concurrent_extractions: int = 4     # env: MAX_CONCURRENT_EXTRACTIONS

    # Batch processing
    batch_max_files: int = 20
    batch_ttl_seconds: int = 7200

    # Persistent row storage — extracted rows saved here as JSON for combine feature.
    # Relative paths are anchored to _PROJECT_ROOT (project root) at import time,
    # so the path is stable regardless of where uvicorn/celery is launched from.
    # Override with an absolute ROWS_STORAGE_PATH env var for Docker/VPS if needed.
    rows_storage_path: str = "storage/extracted_rows"

    @field_validator("rows_storage_path", mode="after")
    @classmethod
    def _resolve_storage_path(cls, v: str) -> str:
        p = Path(v)
        if not p.is_absolute():
            p = _PROJECT_ROOT / v
        return str(p.resolve())

    # Celery / Redis
    redis_url: str = "redis://localhost:6379/0"
    # Set CELERY_ENABLED=true to route batch processing through Celery workers.
    # When false the existing asyncio/thread-pool fallback is used instead.
    celery_enabled: bool = False

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def load_keywords() -> dict:
    """Load keywords.yaml once. Edit the YAML and restart to pick up changes."""
    import yaml
    path = get_settings().keywords_config_path
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
