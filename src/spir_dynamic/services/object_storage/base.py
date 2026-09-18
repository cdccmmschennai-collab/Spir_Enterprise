"""
Storage contract — Phase 3B.

Application code stores and retrieves *objects* addressed by a *key* and never
talks to a concrete backend directly. Two backends implement this contract:

    LocalFilesystemStorage  — key maps to <root>/<key> on disk (current layout)
    MinioObjectStorage      — key maps to <prefix><key> in one S3 bucket

Semantics every backend must honour
-----------------------------------
Key       POSIX-style relative path: "/"-separated segments, no leading or
          trailing "/", no empty, "." or ".." segments, no backslashes, no NUL,
          at most 1024 characters. `normalize_key()` enforces this and raises
          InvalidObjectKey otherwise. "uploads/<job>/original.xlsx" is a key —
          a prefix is just the leading part of a key, not a directory.
Put       Always overwrites. Last writer wins; there is no "create only" mode.
Missing   get_bytes / get_file / open_read / stat raise ObjectNotFound.
          exists() returns False. delete() returns False and does not raise.
Metadata  ObjectInfo(key, size, last_modified [tz-aware UTC], content_type).
          content_type is best-effort: the MinIO backend stores what was
          supplied (or a guess from the key's extension); the filesystem
          backend cannot persist it and derives it from the extension.
Errors    Anything that is not "object missing" or "bad key" surfaces as a
          StorageError subclass. Backend client exceptions (botocore, OSError)
          never leak out of a backend — StorageUnavailable means the backend
          could not be reached / authenticated / found, StorageConfigError
          means the settings are incomplete or invalid.

The contract is intentionally synchronous (the API already off-loads blocking
file work with run_in_executor and the Celery workers are synchronous).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, ClassVar, Iterator, Protocol, runtime_checkable

MAX_KEY_LENGTH = 1024  # S3 object-key limit; the filesystem backend is stricter in practice


# ── Errors ────────────────────────────────────────────────────────────────────

class StorageError(Exception):
    """Base class for every error raised by a storage backend."""


class InvalidObjectKey(StorageError, ValueError):
    """The key violates the key rules (see module docstring)."""


class ObjectNotFound(StorageError):
    """The requested object does not exist."""

    def __init__(self, key: str) -> None:
        super().__init__(f"object not found: {key}")
        self.key = key


class StorageUnavailable(StorageError):
    """The backend cannot be reached, authenticated against, or is missing its bucket/root."""


class StorageConfigError(StorageError):
    """The storage settings are incomplete or invalid for the selected backend."""


class MultipartUploadNotFound(StorageError):
    """The multipart upload does not exist (never created, already completed, aborted or expired)."""

    def __init__(self, key: str, upload_id: str) -> None:
        super().__init__(f"multipart upload not found: {key} ({upload_id})")
        self.key = key
        self.upload_id = upload_id


# ── Metadata ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    last_modified: datetime          # tz-aware, UTC
    content_type: str | None = None


@dataclass(frozen=True)
class UploadedPart:
    """One part of a multipart upload as the backend itself reports it (never client-supplied)."""
    part_number: int
    size: int
    etag: str


@dataclass(frozen=True)
class MultipartUploadInfo:
    """An in-progress (neither completed nor aborted) multipart upload."""
    key: str
    upload_id: str
    initiated: datetime              # tz-aware, UTC


# ── Key rules ────────────────────────────────────────────────────────────────

def normalize_key(key: str) -> str:
    """
    Validate an object key and return it unchanged.

    The rules are the intersection of what S3 accepts and what maps safely onto
    a filesystem directory — so a key that is valid for one backend is valid,
    and means the same thing, for the other.
    """
    if not isinstance(key, str) or not key:
        raise InvalidObjectKey("object key must be a non-empty string")
    if len(key) > MAX_KEY_LENGTH:
        raise InvalidObjectKey(f"object key exceeds {MAX_KEY_LENGTH} characters")
    if "\\" in key:
        raise InvalidObjectKey(f"object key must use '/' separators, not '\\': {key!r}")
    if "\x00" in key:
        raise InvalidObjectKey("object key must not contain NUL")
    for segment in key.split("/"):
        if segment == "":
            raise InvalidObjectKey(f"object key must not have empty, leading or trailing segments: {key!r}")
        if segment in (".", ".."):
            raise InvalidObjectKey(f"object key must not contain '.' or '..' segments: {key!r}")
    return key


def normalize_prefix(prefix: str) -> str:
    """
    Validate a key prefix. "" means "everything"; otherwise it is any leading
    part of a valid key (a trailing "/" is allowed and preserved).
    """
    if prefix == "":
        return ""
    normalize_key(prefix.rstrip("/") if prefix.endswith("/") else prefix)
    return prefix


# ── Contract ─────────────────────────────────────────────────────────────────

@runtime_checkable
class ObjectStorage(Protocol):
    """Protocol every storage backend implements. See module docstring for semantics."""

    backend: ClassVar[str]   # "filesystem" | "minio" — for logs and /health only

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> ObjectInfo:
        """Store `data` under `key`, replacing any existing object."""
        ...

    def put_file(self, key: str, source: Path, *, content_type: str | None = None) -> ObjectInfo:
        """
        Store the local file at `source` under `key` (streamed, never fully in
        RAM), replacing any existing object. `source` is left untouched.
        Raises FileNotFoundError if `source` does not exist.
        """
        ...

    def get_bytes(self, key: str) -> bytes:
        """Return the whole object. Raises ObjectNotFound."""
        ...

    def get_file(self, key: str, dest: Path) -> Path:
        """
        Stream the object into the local file `dest` (parent directories are
        created; an existing file is replaced). Returns `dest`.
        Raises ObjectNotFound; a partially written `dest` is removed on error.
        """
        ...

    def open_read(self, key: str) -> IO[bytes]:
        """Return a binary, read-only, file-like stream. Caller closes it. Raises ObjectNotFound."""
        ...

    def exists(self, key: str) -> bool:
        ...

    def delete(self, key: str) -> bool:
        """Remove the object. True if it existed, False if it was already absent."""
        ...

    def stat(self, key: str) -> ObjectInfo:
        """Metadata for one object. Raises ObjectNotFound."""
        ...

    def list_objects(self, prefix: str = "") -> Iterator[ObjectInfo]:
        """Every object whose key starts with `prefix` ("" = all). Order is unspecified."""
        ...

    def ping(self) -> None:
        """Verify the backend is usable (reachable, writable). Raises StorageUnavailable."""
        ...


# ── Direct (browser -> backend) upload contract — Phase 3D ───────────────────
#
# Optional capability: a backend that can let a client outside the application
# write ONE object through URLs the server signs. Only object backends can do
# this (the filesystem backend does not implement it, and `isinstance(st,
# DirectUploadStorage)` is how callers find out). The unit of work is an S3
# multipart upload:
#
#     upload_id = create_multipart_upload(key)
#     url       = presign_upload_part(key, upload_id, n, expires_in=...)   # client PUTs part n
#     parts     = list_parts(key, upload_id)                                # what the backend has
#     info      = complete_multipart_upload(key, upload_id, parts)
#     abort_multipart_upload(key, upload_id)                                # discard the pieces
#
# A presigned URL is scoped to exactly one (key, upload_id, part_number),
# one HTTP method (PUT) and one lifetime; it never carries the secret key.
# `list_parts` is the server's own view of what was uploaded — callers verify
# against that, never against what a client reports.

@runtime_checkable
class DirectUploadStorage(Protocol):
    def supports_direct_upload(self) -> bool:
        """True when this instance can sign URLs a browser can reach (public endpoint configured)."""
        ...

    def create_multipart_upload(self, key: str, *, content_type: str | None = None) -> str:
        """Start a multipart upload for `key`; returns the backend's upload id."""
        ...

    def presign_upload_part(self, key: str, upload_id: str, part_number: int, *, expires_in: int) -> str:
        """URL a client may PUT part `part_number` to, valid for `expires_in` seconds."""
        ...

    def list_parts(self, key: str, upload_id: str) -> list[UploadedPart]:
        """Parts the backend has received so far, in part-number order. Raises MultipartUploadNotFound."""
        ...

    def complete_multipart_upload(self, key: str, upload_id: str, parts: list[UploadedPart]) -> ObjectInfo:
        """Assemble `parts` into the object `key`. Raises MultipartUploadNotFound."""
        ...

    def abort_multipart_upload(self, key: str, upload_id: str) -> bool:
        """Discard an in-progress upload. True if it existed, False if already gone."""
        ...

    def list_multipart_uploads(self, prefix: str = "") -> Iterator[MultipartUploadInfo]:
        """
        In-progress multipart uploads whose key starts with `prefix`. NOTE:
        MinIO only answers for an exact object key (no prefix navigation), so
        callers must not rely on this to enumerate a whole area — see
        services/direct_upload.open_uploads() for the application's own index.
        """
        ...
