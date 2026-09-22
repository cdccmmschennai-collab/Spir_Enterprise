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
    log_format: str = "text"   # "json" in production (set LOG_FORMAT=json)

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

    # Batch upload staging directory — batch files are streamed here on upload
    # and deleted by the Celery worker after extraction completes. Orphaned
    # files (from crashed workers) are swept on API/worker startup.
    batch_upload_dir: str = "storage/batch_uploads"

    @field_validator("batch_upload_dir", mode="after")
    @classmethod
    def _resolve_batch_upload_dir(cls, v: str) -> str:
        p = Path(v)
        if not p.is_absolute():
            p = _PROJECT_ROOT / v
        return str(p.resolve())

    # Hard upload ceiling — reject any file above this size before streaming
    # begins.  Protects the VPS from zip bombs and accidentally uploaded
    # multi-gigabyte archives that would exhaust disk and RAM.
    absolute_max_file_size_mb: int = 1500   # env: ABSOLUTE_MAX_FILE_SIZE_MB

    # Files above this size (MB) are routed to the 'heavy' Celery queue so
    # normal-sized files never wait behind a large job.
    large_file_threshold_mb: int = 100

    # Files above this size (MB) are routed to the 'giant' Celery queue — a
    # dedicated single-concurrency worker with extended time limits.
    # The giant worker processes one file at a time and is recycled after every
    # task so openpyxl memory does not accumulate between giant extractions.
    giant_file_threshold_mb: int = 500     # env: GIANT_FILE_THRESHOLD_MB

    # Sanitizer — strips embedded bulk assets from large XLSX files before
    # openpyxl extraction (images, OLE objects, embedded PDFs, printer blobs).
    # Set SANITIZER_ENABLED=false to bypass entirely (e.g. for debugging).
    sanitizer_enabled: bool = True
    # Only sanitize files larger than this threshold (MB). Files below the
    # threshold are passed directly to the extractor — no overhead.
    sanitizer_threshold_mb: int = 25

    # Storage lifecycle — daily cleanup task (Celery Beat, 02:00 UTC).
    # JSON files in extracted_rows/ older than this many days are deleted.
    cleanup_json_retention_days: int = 14
    # Batch upload files older than this many hours are treated as stale orphans.
    cleanup_upload_stale_hours: int = 24
    # When true, cleanup task logs what would be deleted but skips actual deletion.
    # Useful for a manual dry-run verification before the first production run.
    cleanup_dry_run: bool = False

    # Celery / Redis
    redis_url: str = "redis://localhost:6379/0"
    # Set CELERY_ENABLED=true to route batch processing through Celery workers.
    # When false the existing asyncio/thread-pool fallback is used instead.
    celery_enabled: bool = False

    # Object storage (MinIO / S3-compatible) — Phase 3A: connection settings
    # only. No upload/extraction path reads these yet; an empty endpoint means
    # "not configured" and every existing workflow keeps using the filesystem.
    # In Docker the endpoint is the Compose service name (http://minio:9000).
    minio_endpoint: str = ""            # env: MINIO_ENDPOINT
    minio_access_key: str = ""          # env: MINIO_ACCESS_KEY
    minio_secret_key: str = ""          # env: MINIO_SECRET_KEY
    minio_bucket: str = "spir-files"    # env: MINIO_BUCKET
    minio_secure: bool = False          # env: MINIO_SECURE (https when true)

    @property
    def minio_configured(self) -> bool:
        return bool(self.minio_endpoint and self.minio_access_key and self.minio_secret_key)

    # Direct browser-to-MinIO upload (Phase 3D). MINIO_ENDPOINT above is the
    # container-to-container address (http://minio:9000) and stays exactly
    # that; a browser cannot resolve a Compose service name, so presigned
    # upload URLs are signed against this second, browser-reachable address
    # instead (Compose: http://localhost:9000; production: the HTTPS host that
    # fronts MinIO). Empty = direct upload off; every upload keeps the Phase 3C
    # API-streamed path.
    minio_public_endpoint: str = ""          # env: MINIO_PUBLIC_ENDPOINT
    # Optional credential used ONLY to sign the browser's part-upload URLs
    # (a presigned URL carries its access-key id in the query string). Point
    # this at a MinIO user whose policy is limited to the batch_uploads/ prefix
    # so the root key id never reaches a browser. Empty = sign with the main
    # MINIO_ACCESS_KEY / MINIO_SECRET_KEY.
    minio_presign_access_key: str = ""       # env: MINIO_PRESIGN_ACCESS_KEY
    minio_presign_secret_key: str = ""       # env: MINIO_PRESIGN_SECRET_KEY
    # Kill switch: keep the endpoint configured but serve every upload through
    # the API again.
    direct_upload_enabled: bool = True       # env: DIRECT_UPLOAD_ENABLED
    # Size of each multipart part the browser PUTs. S3 requires >= 5 MB for
    # every part but the last; 16 MB keeps a 1.5 GB file under 100 parts.
    direct_upload_part_size_mb: int = 16     # env: DIRECT_UPLOAD_PART_SIZE_MB
    # Lifetime of each presigned part URL. The browser asks the API for fresh
    # URLs when one expires mid-upload, so this only bounds a single part's
    # window, not the whole upload.
    direct_upload_url_ttl_seconds: int = 3600   # env: DIRECT_UPLOAD_URL_TTL_SECONDS

    @field_validator("direct_upload_part_size_mb", mode="after")
    @classmethod
    def _validate_part_size(cls, v: int) -> int:
        if v < 5:
            raise ValueError("DIRECT_UPLOAD_PART_SIZE_MB must be at least 5 (S3 minimum part size)")
        return v

    @field_validator("direct_upload_url_ttl_seconds", mode="after")
    @classmethod
    def _validate_url_ttl(cls, v: int) -> int:
        if not 60 <= v <= 7 * 24 * 3600:
            raise ValueError("DIRECT_UPLOAD_URL_TTL_SECONDS must be between 60 and 604800 (7 days)")
        return v

    @property
    def direct_upload_configured(self) -> bool:
        """Direct browser uploads are possible: switched on, MinIO holds the source objects, public endpoint set."""
        return bool(
            self.direct_upload_enabled
            and self.minio_configured
            and self.minio_public_endpoint
            and (self.upload_storage_backend or self.storage_backend) == "minio"
        )

    # Storage backend (Phase 3B) — which ObjectStorage implementation the
    # factory hands to application code: "filesystem" (current behaviour, the
    # default) or "minio" (requires the MINIO_* settings above). No workflow
    # is switched by this setting yet; it only selects the implementation
    # behind spir_dynamic.services.object_storage.
    storage_backend: str = "filesystem"   # env: STORAGE_BACKEND

    @field_validator("storage_backend", mode="after")
    @classmethod
    def _validate_storage_backend(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in ("filesystem", "minio"):
            raise ValueError("STORAGE_BACKEND must be 'filesystem' or 'minio'")
        return v

    # Source-upload backend (Phase 3C) — overrides storage_backend for the
    # BATCH_UPLOADS area only, i.e. the uploaded SPIR workbooks that the API
    # hands to the Celery workers (large single files and every batch file).
    # "" (default) inherits STORAGE_BACKEND, so nothing changes unless this is
    # set explicitly; "minio" makes MinIO the durable home of those source
    # objects while extracted rows and avatars stay wherever STORAGE_BACKEND
    # puts them. Workers download the object to a temporary file for openpyxl.
    upload_storage_backend: str = ""   # env: UPLOAD_STORAGE_BACKEND

    @field_validator("upload_storage_backend", mode="after")
    @classmethod
    def _validate_upload_storage_backend(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in ("", "filesystem", "minio"):
            raise ValueError("UPLOAD_STORAGE_BACKEND must be '', 'filesystem' or 'minio'")
        return v

    # Where a worker materialises a source object it has to download before
    # extraction (Phase 3C). Must be a local disk the worker can write; the
    # sanitizer writes its stripped copy next to it. Empty = the system temp
    # directory. Never a durable storage area.
    worker_scratch_dir: str = ""   # env: WORKER_SCRATCH_DIR

    # Currency conversion (unit price -> QAR). Daily rates come from the free
    # Frankfurter API (no key, no quota) via services/currency_service.py; the
    # rates are fetched once per processing job and frozen for that job.
    currency_api_base_url: str = "https://api.frankfurter.dev"   # env: CURRENCY_API_BASE_URL
    # Socket timeout (connect + each read) for one rate request — never lets a
    # provider outage hang an extraction thread or Celery worker.
    currency_api_timeout_seconds: float = 10.0                    # env: CURRENCY_API_TIMEOUT_SECONDS
    # How long a fetched daily quote is reused in-process before the provider
    # is asked again (a batch of files should not repeat identical lookups).
    # 0 disables the cache. Expired quotes are never served as a fallback.
    currency_rate_cache_ttl_seconds: int = 3600                   # env: CURRENCY_RATE_CACHE_TTL_SECONDS

    @field_validator("currency_api_base_url", mode="after")
    @classmethod
    def _validate_currency_api_base_url(cls, v: str) -> str:
        v = (v or "").strip().rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("CURRENCY_API_BASE_URL must start with http:// or https://")
        return v

    # Avatar image directory. Deliberately NOT anchored to _PROJECT_ROOT: the
    # avatar endpoints have always used the CWD-relative "storage/avatars"
    # (Docker/production run from the project root), and Phase 3B keeps that
    # physical layout unchanged. Override with an absolute AVATAR_DIR if needed.
    avatar_dir: str = "storage/avatars"   # env: AVATAR_DIR

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def load_keywords() -> dict:
    """Load keywords.yaml once. Edit the YAML and restart to pick up changes."""
    import yaml
    path = Path(get_settings().keywords_config_path)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
