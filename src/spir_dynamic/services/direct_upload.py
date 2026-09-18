"""
Direct browser-to-MinIO source uploads — Phase 3D.

The Phase 3C source object (services/source_objects.py) is still the unit the
Celery worker consumes; this module only changes how that object gets INTO
storage for large uploads. Instead of the browser streaming the whole workbook
through FastAPI, the API signs one PUT URL per multipart part and the browser
writes the parts straight to MinIO:

    browser                     API (this module + app/upload_router.py)         MinIO
    -------                     ---------------------------------------          -----
    initiate(filename, size) -> plan_upload(): key, multipart, presigned URLs
                                job slot -> "uploading"
    PUT part n ---------------------------------------------------------------> part n
    ...
    complete() --------------->  claim slot (atomic)
                                 list_parts() <---------------------------------  server-side truth
                                 verify count/sizes -> complete multipart -> stat(size)
                                 dispatch Celery with the SAME source key (Phase 3C contract)
                                 job slot -> "pending" (worker: running -> ok/error)

The server owns every decision: which job/slot, which key (source_object_key
— never client-supplied), how many parts of what size, what the finished
object must look like, and when Celery may start. The browser only ever sees
short-lived, single-part, PUT-only URLs. Nothing here is trusted from the
client except the declared size (verified byte-for-byte on completion) and
the part numbers it wants re-signed.

Abandoned uploads (tab closed mid-transfer, lost session) are reclaimed two
ways. Every upload started here is recorded in a small Redis index
(open_uploads) until it is completed or aborted; the lifecycle cleanup task
walks that index and aborts entries older than cleanup_upload_stale_hours.
MinIO cannot list in-progress multipart uploads by prefix — its
ListMultipartUploads only answers for an exact object key — which is why the
application keeps this index itself. MinIO's own stale-upload expiry
(api stale_uploads_expiry, 24 h by default, set explicitly in Compose) is the
backstop for anything the index misses, e.g. an index write lost to a Redis
outage.

Storage failures surface as StorageError subclasses (never botocore); the
router maps them to HTTP. Everything in this module is blocking — call it
from a thread (run_in_executor) inside the API.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import structlog

from spir_dynamic.services.object_storage import (
    DirectUploadStorage,
    MultipartUploadInfo,
    MultipartUploadNotFound,
    ObjectInfo,
    StorageArea,
    StorageError,
    UploadedPart,
    get_object_storage,
)
from spir_dynamic.services.source_objects import discard_source_object, source_object_key

log = structlog.stdlib.get_logger(__name__)

# S3 hard limit on parts per multipart upload.
MAX_PARTS = 10_000

# Redis hash of multipart uploads this application started and has not yet
# completed or aborted: field "<key>\n<upload_id>" -> initiated unix time.
# Refreshed on every write; long enough to outlive any stale-hours setting.
_INDEX_KEY = "spir:direct_uploads:open"
_INDEX_TTL = 7 * 86400

# Record states. "uploading": the browser may PUT parts / ask for fresh URLs.
# "completing": one complete() call holds the slot; every other call is refused.
STATE_UPLOADING = "uploading"
STATE_COMPLETING = "completing"


# ── Errors (translated to HTTP by the router) ────────────────────────────────

class DirectUploadError(Exception):
    """Base: a direct-upload request cannot be honoured. `status` is the HTTP status to return."""
    status = 400

    def __init__(self, message: str, **extra) -> None:
        super().__init__(message)
        self.extra = extra


class UploadConflict(DirectUploadError):
    """The slot is not in the state the operation needs (duplicate completion, already queued, ...)."""
    status = 409


class UploadIncomplete(UploadConflict):
    """Completion asked for but the backend does not hold every part yet — the client may retry those."""


class UploadVerificationFailed(DirectUploadError):
    """The assembled object is not the file the client declared; it has been discarded."""
    status = 422


# ── Session record ───────────────────────────────────────────────────────────

@dataclass
class UploadSession:
    """Everything the API needs to finish or abort one slot's direct upload (kept in the job store)."""
    job_id: str
    file_idx: int
    filename: str
    size: int
    source_key: str
    upload_id: str
    part_size: int
    part_count: int
    user_id: str
    queue: str
    state: str = STATE_UPLOADING
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "UploadSession":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


# ── Backend ──────────────────────────────────────────────────────────────────

def direct_upload_storage() -> DirectUploadStorage | None:
    """The BATCH_UPLOADS backend if it can take browser uploads, else None (filesystem, or no public endpoint)."""
    st = get_object_storage(StorageArea.BATCH_UPLOADS)
    if isinstance(st, DirectUploadStorage) and st.supports_direct_upload():
        return st
    return None


# ── Index of open uploads (best effort — never raises) ───────────────────────

def _redis() -> Any:
    from spir_dynamic.app.config import get_settings
    import redis
    return redis.from_url(get_settings().redis_url, socket_timeout=2, socket_connect_timeout=2,
                          decode_responses=True)


def index_open_upload(key: str, upload_id: str) -> None:
    try:
        r = _redis()
        r.hset(_INDEX_KEY, f"{key}\n{upload_id}", str(time.time()))
        r.expire(_INDEX_KEY, _INDEX_TTL)
    except Exception as exc:   # MinIO's own stale-upload expiry is the backstop
        log.warning("upload.index_failed", op="open", source_key=key, exc_message=str(exc))


def index_close_upload(key: str, upload_id: str) -> None:
    try:
        _redis().hdel(_INDEX_KEY, f"{key}\n{upload_id}")
    except Exception as exc:
        log.warning("upload.index_failed", op="close", source_key=key, exc_message=str(exc))


def open_uploads() -> list[MultipartUploadInfo]:
    """Every upload started by this application that has not been completed or aborted (oldest first)."""
    try:
        raw = _redis().hgetall(_INDEX_KEY)
    except Exception as exc:
        log.warning("upload.index_failed", op="list", exc_message=str(exc))
        return []
    out: list[MultipartUploadInfo] = []
    for field, ts in raw.items():
        key, _, upload_id = field.partition("\n")
        try:
            initiated = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (TypeError, ValueError):
            continue
        out.append(MultipartUploadInfo(key=key, upload_id=upload_id, initiated=initiated))
    return sorted(out, key=lambda u: u.initiated)


# ── Part arithmetic (pure) ───────────────────────────────────────────────────

def part_count(size: int, part_size: int) -> int:
    if size <= 0 or part_size <= 0:
        raise ValueError("size and part_size must be positive")
    n = -(-size // part_size)
    if n > MAX_PARTS:
        raise ValueError(f"{size} bytes in {part_size}-byte parts needs {n} parts; the maximum is {MAX_PARTS}")
    return n


def expected_part_size(size: int, part_size: int, part_number: int) -> int:
    """Bytes part `part_number` (1-based) must hold: full parts, then whatever is left for the last one."""
    n = part_count(size, part_size)
    if not 1 <= part_number <= n:
        raise ValueError(f"part number {part_number} out of range 1..{n}")
    return part_size if part_number < n else size - part_size * (n - 1)


def verify_parts(parts: list[UploadedPart], size: int, part_size: int) -> tuple[list[int], list[int]]:
    """
    Compare what the backend holds with what the declared size requires.
    Returns (missing part numbers, part numbers with the wrong size / extras).
    Empty lists mean the upload can be completed.
    """
    n = part_count(size, part_size)
    by_number = {p.part_number: p for p in parts}
    missing = [i for i in range(1, n + 1) if i not in by_number]
    wrong = [
        p.part_number for p in parts
        if p.part_number < 1 or p.part_number > n or p.size != expected_part_size(size, part_size, p.part_number)
    ]
    return missing, sorted(wrong)


# ── Operations ───────────────────────────────────────────────────────────────

def plan_upload(
    storage: DirectUploadStorage,
    *,
    job_id: str,
    file_idx: int,
    filename: str,
    size: int,
    user_id: str,
    queue: str,
    part_size: int,
    url_ttl: int,
) -> tuple[UploadSession, list[dict]]:
    """
    Create the multipart upload for slot (job_id, file_idx) and sign every
    part URL. The object key is derived exactly as Phase 3C does it
    (source_object_key), so the worker later stages the same object.
    Returns (session, [{"part_number": n, "url": ...}, ...]).
    """
    key = source_object_key(job_id, file_idx, filename)
    n = part_count(size, part_size)
    upload_id = storage.create_multipart_upload(key)
    index_open_upload(key, upload_id)
    try:
        urls = presign_parts(storage, key, upload_id, range(1, n + 1), url_ttl)
    except Exception:
        # Never leave an orphan multipart upload behind a failed initiate.
        _abort_quietly(storage, key, upload_id, context="presign-failed")
        raise
    session = UploadSession(
        job_id=job_id, file_idx=file_idx, filename=filename, size=size, source_key=key,
        upload_id=upload_id, part_size=part_size, part_count=n, user_id=user_id, queue=queue,
        created_at=time.time(),
    )
    return session, urls


def presign_parts(storage: DirectUploadStorage, key: str, upload_id: str, numbers, url_ttl: int) -> list[dict]:
    return [
        {"part_number": int(n), "url": storage.presign_upload_part(key, upload_id, int(n), expires_in=url_ttl)}
        for n in numbers
    ]


def finalize_upload(storage: DirectUploadStorage, session: UploadSession) -> ObjectInfo:
    """
    Turn the parts MinIO holds into the source object and prove it is the
    declared file. Raises UploadIncomplete (parts missing / wrong size — the
    upload stays open for the client to retry those parts), UploadConflict
    (the multipart upload no longer exists) or UploadVerificationFailed (the
    assembled object's size differs from the declared size — it is deleted).
    """
    key, upload_id = session.source_key, session.upload_id
    try:
        parts = storage.list_parts(key, upload_id)
    except MultipartUploadNotFound:
        index_close_upload(key, upload_id)
        raise UploadConflict("upload no longer exists (already completed, aborted or expired)")

    missing, wrong = verify_parts(parts, session.size, session.part_size)
    if missing or wrong:
        log.warning(
            "upload.incomplete",
            job_id=session.job_id, file_idx=session.file_idx, source_key=key,
            missing=len(missing), wrong_size=len(wrong), received=len(parts), expected=session.part_count,
        )
        raise UploadIncomplete(
            f"upload incomplete: {len(missing)} part(s) missing, {len(wrong)} part(s) with the wrong size",
            missing_parts=missing, wrong_parts=wrong,
        )

    try:
        info = storage.complete_multipart_upload(key, upload_id, parts)
    except MultipartUploadNotFound:
        index_close_upload(key, upload_id)
        raise UploadConflict("upload no longer exists (already completed, aborted or expired)")
    index_close_upload(key, upload_id)

    if info.size != session.size:
        # The object is not what the client declared — it must not reach a worker.
        discard_source_object(key, log_context=f"verify-failed job={session.job_id} idx={session.file_idx}",
                              storage=storage)   # type: ignore[arg-type]
        raise UploadVerificationFailed(
            f"uploaded object is {info.size} bytes but {session.size} bytes were declared",
            expected_size=session.size, actual_size=info.size,
        )
    return info


def abort_upload(storage: DirectUploadStorage, session: UploadSession, *, context: str) -> None:
    """Discard the in-progress upload AND any object the slot already holds. Never raises."""
    _abort_quietly(storage, session.source_key, session.upload_id, context=context)
    discard_source_object(session.source_key, log_context=context, storage=storage)   # type: ignore[arg-type]


def _abort_quietly(storage: DirectUploadStorage, key: str, upload_id: str, *, context: str) -> bool:
    try:
        removed = storage.abort_multipart_upload(key, upload_id)
    except StorageError as exc:
        log.warning("upload.abort_failed", source_key=key, context=context, exc_message=str(exc))
        return False
    index_close_upload(key, upload_id)
    log.info("upload.multipart_aborted", source_key=key, context=context, existed=removed)
    return removed


def reclaim_abandoned_uploads(storage: DirectUploadStorage, *, stale_hours: int, dry_run: bool) -> dict:
    """
    Abort every indexed upload initiated more than stale_hours ago (a browser
    that is still uploading completes or aborts within the job's 2 h TTL, so
    older ones are abandoned). Uploads younger than an hour are never touched.
    An entry whose upload MinIO no longer knows (completed/aborted/expired by
    MinIO itself) is simply dropped from the index. Used by the lifecycle
    cleanup task; never raises for a single bad entry.
    """
    now = time.time()
    cutoff = now - stale_hours * 3600
    safe_guard = now - 3600
    aborted = skipped = dropped = 0
    for up in open_uploads():
        started = up.initiated.timestamp()
        age_h = round((now - started) / 3600, 1)
        if started >= safe_guard:
            skipped += 1
            continue
        if started >= cutoff:
            continue
        if dry_run:
            log.info("cleanup.multipart.would_abort", key=up.key, age_h=age_h)
            continue
        try:
            existed = storage.abort_multipart_upload(up.key, up.upload_id)
        except StorageError as exc:
            log.warning("cleanup.multipart.abort_failed", key=up.key, exc_message=str(exc))
            continue
        index_close_upload(up.key, up.upload_id)
        if existed:
            aborted += 1
            log.info("cleanup.multipart.aborted", key=up.key, age_h=age_h)
        else:
            dropped += 1
    return {"aborted": aborted, "skipped_recent": skipped, "dropped_stale_entries": dropped}
