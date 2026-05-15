"""
User-triggered storage deletion.

Called by the /history DELETE endpoint after DB records are confirmed owned
by the requesting user.

Design principles:
  - Only deletes paths that are explicitly referenced in DB records (no scanning)
  - Path-validates every file before touching it (prevent traversal)
  - Idempotent: already-missing files are not errors
  - Per-file try/except: one failure does not abort the rest
  - Prunes empty parent dirs after each deletion so {user_id}/{job_id}/ dirs
    do not accumulate as orphaned empty folders
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from spir_dynamic.utils.safe_storage import remove_empty_parents

log = logging.getLogger(__name__)


def delete_storage_for_records(records, storage_root: Path) -> dict[str, int]:
    """
    Delete on-disk JSON files for a list of ExtractionHistory ORM objects.

    Args:
        records:      Iterable of ExtractionHistory instances. Must already be
                      filtered by ownership — this function does no auth checks.
        storage_root: Canonical absolute path to the storage root directory.

    Returns:
        Counts dict with keys: deleted, missing, blocked, errors.
    """
    root = Path(os.path.realpath(str(storage_root)))
    counts = {"deleted": 0, "missing": 0, "blocked": 0, "errors": 0}

    for rec in records:
        if not getattr(rec, "json_path", None):
            continue

        p = Path(os.path.realpath(rec.json_path))

        # Reject any path that escapes storage_root (DB corruption / injection guard)
        try:
            p.relative_to(root)
        except ValueError:
            log.warning(
                "delete_blocked record_id=%s path=%s reason=outside_storage_root",
                rec.id, p,
            )
            counts["blocked"] += 1
            continue

        if not p.exists():
            counts["missing"] += 1
            continue

        try:
            p.unlink()
            counts["deleted"] += 1
            log.info("file_deleted record_id=%s path=%s", rec.id, p)
            remove_empty_parents(p, root)
        except OSError as exc:
            counts["errors"] += 1
            log.warning("file_delete_failed record_id=%s path=%s error=%s", rec.id, p, exc)

    return counts
