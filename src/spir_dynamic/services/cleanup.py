"""
Safe file cleanup utilities.

Use safe_delete() anywhere a file might or might not exist.
Use cleanup_stale_uploads() on API/worker startup to recover from
crashed extraction workers that left orphaned upload files.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)


def safe_delete(path: "str | Path", *, log_context: str = "") -> bool:
    """
    Delete a file. Returns True if deleted, False if already absent.
    Never raises — logs a warning on permission or IO errors.
    """
    p = Path(path)
    ctx = f" [{log_context}]" if log_context else ""
    try:
        if p.exists():
            p.unlink()
            log.info("File deleted%s: %s", ctx, p)
            return True
        log.debug("File already absent%s: %s", ctx, p)
        return False
    except Exception as exc:
        log.warning("Delete failed%s: %s — %s", ctx, p, exc)
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
                log.info("Stale batch upload removed: %s", p)
                deleted += 1
        except Exception as exc:
            log.warning("Stale upload cleanup error for %s: %s", p, exc)
    if deleted:
        log.info("Startup cleanup: removed %d stale batch upload(s) from %s", deleted, d)
    return deleted
