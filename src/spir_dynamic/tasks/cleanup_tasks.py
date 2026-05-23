"""
Celery periodic task: storage lifecycle management.

Scheduled via Celery Beat at 02:00 UTC daily.

Four phases run in sequence — any individual phase failure is logged and
skipped; the remaining phases still execute.

Phase 1 — Expire JSON files
  Delete extracted_rows/*.json older than CLEANUP_JSON_RETENTION_DAYS (14).
  These files are written after every successful extraction and hold the
  full row payload for the combine/download feature.  After the retention
  window most users have already downloaded their result.

Phase 2 — Orphan JSON cleanup
  Delete extracted_rows/*.json whose file_id has no matching row in
  extraction_history.  This catches files left behind by:
    - crashed extraction tasks that wrote the file but not the DB record
    - manual DB truncates / row deletes
  Only runs when DATABASE_URL is configured.  Skipped silently otherwise.

Phase 3 — Stale batch uploads
  Delete storage/batch_uploads/* older than CLEANUP_UPLOAD_STALE_HOURS (24).
  Normally the extraction task deletes uploads immediately on success or
  after exhausting retries.  This phase catches anything that slipped
  through (worker killed mid-task, disk full at cleanup time, etc.).

Phase 4 — Disk metrics
  Refresh Prometheus Gauges for JSON count, JSON dir size, and upload dir
  size so Grafana always shows current storage state.

Dry-run mode:
  Set CLEANUP_DRY_RUN=true in .env, or pass dry_run=True when triggering
  manually via Celery CLI.  All phases log what would be deleted but make
  no filesystem changes.

Audit:
  All actions logged via structlog to the system journal.
  No DB audit rows are written — cleanup is housekeeping, not user activity.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import structlog

from spir_dynamic.celery_app import celery_app

log = structlog.stdlib.get_logger(__name__)


@celery_app.task(
    name="spir_dynamic.tasks.lifecycle_cleanup",
    bind=True,
    max_retries=0,        # cleanup failures are logged, not retried
    acks_late=False,      # safe to rerun if the Beat fires twice
    ignore_result=True,
)
def lifecycle_cleanup_task(self, dry_run: bool = False) -> dict:
    """
    Full storage lifecycle cleanup.

    Can be triggered manually:
        celery -A spir_dynamic.celery_app call \\
            spir_dynamic.tasks.lifecycle_cleanup \\
            --kwargs '{"dry_run": true}'

    Returns a summary dict for inspection (result_backend not used for Beat tasks).
    """
    from spir_dynamic.app.config import get_settings
    cfg = get_settings()

    # Config-level dry_run overrides task arg (env var takes precedence)
    effective_dry_run = dry_run or cfg.cleanup_dry_run

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        cleanup_task_id=self.request.id or "manual",
        dry_run=effective_dry_run,
    )

    t0 = time.perf_counter()
    log.info("cleanup.start", dry_run=effective_dry_run)

    rows_dir   = Path(cfg.rows_storage_path)
    upload_dir = Path(cfg.batch_upload_dir)

    summary: dict = {
        "dry_run": effective_dry_run,
        "phases": {},
    }

    # Phase 1 — Expire JSON files
    try:
        result = _purge_old_json_files(
            rows_dir,
            retention_days=cfg.cleanup_json_retention_days,
            dry_run=effective_dry_run,
        )
        summary["phases"]["json_expiry"] = result
    except Exception as exc:
        log.error("cleanup.phase_failed", phase="json_expiry", exc_message=str(exc))
        summary["phases"]["json_expiry"] = {"error": str(exc)}

    # Phase 2 — Orphan JSON cleanup (DB-dependent)
    try:
        result = _purge_orphan_json_files(
            rows_dir,
            database_url=cfg.database_url,
            dry_run=effective_dry_run,
        )
        summary["phases"]["orphan_cleanup"] = result
    except Exception as exc:
        log.error("cleanup.phase_failed", phase="orphan_cleanup", exc_message=str(exc))
        summary["phases"]["orphan_cleanup"] = {"error": str(exc)}

    # Phase 3 — Stale upload staging files
    try:
        result = _purge_stale_uploads(
            upload_dir,
            stale_hours=cfg.cleanup_upload_stale_hours,
            dry_run=effective_dry_run,
        )
        summary["phases"]["stale_uploads"] = result
    except Exception as exc:
        log.error("cleanup.phase_failed", phase="stale_uploads", exc_message=str(exc))
        summary["phases"]["stale_uploads"] = {"error": str(exc)}

    # Phase 4 — Refresh disk metrics (always runs, even in dry-run)
    try:
        result = _update_disk_metrics(rows_dir, upload_dir)
        summary["phases"]["disk_metrics"] = result
    except Exception as exc:
        log.error("cleanup.phase_failed", phase="disk_metrics", exc_message=str(exc))
        summary["phases"]["disk_metrics"] = {"error": str(exc)}

    from spir_dynamic.monitoring.metrics import CLEANUP_DURATION
    duration_s = round(time.perf_counter() - t0, 2)
    CLEANUP_DURATION.observe(duration_s)

    log.info(
        "cleanup.done",
        duration_s=duration_s,
        dry_run=effective_dry_run,
        phases=list(summary["phases"].keys()),
    )
    return summary


# ── Phase 1: Expire old JSON files ────────────────────────────────────────────

def _purge_old_json_files(
    rows_dir: Path,
    retention_days: int,
    dry_run: bool,
) -> dict:
    """
    Delete extracted_rows/*.json files older than retention_days.

    Only considers files at the top level of rows_dir — no subdirectory walk.
    Safety guard: skips files modified within the last hour regardless of mtime
    (protects files from an in-progress extraction that started near midnight).
    """
    from spir_dynamic.monitoring.metrics import CLEANUP_DELETED_FILES

    if not rows_dir.is_dir():
        log.info("cleanup.json_expiry.skip", reason="dir_not_found", path=str(rows_dir))
        return {"skipped": True, "reason": "dir_not_found"}

    cutoff_s    = time.time() - (retention_days * 86400)
    safe_guard  = time.time() - 3600   # never touch files modified in last 1h
    deleted     = 0
    skipped     = 0
    bytes_freed = 0

    for p in rows_dir.iterdir():
        if not p.is_file() or p.suffix != ".json":
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue

        if mtime >= safe_guard:
            skipped += 1
            continue

        if mtime < cutoff_s:
            size = p.stat().st_size
            if dry_run:
                log.info(
                    "cleanup.json_expiry.would_delete",
                    path=p.name,
                    age_days=round((time.time() - mtime) / 86400, 1),
                )
            else:
                try:
                    p.unlink(missing_ok=True)
                    CLEANUP_DELETED_FILES.labels(reason="expired").inc()
                    log.info(
                        "cleanup.json_expiry.deleted",
                        path=p.name,
                        age_days=round((time.time() - mtime) / 86400, 1),
                    )
                    deleted += 1
                    bytes_freed += size
                except OSError as exc:
                    log.warning("cleanup.json_expiry.delete_failed", path=str(p), exc_message=str(exc))

    mb_freed = round(bytes_freed / (1024 * 1024), 2)
    log.info(
        "cleanup.json_expiry.done",
        deleted=deleted,
        skipped_recent=skipped,
        mb_freed=mb_freed,
        retention_days=retention_days,
        dry_run=dry_run,
    )
    return {
        "deleted": deleted,
        "skipped_recent": skipped,
        "mb_freed": mb_freed,
        "retention_days": retention_days,
    }


# ── Phase 2: Orphan JSON cleanup ──────────────────────────────────────────────

def _purge_orphan_json_files(
    rows_dir: Path,
    database_url: str,
    dry_run: bool,
) -> dict:
    """
    Delete extracted_rows/*.json files whose file_id has no DB record.

    Queries extraction_history for all known file_ids, then removes any
    JSON file on disk whose stem (filename without extension) is not in
    that set.

    Skipped when DATABASE_URL is not configured.
    """
    from spir_dynamic.monitoring.metrics import CLEANUP_DELETED_FILES

    if not database_url:
        log.info("cleanup.orphan.skip", reason="no_database_url")
        return {"skipped": True, "reason": "no_database_url"}

    if not rows_dir.is_dir():
        log.info("cleanup.orphan.skip", reason="dir_not_found")
        return {"skipped": True, "reason": "dir_not_found"}

    known_file_ids = _fetch_known_file_ids(database_url)
    if known_file_ids is None:
        return {"skipped": True, "reason": "db_query_failed"}

    safe_guard = time.time() - 3600  # skip files touched in last 1h

    deleted     = 0
    skipped     = 0
    bytes_freed = 0

    for p in rows_dir.iterdir():
        if not p.is_file() or p.suffix != ".json":
            continue

        file_id = p.stem

        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue

        # Safety: never touch recently modified files — they may be mid-write.
        if mtime >= safe_guard:
            skipped += 1
            continue

        if file_id not in known_file_ids:
            size = p.stat().st_size
            if dry_run:
                log.info("cleanup.orphan.would_delete", path=p.name, file_id=file_id)
            else:
                try:
                    p.unlink(missing_ok=True)
                    CLEANUP_DELETED_FILES.labels(reason="orphan").inc()
                    log.info("cleanup.orphan.deleted", path=p.name, file_id=file_id)
                    deleted += 1
                    bytes_freed += size
                except OSError as exc:
                    log.warning(
                        "cleanup.orphan.delete_failed",
                        path=str(p),
                        exc_message=str(exc),
                    )

    mb_freed = round(bytes_freed / (1024 * 1024), 2)
    log.info(
        "cleanup.orphan.done",
        deleted=deleted,
        skipped_recent=skipped,
        mb_freed=mb_freed,
        known_records=len(known_file_ids),
        dry_run=dry_run,
    )
    return {
        "deleted": deleted,
        "skipped_recent": skipped,
        "mb_freed": mb_freed,
        "known_records": len(known_file_ids),
    }


def _fetch_known_file_ids(database_url: str) -> Optional[set[str]]:
    """
    Query extraction_history and return the set of all known file_ids.

    Uses a sync psycopg2 NullPool engine — safe for Celery worker processes.
    Returns None on connection/query failure.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    sync_url = database_url.replace("+asyncpg", "+psycopg2")
    if sync_url.startswith("postgres://"):
        sync_url = "postgresql+psycopg2://" + sync_url[len("postgres://"):]

    try:
        engine = create_engine(sync_url, poolclass=NullPool, pool_pre_ping=True)
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT file_id FROM extraction_history WHERE file_id IS NOT NULL")
            ).fetchall()
        engine.dispose()
        return {row[0] for row in rows if row[0]}
    except Exception as exc:
        log.warning("cleanup.orphan.db_query_failed", exc_message=str(exc))
        return None


# ── Phase 3: Stale batch upload cleanup ───────────────────────────────────────

def _purge_stale_uploads(
    upload_dir: Path,
    stale_hours: int,
    dry_run: bool,
) -> dict:
    """
    Delete batch_uploads/* files older than stale_hours.

    Normally the extraction task deletes uploads immediately after processing.
    This phase catches stragglers: uploads from crashed workers, tasks that
    exhausted retries before cleanup ran, or temporary filesystem errors at
    deletion time.

    Skips: directories, hidden files, non-file entries.
    """
    from spir_dynamic.monitoring.metrics import CLEANUP_DELETED_FILES

    if not upload_dir.is_dir():
        log.info("cleanup.uploads.skip", reason="dir_not_found", path=str(upload_dir))
        return {"skipped": True, "reason": "dir_not_found"}

    cutoff_s    = time.time() - (stale_hours * 3600)
    safe_guard  = time.time() - 3600
    deleted     = 0
    skipped     = 0
    bytes_freed = 0

    for p in upload_dir.iterdir():
        if not p.is_file() or p.name.startswith("."):
            continue
        try:
            mtime = p.stat().st_mtime
            size  = p.stat().st_size
        except OSError:
            continue

        if mtime >= safe_guard:
            skipped += 1
            continue

        if mtime < cutoff_s:
            if dry_run:
                log.info(
                    "cleanup.uploads.would_delete",
                    path=p.name,
                    age_h=round((time.time() - mtime) / 3600, 1),
                )
            else:
                try:
                    p.unlink(missing_ok=True)
                    CLEANUP_DELETED_FILES.labels(reason="stale_upload").inc()
                    log.info(
                        "cleanup.uploads.deleted",
                        path=p.name,
                        age_h=round((time.time() - mtime) / 3600, 1),
                    )
                    deleted += 1
                    bytes_freed += size
                except OSError as exc:
                    log.warning(
                        "cleanup.uploads.delete_failed",
                        path=str(p),
                        exc_message=str(exc),
                    )

    mb_freed = round(bytes_freed / (1024 * 1024), 2)
    log.info(
        "cleanup.uploads.done",
        deleted=deleted,
        skipped_recent=skipped,
        mb_freed=mb_freed,
        stale_hours=stale_hours,
        dry_run=dry_run,
    )
    return {
        "deleted": deleted,
        "skipped_recent": skipped,
        "mb_freed": mb_freed,
        "stale_hours": stale_hours,
    }


# ── Phase 4: Disk metrics ─────────────────────────────────────────────────────

def _update_disk_metrics(rows_dir: Path, upload_dir: Path) -> dict:
    """
    Measure current storage state and update Prometheus Gauges.

    Always runs (even in dry-run mode) — reading filesystem state is safe.
    """
    from spir_dynamic.monitoring.metrics import (
        STORAGE_JSON_COUNT,
        STORAGE_JSON_SIZE_MB,
        STORAGE_UPLOAD_SIZE_MB,
    )

    json_count   = 0
    json_bytes   = 0
    upload_bytes = 0

    if rows_dir.is_dir():
        for p in rows_dir.iterdir():
            if p.is_file() and p.suffix == ".json":
                json_count += 1
                try:
                    json_bytes += p.stat().st_size
                except OSError:
                    pass

    if upload_dir.is_dir():
        for p in upload_dir.iterdir():
            if p.is_file():
                try:
                    upload_bytes += p.stat().st_size
                except OSError:
                    pass

    json_mb   = round(json_bytes   / (1024 * 1024), 2)
    upload_mb = round(upload_bytes / (1024 * 1024), 2)

    STORAGE_JSON_COUNT.set(json_count)
    STORAGE_JSON_SIZE_MB.set(json_mb)
    STORAGE_UPLOAD_SIZE_MB.set(upload_mb)

    log.info(
        "cleanup.metrics_updated",
        json_count=json_count,
        json_size_mb=json_mb,
        upload_size_mb=upload_mb,
    )
    return {
        "json_count": json_count,
        "json_size_mb": json_mb,
        "upload_size_mb": upload_mb,
    }
