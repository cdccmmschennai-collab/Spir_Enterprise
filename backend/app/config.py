"""
app/config.py  (v2)
────────────────────
All configuration in one place. Values come from environment variables
or a .env file. Change a setting once here — everywhere picks it up.

NEW IN V2:
  storage_backend   — "memory" | "redis" | "s3"
  s3_bucket         — S3 bucket for result storage
  s3_region         — AWS region
  s3_endpoint_url   — MinIO / Cloudflare R2 endpoint (optional)
  async_threshold_mb — files above this size go to Celery background queue
  sheet_workers     — thread pool size for parallel sheet extraction
"""
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Application ────────────────────────────────────────────────────────────
    app_name:    str = "SPIR Enterprise Extraction"
    app_version: str = "2.0.0"
    debug:       bool = False
    log_level:   str  = "INFO"

    # ── Auth (JWT) ─────────────────────────────────────────────────────────────
    secret_key:                  str  = "change-me-64-char-random-string"
    access_token_expire_minutes: int  = 30
    refresh_token_expire_days:   int  = 7
    auth_enabled:                bool = True

    # ── Server ─────────────────────────────────────────────────────────────────
    host:    str = "0.0.0.0"
    port:    int = 8000
    workers: int = 4          # uvicorn worker processes

    # ── File handling ──────────────────────────────────────────────────────────
    max_file_size_mb:  int = 2048      # supports 2GB files
    upload_dir:        str = "/tmp/spir_uploads"
    result_ttl_seconds: int = 3600

    # ── Processing mode thresholds ─────────────────────────────────────────────
    # Files below async_threshold_mb are processed synchronously (inline).
    # Files at or above this threshold are queued to Celery background worker.
    async_threshold_mb: float = 50.0   # 50 MB boundary

    # ── Parallel sheet extraction ──────────────────────────────────────────────
    sheet_workers: int = 4    # ThreadPoolExecutor max_workers for sheet processing

    # ── Redis / Celery ─────────────────────────────────────────────────────────
    redis_url:      str = "redis://localhost:6379/0"
    celery_broker:  str = "redis://localhost:6379/0"
    celery_backend: str = "redis://localhost:6379/1"

    # ── Storage backend ─────────────────────────────────────────────────────────
    # "memory"  — single process, no deps
    # "redis"   — multi-process same host
    # "s3"      — multi-host, unlimited size
    storage_backend:  str  = "memory"
    s3_bucket:        str  = ""
    s3_region:        str  = "us-east-1"
    s3_endpoint_url:  str  = ""        # leave empty for AWS, set for MinIO/R2

    # ── CORS ───────────────────────────────────────────────────────────────────
    allowed_origins: list[str] = ["*"]   # tighten in production

    # ── Preview ────────────────────────────────────────────────────────────────
    preview_row_count: int = 12

    class Config:
        env_file          = ".env"
        env_file_encoding = "utf-8"

@lru_cache
def get_settings() -> Settings:
    return Settings()
