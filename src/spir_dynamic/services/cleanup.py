"""
Safe file cleanup utilities.

Use safe_delete() anywhere a file might or might not exist.
Use cleanup_stale_uploads() on API/worker startup to recover from
crashed extraction workers that left orphaned upload files.
"""
from __future__ import annotations

import time
from pathlib import Path

import structlog

log = structlog.stdlib.get_logger(__name__)


def safe_delete(path: "str | Path", *, log_context: str = "") -> bool:
    """
    Delete a file. Returns True if deleted, False if already absent.
    Never raises — logs a warning on permission or IO errors.
    """
    p = Path(path)
    try:
        if p.exists():
            p.unlink()
            log.info("file.deleted", path=str(p), context=log_context)
            return True
        log.debug("file.absent", path=str(p), context=log_context)
        return False
    except Exception as exc:
        log.warning("file.delete_failed", path=str(p), context=log_context, exc_message=str(exc))
        return False


def cleanup_stale_uploads(upload_dir: "str | Path", max_age_seconds: int = 86400) -> int:
    """
    Remove files in upload_dir that are older than max_age_seconds.
    Called on startup to remove orphans left by crashed workers.
    Returns the number of files deleted.
    """
    d = Path(upload_dir)
    if not d.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    deleted = 0
    for p in d.iterdir():
        if not p.is_file():
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                log.info("upload.stale_removed", path=str(p))
                deleted += 1
        except Exception as exc:
            log.warning("upload.stale_cleanup_error", path=str(p), exc_message=str(exc))
    if deleted:
        log.info("upload.startup_cleanup", removed=deleted, dir=str(d))
    return deleted
