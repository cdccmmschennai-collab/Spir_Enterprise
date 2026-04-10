"""
Centralized configuration via environment variables / .env file.
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

    # Batch processing
    batch_max_files: int = 20
    batch_ttl_seconds: int = 7200

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
