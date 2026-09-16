"""
LocalFilesystemStorage — the storage contract over a directory tree.

One instance wraps one storage area (extracted_rows, batch_uploads, avatars).
A key maps to <root>/<key> exactly, so the physical layout the application
already uses is preserved byte-for-byte:

    LocalFilesystemStorage(cfg.rows_storage_path).put_bytes("abc.json", ...)
    ->  storage/extracted_rows/abc.json

Writes are plain overwrites (Path.write_bytes / shutil.copyfile), matching the
existing call sites — no temp-and-rename, no locking. Parent directories are
created on demand for writes; nothing is created at construction time.
"""
from __future__ import annotations

import mimetypes
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, ClassVar, Iterator

from spir_dynamic.services.object_storage.base import (
    ObjectInfo,
    ObjectNotFound,
    StorageError,
    StorageUnavailable,
    normalize_key,
    normalize_prefix,
)

_PROBE_NAME = ".health_probe"   # same probe file /api/health has always used


class LocalFilesystemStorage:
    backend: ClassVar[str] = "filesystem"

    def __init__(self, root: str | Path) -> None:
        # Kept exactly as given (absolute or CWD-relative) so this resolves the
        # same way the Path(cfg.xxx_dir) call sites always have.
        self.root = Path(root)

    def __repr__(self) -> str:
        return f"LocalFilesystemStorage(root={str(self.root)!r})"

    # ── backend-specific extra (not part of the contract) ────────────────────

    def path_for(self, key: str) -> Path:
        """Physical path of `key`. Only for call sites that must hand a real file to openpyxl etc."""
        return self.root / normalize_key(key)

    # ── contract ─────────────────────────────────────────────────────────────

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> ObjectInfo:
        path = self.path_for(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as exc:
            raise StorageError(f"write failed for {key}: {exc}") from exc
        return self.stat(key)

    def put_file(self, key: str, source: Path, *, content_type: str | None = None) -> ObjectInfo:
        source = Path(source)
        if not source.is_file():
            raise FileNotFoundError(f"source file not found: {source}")
        path = self.path_for(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, path)
        except OSError as exc:
            raise StorageError(f"copy failed for {key}: {exc}") from exc
        return self.stat(key)

    def get_bytes(self, key: str) -> bytes:
        path = self.path_for(key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise ObjectNotFound(key) from None
        except OSError as exc:
            raise StorageError(f"read failed for {key}: {exc}") from exc

    def get_file(self, key: str, dest: Path) -> Path:
        src = self.path_for(key)
        if not src.is_file():
            raise ObjectNotFound(key)
        dest = Path(dest)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        except OSError as exc:
            dest.unlink(missing_ok=True)
            raise StorageError(f"download failed for {key}: {exc}") from exc
        return dest

    def open_read(self, key: str) -> IO[bytes]:
        path = self.path_for(key)
        try:
            return path.open("rb")
        except FileNotFoundError:
            raise ObjectNotFound(key) from None
        except OSError as exc:
            raise StorageError(f"open failed for {key}: {exc}") from exc

    def exists(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def delete(self, key: str) -> bool:
        path = self.path_for(key)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StorageError(f"delete failed for {key}: {exc}") from exc
        return True

    def stat(self, key: str) -> ObjectInfo:
        path = self.path_for(key)
        try:
            st = path.stat()
        except FileNotFoundError:
            raise ObjectNotFound(key) from None
        except OSError as exc:
            raise StorageError(f"stat failed for {key}: {exc}") from exc
        if not path.is_file():
            raise ObjectNotFound(key)
        return ObjectInfo(
            key=key,
            size=st.st_size,
            last_modified=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
            content_type=mimetypes.guess_type(key)[0],
        )

    def list_objects(self, prefix: str = "") -> Iterator[ObjectInfo]:
        prefix = normalize_prefix(prefix)
        if not self.root.is_dir():
            return
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            key = path.relative_to(self.root).as_posix()
            if key.startswith(prefix):
                try:
                    yield self.stat(key)
                except ObjectNotFound:
                    continue   # raced with a delete

    def ping(self) -> None:
        # Like head_bucket for S3: a missing root is "unavailable", not auto-created.
        probe = self.root / _PROBE_NAME
        try:
            probe.touch()
            probe.unlink(missing_ok=True)
        except OSError as exc:
            raise StorageUnavailable(str(exc)) from exc
