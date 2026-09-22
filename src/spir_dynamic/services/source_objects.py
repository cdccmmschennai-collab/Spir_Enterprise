"""
Source-upload objects — Phase 3C.

The uploaded SPIR workbook that the API hands to a Celery worker (a large
single file, or any batch file) is a *source object* in the BATCH_UPLOADS
storage area. This module is the only place that knows how those objects are
named, stored, materialised for extraction and discarded:

    API                                   worker
    ---                                   ------
    stream upload -> temp file            staged_source(key)
    store_source_upload(temp, key)          |  filesystem backend: process in place
      -> ObjectStorage.put_file             |  any other backend : get_file -> scratch temp
    enqueue task with `key`                 v
                                          sanitizer / run_pipeline (local Path, unchanged)
                                          discard_source_object(key)

Object key
----------
    <job_id>_<idx:03d>_<safe_filename>

`job_id` is the uuid4 batch job id and `idx` the file's slot in that job, so
the key is unique per upload slot and stable across retries of the same slot
(a re-upload or a re-dispatch overwrites the same object rather than adding a
second one). `safe_filename` is the original name reduced to a single safe
path segment (kept so the object is recognisable in the bucket and so the
extension survives for the sanitizer). No host path is part of the key.

On the filesystem backend the key maps to <batch_upload_dir>/<key> — exactly
the file the pre-3C code wrote — so filesystem mode keeps its physical layout
and its existing stale-file cleanup. On MinIO it becomes batch_uploads/<key>.

Only the worker deletes a source object (after success, or once a failure is
final). Objects orphaned by a crash are removed by the lifecycle cleanup task
once they are older than cleanup_upload_stale_hours.
"""
from __future__ import annotations

import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterator

import structlog

from spir_dynamic.services.object_storage import (
    ObjectInfo,
    ObjectNotFound,
    ObjectStorage,
    StorageArea,
    get_object_storage,
    normalize_key,
)

log = structlog.stdlib.get_logger(__name__)

# Worker-side temp files: "<prefix>p<pid>_<job>_<idx>_<random><ext>" in the
# scratch dir. The pid lets a replacement worker process reclaim the files of
# a process that died (OOM kill) without waiting for the age cutoff.
SCRATCH_PREFIX = "spir_src_"
# A scratch file older than this cannot belong to a running task (the longest
# Celery hard time limit is the giant queue's 36 min), so a worker may sweep it
# even when its owning pid is still alive (pid reuse).
SCRATCH_STALE_SECONDS = 4 * 3600


# ── naming ───────────────────────────────────────────────────────────────────

def safe_filename(filename: str) -> str:
    """Original upload name reduced to one safe key segment (same rule as the pre-3C disk name)."""
    return re.sub(r'[^\w\-_. ]', '_', Path(filename).name)[:80] or "upload"


def source_object_key(job_id: str, idx: int, filename: str) -> str:
    """Object key for one upload slot of a job. Deterministic; validated as a storage key."""
    return normalize_key(f"{job_id}_{idx:03d}_{safe_filename(filename)}")


# ── backend ──────────────────────────────────────────────────────────────────

def get_source_storage() -> ObjectStorage:
    """The BATCH_UPLOADS area backend (filesystem unless UPLOAD_STORAGE_BACKEND says otherwise)."""
    return get_object_storage(StorageArea.BATCH_UPLOADS)


# ── API side ─────────────────────────────────────────────────────────────────

def store_source_upload(temp_path: Path, key: str, storage: ObjectStorage | None = None) -> ObjectInfo:
    """
    Persist a streamed upload (a local temp file) as the source object `key`.

    Blocking — call from a thread (run_in_executor). The temp file is removed
    afterwards whether or not the put succeeded: it is a staging copy nobody
    else references. Storage errors propagate (StorageUnavailable, ...).
    """
    st = storage or get_source_storage()
    temp_path = Path(temp_path)
    try:
        info = st.put_file(key, temp_path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("source.temp_cleanup_failed", path=str(temp_path), exc_message=str(exc))
    log.info("source.stored", key=key, backend=st.backend, size_mb=round(info.size / (1024 * 1024), 1))
    return info


def discard_source_object(key: str, *, log_context: str = "", storage: ObjectStorage | None = None) -> bool:
    """
    Delete the durable source object with the server-side credentials. Never
    raises — the storage equivalent of cleanup.safe_delete(): an extraction
    that produced rows is a success even if its source could not be removed,
    and the stale sweep will catch the leftover.

    Never raising is not the same as staying quiet. A failure is logged at
    error level with the key, the calling context and the backend, and counted
    in SOURCE_DELETE_FAILURES, because the silent version of this is how 8 GB
    of processed uploads accumulated behind an AccessDenied nobody was
    watching. True if the object existed and was removed.
    """
    st: ObjectStorage | None = None
    try:
        st = storage or get_source_storage()
        removed = st.delete(key)
    except Exception as exc:
        from spir_dynamic.monitoring.metrics import SOURCE_DELETE_FAILURES
        SOURCE_DELETE_FAILURES.labels(stage="task").inc()
        log.error(
            "source.delete_failed",
            key=key,
            context=log_context,
            backend=getattr(st, "backend", "unknown"),
            error_type=type(exc).__name__,
            exc_message=str(exc),
        )
        return False
    if removed:
        log.info("source.deleted", key=key, context=log_context, backend=st.backend)
    else:
        log.debug("source.absent", key=key, context=log_context)
    return removed


# ── worker side ──────────────────────────────────────────────────────────────

def scratch_dir(configured: str = "") -> Path:
    """Local directory for worker temp files: WORKER_SCRATCH_DIR or the system temp dir."""
    return Path(configured) if configured else Path(tempfile.gettempdir())


@contextmanager
def staged_source(
    key: str,
    *,
    storage: ObjectStorage | None = None,
    scratch: Path | None = None,
    log_context: str = "",
) -> Iterator[Path]:
    """
    Materialise source object `key` as a local file for the extraction pipeline.

    Yields a Path the existing sanitizer / openpyxl code can open. With the
    filesystem backend that is the object's own file (processed in place, as
    before Phase 3C). With any other backend the object is downloaded into a
    unique temp file in the scratch dir, which is deleted when the block exits
    — on success, on error, and on retry alike. The durable object is never
    modified here.

    Raises ObjectNotFound if the object is missing, StorageUnavailable /
    StorageError if it cannot be fetched.
    """
    st = storage or get_source_storage()

    # The filesystem backend exposes the physical file; nothing to copy.
    path_for = getattr(st, "path_for", None)
    if path_for is not None:
        if not st.exists(key):
            raise ObjectNotFound(key)
        yield path_for(key)
        return

    suffix = PurePosixPath(key).suffix.lower()
    scratch = scratch or scratch_dir()
    scratch.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{SCRATCH_PREFIX}p{os.getpid()}_{PurePosixPath(key).stem[:60]}_", suffix=suffix, dir=scratch,
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        t0 = time.perf_counter()
        st.get_file(key, tmp_path)
        log.info(
            "source.staged",
            key=key,
            backend=st.backend,
            path=str(tmp_path),
            size_mb=round(tmp_path.stat().st_size / (1024 * 1024), 1),
            duration_s=round(time.perf_counter() - t0, 2),
            context=log_context,
        )
        yield tmp_path
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
            log.debug("source.staged_removed", path=str(tmp_path), context=log_context)
        except OSError as exc:
            log.warning("source.staged_cleanup_failed", path=str(tmp_path), exc_message=str(exc))


def sweep_stale_scratch(scratch: Path, max_age_seconds: int = SCRATCH_STALE_SECONDS) -> int:
    """
    Remove leftover worker temp files: downloaded sources whose owning worker
    process is gone (pid in the name no longer alive — e.g. OOM-killed), plus
    any download or sanitizer san_* copy older than max_age_seconds. Called
    when a worker process starts, so the child that replaces a crashed one
    cleans up after it right away. Recent files of a live pid may belong to a
    running sibling process and are left alone. Returns the number removed.
    """
    if not scratch.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for pattern in (f"{SCRATCH_PREFIX}*", "san_*"):
        for p in scratch.glob(pattern):
            try:
                if not p.is_file():
                    continue
                if p.stat().st_mtime < cutoff or _owner_dead(p.name):
                    p.unlink(missing_ok=True)
                    log.info("source.stale_scratch_removed", path=str(p))
                    removed += 1
            except OSError as exc:
                log.warning("source.stale_scratch_error", path=str(p), exc_message=str(exc))
    return removed


def _owner_dead(name: str) -> bool:
    """True if `name` carries a "p<pid>_" tag whose process no longer exists (never for this process)."""
    m = re.match(re.escape(SCRATCH_PREFIX) + r"p(\d+)_", name)
    if not m:
        return False
    pid = int(m.group(1))
    if pid == os.getpid():
        return False
    # /proc is what the Linux worker containers have; elsewhere (Windows dev
    # runs) only the age rule applies. os.kill(pid, 0) is deliberately not
    # used: on Windows it terminates the process instead of probing it.
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    return not (proc / str(pid)).exists()
