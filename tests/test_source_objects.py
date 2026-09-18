"""
Phase 3C — source uploads through object storage.

Layers:

  Unit (always run)
    - object key: deterministic, collision-safe, host-path free
    - Settings / factory: UPLOAD_STORAGE_BACKEND overrides BATCH_UPLOADS only
    - store_source_upload / discard_source_object / staged_source / sweep
      against a LocalFilesystemStorage (in place) and an in-memory
      non-filesystem backend (download to scratch temp file)
    - process_file_task: pipeline receives the temp file, temp file removed,
      source object discarded after success, missing object -> controlled
      error without retry, unavailable backend -> retry path, extraction
      failure keeps the source object for the retry
    - lifecycle cleanup: stale source objects swept through the contract

  API (needs starlette TestClient / httpx — skipped on the host venv)
    - /api/extract: > threshold -> 202, object stored, task gets the key;
      <= threshold -> 200 sync, nothing stored
    - batch register/upload: object stored + task gets the key, giant
      routing unchanged, storage failure -> 503 + slot error, broker failure
      -> object discarded + slot error, Celery-disabled fallback

  MinIO integration (skipped when the Compose MinIO is not reachable)
    - real store / stage / overwrite / discard under a test-only prefix

  Docker end-to-end (skipped unless the Compose stack is up on :8000)
    - small file stays synchronous (HTTP 200)
    - 223 MB workbook: 202 / queue=heavy -> worker-heavy -> MinIO object ->
      download -> extraction -> result / history / download; temp file and
      source object gone afterwards
    - batch normal + heavy through MinIO; thresholds unchanged

Run:   pytest tests/test_source_objects.py -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import IO, ClassVar, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spir_dynamic.app.config import Settings
from spir_dynamic.services.object_storage import (
    InvalidObjectKey,
    LocalFilesystemStorage,
    MinioObjectStorage,
    ObjectInfo,
    ObjectNotFound,
    ObjectStorage,
    StorageArea,
    StorageUnavailable,
    area_backend,
    build_object_storage,
    normalize_key,
    reset_object_storage,
)
from spir_dynamic.services import source_objects as so
from spir_dynamic.services.source_objects import (
    SCRATCH_PREFIX,
    discard_source_object,
    source_object_key,
    staged_source,
    store_source_upload,
    sweep_stale_scratch,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_PREFIX_ROOT = "_tests/source_objects/"
MB = 1024 * 1024


# ── Test doubles ─────────────────────────────────────────────────────────────

class MemoryObjectStorage:
    """A non-filesystem ObjectStorage (no path_for) so the download path is exercised without MinIO."""

    backend: ClassVar[str] = "memory"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.mtimes: dict[str, datetime] = {}
        self.get_file_calls = 0
        self.fail_with: Exception | None = None   # raised by every operation when set

    def _check(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def _info(self, key: str) -> ObjectInfo:
        return ObjectInfo(key=key, size=len(self.objects[key]), last_modified=self.mtimes[key])

    def put_bytes(self, key, data, *, content_type=None):
        self._check()
        normalize_key(key)
        self.objects[key] = bytes(data)
        self.mtimes[key] = datetime.now(timezone.utc)
        return self._info(key)

    def put_file(self, key, source, *, content_type=None):
        self._check()
        source = Path(source)
        if not source.is_file():
            raise FileNotFoundError(source)
        return self.put_bytes(key, source.read_bytes())

    def get_bytes(self, key):
        self._check()
        if key not in self.objects:
            raise ObjectNotFound(key)
        return self.objects[key]

    def get_file(self, key, dest):
        self._check()
        self.get_file_calls += 1
        if key not in self.objects:
            raise ObjectNotFound(key)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.objects[key])
        return dest

    def open_read(self, key) -> IO[bytes]:
        return io.BytesIO(self.get_bytes(key))

    def exists(self, key):
        self._check()
        return key in self.objects

    def delete(self, key):
        self._check()
        self.mtimes.pop(key, None)
        return self.objects.pop(key, None) is not None

    def stat(self, key):
        self._check()
        if key not in self.objects:
            raise ObjectNotFound(key)
        return self._info(key)

    def list_objects(self, prefix="") -> Iterator[ObjectInfo]:
        self._check()
        for key in list(self.objects):
            if key.startswith(prefix):
                yield self._info(key)

    def ping(self):
        self._check()


_STORAGE_ENV = (
    "STORAGE_BACKEND", "UPLOAD_STORAGE_BACKEND", "WORKER_SCRATCH_DIR", "AVATAR_DIR",
    "ROWS_STORAGE_PATH", "BATCH_UPLOAD_DIR",
    "MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_BUCKET", "MINIO_SECURE",
)


@pytest.fixture(autouse=True)
def _isolate_backends(monkeypatch):
    """Unit tests must see neither the Compose env (UPLOAD_STORAGE_BACKEND=minio in-container) nor a cached backend."""
    for k in _STORAGE_ENV:
        monkeypatch.delenv(k, raising=False)
    reset_object_storage()
    yield
    reset_object_storage()


def _settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


# ── Unit: object key ─────────────────────────────────────────────────────────

class TestObjectKey:
    def test_key_layout(self):
        assert source_object_key("9f1c", 0, "VEN-4460.xlsm") == "9f1c_000_VEN-4460.xlsm"
        assert source_object_key("9f1c", 12, "a b.xlsx") == "9f1c_012_a b.xlsx"

    def test_deterministic(self):
        assert source_object_key("job", 1, "x.xlsm") == source_object_key("job", 1, "x.xlsm")

    def test_slots_and_jobs_never_collide(self):
        keys = {source_object_key(j, i, "same.xlsx") for j in ("job-a", "job-b") for i in range(3)}
        assert len(keys) == 6

    @pytest.mark.parametrize("name", [
        "../../etc/passwd", "C:\\Users\\x\\file.xlsm", "/abs/path/file.xlsx", "dir/sub/f.xlsx", "..", ".",
    ])
    def test_no_host_path_survives(self, name):
        key = source_object_key("job", 0, name)
        assert "/" not in key and "\\" not in key and key.startswith("job_000_")
        normalize_key(key)                       # valid for every backend (single segment)

    def test_extension_preserved_for_sanitizer(self):
        assert source_object_key("j", 0, "x.XLSM").endswith(".XLSM")

    def test_empty_or_odd_names_still_valid(self):
        assert source_object_key("j", 0, "") == "j_000_upload"
        assert normalize_key(source_object_key("j", 0, "\x00\n"))

    def test_long_names_truncated(self):
        key = source_object_key("j", 0, "a" * 500 + ".xlsx")
        assert len(key) <= len("j_000_") + 80
        normalize_key(key)

    def test_filesystem_backend_maps_key_to_pre_3c_path(self, tmp_path):
        # <batch_upload_dir>/<job>_<idx>_<safe> — exactly the file the old code wrote
        st = LocalFilesystemStorage(tmp_path)
        assert st.path_for(source_object_key("job", 2, "f.xlsx")) == tmp_path / "job_002_f.xlsx"


# ── Unit: configuration ──────────────────────────────────────────────────────

class TestUploadBackendSetting:
    def test_default_inherits_storage_backend(self):
        s = _settings()
        assert s.upload_storage_backend == ""
        assert area_backend(StorageArea.BATCH_UPLOADS, s) == "filesystem"
        assert isinstance(build_object_storage(StorageArea.BATCH_UPLOADS, s), LocalFilesystemStorage)

    def test_override_applies_to_batch_uploads_only(self):
        s = _settings(
            upload_storage_backend="minio",
            minio_endpoint="http://minio:9000", minio_access_key="k", minio_secret_key="s",
        )
        assert area_backend(StorageArea.BATCH_UPLOADS, s) == "minio"
        assert area_backend(StorageArea.EXTRACTED_ROWS, s) == "filesystem"
        assert area_backend(StorageArea.AVATARS, s) == "filesystem"
        up = build_object_storage(StorageArea.BATCH_UPLOADS, s)
        assert isinstance(up, MinioObjectStorage) and up.prefix == "batch_uploads/"
        assert isinstance(build_object_storage(StorageArea.EXTRACTED_ROWS, s), LocalFilesystemStorage)
        assert isinstance(build_object_storage(StorageArea.AVATARS, s), LocalFilesystemStorage)

    def test_explicit_filesystem_override_wins_over_global_minio(self):
        s = _settings(
            storage_backend="minio", upload_storage_backend="filesystem",
            minio_endpoint="http://minio:9000", minio_access_key="k", minio_secret_key="s",
        )
        assert isinstance(build_object_storage(StorageArea.BATCH_UPLOADS, s), LocalFilesystemStorage)
        assert isinstance(build_object_storage(StorageArea.AVATARS, s), MinioObjectStorage)

    def test_env_and_validation(self, monkeypatch):
        monkeypatch.setenv("UPLOAD_STORAGE_BACKEND", "MinIO")
        assert _settings().upload_storage_backend == "minio"
        monkeypatch.setenv("UPLOAD_STORAGE_BACKEND", "s3")
        with pytest.raises(Exception):
            _settings()

    def test_queue_thresholds_unchanged(self):
        s = _settings()
        assert s.large_file_threshold_mb == 100
        assert s.giant_file_threshold_mb == 500
        assert s.absolute_max_file_size_mb == 1500

    def test_scratch_dir_default_is_system_temp(self):
        import tempfile
        assert so.scratch_dir("") == Path(tempfile.gettempdir())
        assert so.scratch_dir("/var/tmp/spir") == Path("/var/tmp/spir")


# ── Unit: API-side store / discard ──────────────────────────────────────────

class TestStoreSourceUpload:
    def test_stores_and_consumes_temp(self, tmp_path):
        st = MemoryObjectStorage()
        tmp = tmp_path / "spir_upload_x.xlsm"
        tmp.write_bytes(b"data")
        info = store_source_upload(tmp, "j_000_f.xlsm", storage=st)
        assert info.size == 4 and st.objects["j_000_f.xlsm"] == b"data"
        assert not tmp.exists()

    def test_temp_removed_even_when_put_fails(self, tmp_path):
        st = MemoryObjectStorage()
        st.fail_with = StorageUnavailable("minio down")
        tmp = tmp_path / "spir_upload_y.xlsm"
        tmp.write_bytes(b"data")
        with pytest.raises(StorageUnavailable):
            store_source_upload(tmp, "j_000_f.xlsm", storage=st)
        assert not tmp.exists() and st.objects == {}

    def test_retry_of_same_slot_overwrites_not_duplicates(self, tmp_path):
        st = MemoryObjectStorage()
        for payload in (b"first", b"second"):
            tmp = tmp_path / "t.xlsm"
            tmp.write_bytes(payload)
            store_source_upload(tmp, source_object_key("job", 0, "f.xlsm"), storage=st)
        assert list(st.objects) == ["job_000_f.xlsm"]
        assert st.objects["job_000_f.xlsm"] == b"second"

    def test_uses_batch_uploads_area_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BATCH_UPLOAD_DIR", str(tmp_path / "uploads"))
        with patch("spir_dynamic.services.object_storage.factory.get_settings", return_value=_settings()):
            tmp = tmp_path / "t.xlsm"
            tmp.write_bytes(b"z")
            store_source_upload(tmp, "j_000_t.xlsm")
        assert (tmp_path / "uploads" / "j_000_t.xlsm").read_bytes() == b"z"


class TestDiscardSourceObject:
    def test_removes_and_reports(self):
        st = MemoryObjectStorage()
        st.put_bytes("k", b"1")
        assert discard_source_object("k", storage=st) is True
        assert discard_source_object("k", storage=st) is False

    def test_never_raises(self):
        st = MemoryObjectStorage()
        st.fail_with = StorageUnavailable("down")
        assert discard_source_object("k", storage=st) is False
        assert discard_source_object("bad\\key", storage=MemoryObjectStorage()) is False


# ── Unit: worker-side staging ────────────────────────────────────────────────

class TestStagedSource:
    def test_filesystem_backend_processes_in_place(self, tmp_path):
        st = LocalFilesystemStorage(tmp_path / "uploads")
        st.put_bytes("j_000_f.xlsx", b"x")
        scratch = tmp_path / "scratch"
        with staged_source("j_000_f.xlsx", storage=st, scratch=scratch) as p:
            assert p == tmp_path / "uploads" / "j_000_f.xlsx"
        assert not scratch.exists()                        # nothing downloaded
        assert st.exists("j_000_f.xlsx")                   # source untouched

    def test_filesystem_backend_missing_raises(self, tmp_path):
        st = LocalFilesystemStorage(tmp_path)
        with pytest.raises(ObjectNotFound):
            with staged_source("nope.xlsx", storage=st):
                pass

    def test_other_backend_downloads_to_unique_scratch_temp(self, tmp_path):
        st = MemoryObjectStorage()
        st.put_bytes("job_000_big.XLSM", b"workbook-bytes")
        scratch = tmp_path / "scratch"
        with staged_source("job_000_big.XLSM", storage=st, scratch=scratch) as p:
            assert p.parent == scratch
            assert p.name.startswith(SCRATCH_PREFIX) and "job_000_big" in p.name
            assert p.suffix == ".xlsm"                     # sanitizer keys off the extension
            assert p.read_bytes() == b"workbook-bytes"
            first = p
        assert not first.exists()                          # removed on exit
        assert st.objects["job_000_big.XLSM"] == b"workbook-bytes"   # durable object untouched
        with staged_source("job_000_big.XLSM", storage=st, scratch=scratch) as p2:
            assert p2 != first                             # never reused

    def test_temp_removed_when_processing_raises(self, tmp_path):
        st = MemoryObjectStorage()
        st.put_bytes("k.xlsx", b"1")
        with pytest.raises(RuntimeError):
            with staged_source("k.xlsx", storage=st, scratch=tmp_path) as p:
                assert p.exists()
                raise RuntimeError("extraction blew up")
        assert list(tmp_path.iterdir()) == []

    def test_missing_object_is_object_not_found_and_leaves_nothing(self, tmp_path):
        st = MemoryObjectStorage()
        with pytest.raises(ObjectNotFound):
            with staged_source("missing.xlsx", storage=st, scratch=tmp_path):
                pass
        assert list(tmp_path.iterdir()) == []

    def test_unavailable_backend_is_storage_unavailable_and_leaves_nothing(self, tmp_path):
        st = MemoryObjectStorage()
        st.fail_with = StorageUnavailable("minio down")
        with pytest.raises(StorageUnavailable):
            with staged_source("k.xlsx", storage=st, scratch=tmp_path):
                pass
        assert list(tmp_path.iterdir()) == []

    def test_concurrent_stagings_of_same_key_do_not_share_a_file(self, tmp_path):
        st = MemoryObjectStorage()
        st.put_bytes("k.xlsx", b"1")
        with staged_source("k.xlsx", storage=st, scratch=tmp_path) as a, \
             staged_source("k.xlsx", storage=st, scratch=tmp_path) as b:
            assert a != b and a.exists() and b.exists()
        assert list(tmp_path.iterdir()) == []


class TestSweepStaleScratch:
    def test_only_old_worker_files_removed(self, tmp_path):
        old_src = tmp_path / f"{SCRATCH_PREFIX}old.xlsm"
        old_san = tmp_path / "san_old.xlsm"
        fresh = tmp_path / f"{SCRATCH_PREFIX}fresh.xlsm"
        other = tmp_path / "unrelated_old.bin"
        for p in (old_src, old_san, fresh, other):
            p.write_bytes(b"x")
        stale = time.time() - so.SCRATCH_STALE_SECONDS - 60
        for p in (old_src, old_san, other):
            os.utime(p, (stale, stale))
        assert sweep_stale_scratch(tmp_path) == 2
        assert not old_src.exists() and not old_san.exists()
        assert fresh.exists() and other.exists()

    def test_missing_dir_is_noop(self, tmp_path):
        assert sweep_stale_scratch(tmp_path / "nope") == 0

    @pytest.mark.skipif(not Path("/proc").is_dir(), reason="pid liveness uses /proc (Linux worker containers)")
    def test_dead_owner_files_removed_immediately_live_owner_kept(self, tmp_path):
        import subprocess as sp, sys
        dead = sp.Popen([sys.executable, "-c", "pass"]); dead.wait()          # pid that no longer exists
        orphan = tmp_path / f"{SCRATCH_PREFIX}p{dead.pid}_job_000_x.xlsm"
        mine = tmp_path / f"{SCRATCH_PREFIX}p{os.getpid()}_job_001_y.xlsm"
        orphan.write_bytes(b"1"); mine.write_bytes(b"1")                      # both brand new
        assert sweep_stale_scratch(tmp_path) == 1
        assert not orphan.exists() and mine.exists()


# ── Unit: the Celery task on a non-filesystem backend ───────────────────────

_P = SimpleNamespace(
    job_store="spir_dynamic.services.job_store.get_job_store",
    file_result="spir_dynamic.services.job_store.FileResult",
    settings="spir_dynamic.app.config.get_settings",
    src_storage="spir_dynamic.services.source_objects.get_source_storage",
    pipeline="spir_dynamic.app.pipeline.run_pipeline",
    sanitizer="spir_dynamic.extraction.sanitizer.sanitize_workbook",
    audit="spir_dynamic.services.audit_service.log_extraction_worker",
    storage="spir_dynamic.services.storage.get_storage",
    redis="redis.from_url",
    metrics=[
        "spir_dynamic.monitoring.metrics.DELIVERY_CAP_HITS",
        "spir_dynamic.monitoring.metrics.SANITIZER_RUNS",
        "spir_dynamic.monitoring.metrics.SANITIZER_SAVINGS_MB",
        "spir_dynamic.monitoring.metrics.SANITIZER_REDUCTION_PCT",
        "spir_dynamic.monitoring.metrics.SANITIZER_DURATION",
    ],
)

_RESULT = {
    "status": "done", "file_id": "fid-1", "filename": "big_Extraction.xlsx", "spir_no": "S-1",
    "total_rows": 7, "total_tags": 3, "preview_cols": [], "preview_rows": [], "format": "A",
    "equipment": "", "manufacturer": "", "supplier": "", "spir_type": None, "eqpt_qty": 0,
    "spare_items": 0, "annexure_count": 0, "dup1_count": 0, "sap_count": 0,
}
_NOOP_SAN = SimpleNamespace(
    sanitized_path=None, used_fallback=False, skip_reason="skipped",
    original_size_mb=0.1, sanitized_size_mb=0.1, reduction_pct=0, duration_s=0.01,
)


@contextlib.contextmanager
def worker_ctx(tmp_path: Path, storage: ObjectStorage, *, pipeline=None, sanitizer=None, retries: int = 0):
    """process_file_task with everything but the storage bridge mocked; yields (task, store, pipeline_mock)."""
    from spir_dynamic.tasks.extraction_tasks import process_file_task

    store = MagicMock()
    settings = SimpleNamespace(
        redis_url="redis://localhost:6379/0",
        rows_storage_path=str(tmp_path / "rows"),
        worker_scratch_dir=str(tmp_path / "scratch"),
    )
    redis_stub = MagicMock()
    redis_stub.incr.return_value = 1
    pipeline_mock = pipeline or MagicMock(return_value=dict(_RESULT))
    patches = [
        patch(_P.job_store, return_value=store),
        patch(_P.file_result, side_effect=lambda **kw: SimpleNamespace(**kw)),
        patch(_P.settings, return_value=settings),
        patch(_P.src_storage, return_value=storage),
        patch(_P.redis, return_value=redis_stub),
        patch(_P.pipeline, pipeline_mock),
        patch(_P.sanitizer, sanitizer or MagicMock(return_value=_NOOP_SAN)),
        patch(_P.audit), patch(_P.storage),
        *(patch(m) for m in _P.metrics),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        process_file_task.push_request(id="t-1", retries=retries)
        try:
            yield process_file_task, store, pipeline_mock
        finally:
            process_file_task.pop_request()


def _slot_updates(store) -> list:
    return [c.args[2] for c in store.update_result.call_args_list]


class TestWorkerTask:
    KEY = "job-1_000_big.xlsm"

    def test_pipeline_gets_temp_file_then_cleanup(self, tmp_path):
        st = MemoryObjectStorage()
        st.put_bytes(self.KEY, b"wb")
        seen: dict = {}

        def fake_pipeline(path, filename):
            seen["path"] = Path(path)
            seen["existed"] = Path(path).exists()
            seen["content"] = Path(path).read_bytes()
            return dict(_RESULT)

        san = MagicMock(return_value=_NOOP_SAN)
        with worker_ctx(tmp_path, st, pipeline=MagicMock(side_effect=fake_pipeline), sanitizer=san) as (task, store, _):
            out = task.run(job_id="job-1", file_idx=0, source_key=self.KEY, filename="big.xlsm", user_id="u")

        assert out["status"] == "ok" and out["file_id"] == "fid-1" and out["total_rows"] == 7
        # the existing pipeline received a real local file in the scratch dir
        assert seen["existed"] and seen["content"] == b"wb"
        assert seen["path"].parent == tmp_path / "scratch"
        assert seen["path"].name.startswith(SCRATCH_PREFIX) and seen["path"].suffix == ".xlsm"
        # the sanitizer saw the same local file
        assert san.call_args.args[0] == seen["path"]
        # temp file removed, source object discarded, slot ok
        assert not seen["path"].exists() and list((tmp_path / "scratch").iterdir()) == []
        assert self.KEY not in st.objects
        assert [u.status for u in _slot_updates(store)] == ["running", "ok"]
        assert _slot_updates(store)[-1].file_id == "fid-1"

    def test_missing_object_is_controlled_failure_without_retry(self, tmp_path):
        st = MemoryObjectStorage()
        with worker_ctx(tmp_path, st) as (task, store, pipeline):
            with patch.object(task, "retry") as retry:
                out = task.run(job_id="job-1", file_idx=0, source_key=self.KEY, filename="big.xlsm")
        assert out["status"] == "error" and "not found" in out["error"]
        retry.assert_not_called()
        pipeline.assert_not_called()
        last = _slot_updates(store)[-1]
        assert last.status == "error" and self.KEY in last.error
        assert not (tmp_path / "scratch").exists() or list((tmp_path / "scratch").iterdir()) == []

    def test_unavailable_backend_goes_through_retry_and_final_error(self, tmp_path):
        from celery.exceptions import MaxRetriesExceededError, Retry
        st = MemoryObjectStorage()
        st.fail_with = StorageUnavailable("minio down")

        # attempt 1: retry requested, nothing marked as final
        with worker_ctx(tmp_path, st) as (task, store, pipeline):
            with patch.object(task, "retry", side_effect=Retry("retrying")) as retry:
                with pytest.raises(Retry):
                    task.run(job_id="job-1", file_idx=0, source_key=self.KEY, filename="big.xlsm")
        retry.assert_called_once()
        assert isinstance(retry.call_args.kwargs["exc"], StorageUnavailable)
        assert [u.status for u in _slot_updates(store)] == ["running"]
        pipeline.assert_not_called()

        # last attempt: retries exhausted -> controlled error on the slot
        with worker_ctx(tmp_path, st, retries=3) as (task, store, _):
            with patch.object(task, "retry", side_effect=MaxRetriesExceededError()):
                out = task.run(job_id="job-1", file_idx=0, source_key=self.KEY, filename="big.xlsm")
        assert out["status"] == "error" and "minio down" in out["error"]
        assert _slot_updates(store)[-1].status == "error"

    def test_extraction_failure_keeps_source_for_retry_and_removes_temp(self, tmp_path):
        from celery.exceptions import Retry
        st = MemoryObjectStorage()
        st.put_bytes(self.KEY, b"wb")
        with worker_ctx(tmp_path, st, pipeline=MagicMock(side_effect=ValueError("bad sheet"))) as (task, store, _):
            with patch.object(task, "retry", side_effect=Retry("retrying")) as retry:
                with pytest.raises(Retry):
                    task.run(job_id="job-1", file_idx=0, source_key=self.KEY, filename="big.xlsm")
        retry.assert_called_once()
        assert self.KEY in st.objects                                  # retry can re-download
        assert list((tmp_path / "scratch").iterdir()) == []            # temp gone
        assert _slot_updates(store)[-1].status == "running"            # not final yet

    def test_final_failure_discards_source(self, tmp_path):
        from celery.exceptions import MaxRetriesExceededError
        st = MemoryObjectStorage()
        st.put_bytes(self.KEY, b"wb")
        with worker_ctx(tmp_path, st, pipeline=MagicMock(side_effect=ValueError("bad sheet")), retries=3) as (task, store, _):
            with patch.object(task, "retry", side_effect=MaxRetriesExceededError()):
                out = task.run(job_id="job-1", file_idx=0, source_key=self.KEY, filename="big.xlsm")
        assert out["status"] == "error"
        assert self.KEY not in st.objects
        assert _slot_updates(store)[-1].status == "error"

    def test_retry_uses_same_object_no_duplicates(self, tmp_path):
        st = MemoryObjectStorage()
        st.put_bytes(self.KEY, b"wb")
        calls = {"n": 0}

        def flaky(path, filename):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("transient")
            return dict(_RESULT)

        from celery.exceptions import Retry
        with worker_ctx(tmp_path, st, pipeline=MagicMock(side_effect=flaky)) as (task, *_):
            with patch.object(task, "retry", side_effect=Retry("r")), pytest.raises(Retry):
                task.run(job_id="job-1", file_idx=0, source_key=self.KEY, filename="big.xlsm")
        with worker_ctx(tmp_path, st, pipeline=MagicMock(side_effect=flaky), retries=1) as (task, *_):
            out = task.run(job_id="job-1", file_idx=0, source_key=self.KEY, filename="big.xlsm")
        assert out["status"] == "ok"
        assert st.get_file_calls == 2 and st.objects == {}            # downloaded twice, one object, now gone

    def test_timeout_discards_source_and_temp(self, tmp_path):
        from celery.exceptions import SoftTimeLimitExceeded
        st = MemoryObjectStorage()
        st.put_bytes(self.KEY, b"wb")
        with worker_ctx(tmp_path, st, pipeline=MagicMock(side_effect=SoftTimeLimitExceeded())) as (task, store, _):
            out = task.run(job_id="job-1", file_idx=0, source_key=self.KEY, filename="big.xlsm")
        assert out["status"] == "error" and "timed out" in out["error"]
        assert self.KEY not in st.objects
        assert list((tmp_path / "scratch").iterdir()) == []

    def test_delivery_cap_keyed_on_source_key(self, tmp_path):
        st = MemoryObjectStorage()
        st.put_bytes(self.KEY, b"wb")
        with worker_ctx(tmp_path, st) as (task, *_):
            with patch(_P.redis) as redis_factory:
                r = redis_factory.return_value
                r.incr.return_value = 1
                task.run(job_id="job-1", file_idx=0, source_key=self.KEY, filename="big.xlsm")
        assert r.incr.call_args.args[0] == f"spir:dlv:{self.KEY}"

    def test_filesystem_backend_unchanged_in_place(self, tmp_path):
        """With the default backend the worker still opens the upload where the API put it."""
        uploads = tmp_path / "uploads"
        st = LocalFilesystemStorage(uploads)
        st.put_bytes(self.KEY, b"wb")
        seen = {}

        def fake_pipeline(path, filename):
            seen["path"] = Path(path)
            return dict(_RESULT)

        with worker_ctx(tmp_path, st, pipeline=MagicMock(side_effect=fake_pipeline)) as (task, *_):
            out = task.run(job_id="job-1", file_idx=0, source_key=self.KEY, filename="big.xlsm")
        assert out["status"] == "ok"
        assert seen["path"] == uploads / self.KEY
        assert not (uploads / self.KEY).exists()                        # deleted after success
        assert not (tmp_path / "scratch").exists()                      # no download happened


# ── Unit: lifecycle cleanup on an object backend ────────────────────────────

class TestCleanupSourceObjects:
    def test_stale_objects_swept_through_contract(self):
        from spir_dynamic.tasks.cleanup_tasks import _purge_stale_source_objects
        st = MemoryObjectStorage()
        now = datetime.now(timezone.utc)
        st.put_bytes("old_000_a.xlsx", b"1" * 10)
        st.mtimes["old_000_a.xlsx"] = now - timedelta(hours=30)
        st.put_bytes("mid_000_b.xlsx", b"1")
        st.mtimes["mid_000_b.xlsx"] = now - timedelta(hours=5)
        st.put_bytes("new_000_c.xlsx", b"1")            # within the 1h guard

        with patch("spir_dynamic.monitoring.metrics.CLEANUP_DELETED_FILES"):
            dry = _purge_stale_source_objects(st, stale_hours=24, dry_run=True)
            assert dry["deleted"] == 0 and len(st.objects) == 3
            live = _purge_stale_source_objects(st, stale_hours=24, dry_run=False)
        assert live["deleted"] == 1 and live["skipped_recent"] == 1 and live["backend"] == "memory"
        assert set(st.objects) == {"mid_000_b.xlsx", "new_000_c.xlsx"}

    def test_task_uses_object_sweep_only_for_non_filesystem_backend(self, tmp_path):
        from spir_dynamic.tasks import cleanup_tasks as ct
        st = MemoryObjectStorage()
        with patch("spir_dynamic.services.object_storage.get_object_storage", return_value=st):
            assert ct._source_object_storage() is st
        with patch("spir_dynamic.services.object_storage.get_object_storage",
                   return_value=LocalFilesystemStorage(tmp_path)):
            assert ct._source_object_storage() is None
        with patch("spir_dynamic.services.object_storage.get_object_storage", side_effect=RuntimeError("cfg")):
            assert ct._source_object_storage() is None

    def test_metrics_measure_objects(self, tmp_path):
        from spir_dynamic.tasks.cleanup_tasks import _update_disk_metrics
        st = MemoryObjectStorage()
        st.put_bytes("a", b"1" * (2 * MB))
        with patch("spir_dynamic.monitoring.metrics.STORAGE_JSON_COUNT"), \
             patch("spir_dynamic.monitoring.metrics.STORAGE_JSON_SIZE_MB"), \
             patch("spir_dynamic.monitoring.metrics.STORAGE_UPLOAD_SIZE_MB") as gauge:
            out = _update_disk_metrics(tmp_path, tmp_path / "uploads", upload_storage=st)
        assert out["upload_size_mb"] == 2.0
        gauge.set.assert_called_with(2.0)


# ── API: FastAPI endpoints (TestClient needs httpx) ─────────────────────────

def _testclient_available() -> bool:
    try:
        from fastapi.testclient import TestClient  # noqa: F401
        import httpx  # noqa: F401
        return True
    except Exception:
        return False


@pytest.fixture
def api(tmp_path):
    if not _testclient_available():
        pytest.skip("starlette TestClient / httpx not installed in this environment")
    from fastapi.testclient import TestClient
    from spir_dynamic.app.auth import TokenData, get_current_user
    from spir_dynamic.app.config import get_settings
    from spir_dynamic.app.main import app

    storage = MemoryObjectStorage()
    cfg = get_settings().model_copy(update={
        "rows_storage_path": str(tmp_path / "rows"),
        "batch_upload_dir": str(tmp_path / "uploads"),
        "max_file_size_mb": 2048, "large_file_threshold_mb": 100, "giant_file_threshold_mb": 500,
        "celery_enabled": True,
    })
    app.dependency_overrides[get_current_user] = lambda: TokenData("tester", "user-1", "jti-1")
    with patch(_P.src_storage, return_value=storage):
        try:
            yield SimpleNamespace(client=TestClient(app), cfg=cfg, storage=storage, tmp=tmp_path)
        finally:
            app.dependency_overrides.clear()


def _no_temp_left() -> bool:
    import tempfile
    recent = time.time() - 30
    return not [p for p in Path(tempfile.gettempdir()).glob("spir_upload_*") if p.stat().st_mtime > recent]


class TestSingleFileEndpoint:
    def test_large_file_stored_as_object_and_key_passed_to_celery(self, api):
        cfg = api.cfg.model_copy(update={"large_file_threshold_mb": 0})
        task = MagicMock()
        store = MagicMock()
        with patch("spir_dynamic.app.routes.get_settings", return_value=cfg), \
             patch("spir_dynamic.app.routes.run_pipeline") as pipeline, \
             patch("spir_dynamic.tasks.extraction_tasks.process_file_task", task), \
             patch("spir_dynamic.monitoring.metrics.GIANT_FILES_ROUTED"), \
             patch("spir_dynamic.app.routes.get_job_store", return_value=store), \
             patch("spir_dynamic.app.routes._persist_job_to_db", new=AsyncMock()):
            res = api.client.post("/api/extract", files={"file": ("big.xlsm", b"x" * 10, "application/octet-stream")})

        assert res.status_code == 202
        body = res.json()
        assert body["status"] == "queued" and body["queue"] == "heavy"
        key = source_object_key(body["job_id"], 0, "big.xlsm")
        assert api.storage.objects == {key: b"x" * 10}              # durable source object
        args = task.apply_async.call_args.kwargs["args"]
        assert args == [body["job_id"], 0, key, "big.xlsm", "user-1"]  # object reference, not a path
        assert task.apply_async.call_args.kwargs["queue"] == "heavy"
        pipeline.assert_not_called()
        assert not (api.tmp / "uploads").exists()                    # nothing on the filesystem area
        assert _no_temp_left()

    def test_small_file_stays_sync_and_stores_nothing(self, api):
        with patch("spir_dynamic.app.routes.get_settings", return_value=api.cfg), \
             patch("spir_dynamic.app.routes.run_pipeline", return_value=dict(_RESULT)) as pipeline, \
             patch("spir_dynamic.app.routes._dispatch_celery") as dispatch:
            res = api.client.post("/api/extract", files={"file": ("small.xlsx", b"x" * 10, "application/octet-stream")})
        assert res.status_code == 200 and res.json()["file_id"] == "fid-1"
        pipeline.assert_called_once()
        dispatch.assert_not_called()
        assert api.storage.objects == {}

    def test_storage_unavailable_is_503_and_leaves_nothing(self, api):
        cfg = api.cfg.model_copy(update={"large_file_threshold_mb": 0})
        api.storage.fail_with = StorageUnavailable("minio down")
        with patch("spir_dynamic.app.routes.get_settings", return_value=cfg), \
             patch("spir_dynamic.app.routes.run_pipeline") as pipeline, \
             patch("spir_dynamic.app.routes._dispatch_celery") as dispatch:
            res = api.client.post("/api/extract", files={"file": ("big.xlsm", b"x" * 10, "application/octet-stream")})
        assert res.status_code == 503 and "minio down" in res.json()["detail"]
        pipeline.assert_not_called()
        dispatch.assert_not_called()
        assert api.storage.objects == {} and _no_temp_left()

    def test_dispatch_failure_after_store_discards_object(self, api):
        cfg = api.cfg.model_copy(update={"large_file_threshold_mb": 0})
        store = MagicMock()
        with patch("spir_dynamic.app.routes.get_settings", return_value=cfg), \
             patch("spir_dynamic.app.routes.run_pipeline"), \
             patch("spir_dynamic.app.routes._dispatch_celery", side_effect=RuntimeError("broker down")), \
             patch("spir_dynamic.app.routes.get_job_store", return_value=store):
            res = api.client.post("/api/extract", files={"file": ("big.xlsm", b"x" * 10, "application/octet-stream")})
        assert res.status_code == 503
        assert api.storage.objects == {}                              # stored then discarded
        _jid, idx, result = store.update_result.call_args.args
        assert idx == 0 and result.status == "error"

    def test_health_reports_upload_backend(self, api):
        cfg = api.cfg.model_copy(update={"upload_storage_backend": "minio"})
        with patch("spir_dynamic.app.routes.get_settings", return_value=cfg), \
             patch("spir_dynamic.app.routes.get_object_storage", return_value=MemoryObjectStorage()):
            body = api.client.get("/api/health").json()
        assert body["storage_backend"] == "filesystem" and body["upload_storage_backend"] == "minio"


class TestBatchEndpoints:
    def _register(self, api, names, store):
        with patch("spir_dynamic.app.batch_router.get_settings", return_value=api.cfg), \
             patch("spir_dynamic.app.batch_router.get_job_store", return_value=store), \
             patch("spir_dynamic.app.batch_router._persist_job_to_db", new=AsyncMock()):
            res = api.client.post("/api/batch/register", json={"filenames": names})
        assert res.status_code == 200
        return res.json()["job_id"]

    def _job_store(self, names=None):
        from spir_dynamic.services.job_store import JobStore
        return JobStore()

    def test_upload_stores_object_and_routes_normal(self, api):
        store = self._job_store(["a.xlsx"])
        job_id = self._register(api, ["a.xlsx"], store)
        task = MagicMock()
        with patch("spir_dynamic.app.batch_router.get_settings", return_value=api.cfg), \
             patch("spir_dynamic.app.batch_router.get_job_store", return_value=store), \
             patch("spir_dynamic.tasks.extraction_tasks.process_file_task", task), \
             patch("spir_dynamic.monitoring.metrics.GIANT_FILES_ROUTED"):
            res = api.client.post(f"/api/batch/{job_id}/upload", data={"file_idx": "0"},
                                  files={"file": ("a.xlsx", b"y" * 5, "application/octet-stream")})
        assert res.status_code == 200 and res.json()["status"] == "queued"
        key = source_object_key(job_id, 0, "a.xlsx")
        assert api.storage.objects == {key: b"y" * 5}
        assert task.apply_async.call_args.kwargs["args"] == [job_id, 0, key, "a.xlsx", "user-1"]
        assert task.apply_async.call_args.kwargs["queue"] == "normal"
        assert not (api.tmp / "uploads").exists()

    def test_heavy_and_giant_routing_unchanged(self, api):
        from spir_dynamic.app.batch_router import _GIANT_SOFT_LIMIT, _HEAVY_SOFT_LIMIT
        store = self._job_store(["h.xlsm", "g.xlsm"])
        job_id = self._register(api, ["h.xlsm", "g.xlsm"], store)
        task = MagicMock()
        # 10-byte uploads: thresholds shrunk so slot 0 routes heavy, slot 1 giant
        heavy_cfg = api.cfg.model_copy(update={"large_file_threshold_mb": 0})
        giant_cfg = api.cfg.model_copy(update={"large_file_threshold_mb": 0, "giant_file_threshold_mb": 0})
        for idx, cfg in ((0, heavy_cfg), (1, giant_cfg)):
            with patch("spir_dynamic.app.batch_router.get_settings", return_value=cfg), \
                 patch("spir_dynamic.app.batch_router.get_job_store", return_value=store), \
                 patch("spir_dynamic.tasks.extraction_tasks.process_file_task", task), \
                 patch("spir_dynamic.monitoring.metrics.GIANT_FILES_ROUTED"):
                res = api.client.post(f"/api/batch/{job_id}/upload", data={"file_idx": str(idx)},
                                      files={"file": ("f.xlsm", b"z" * 10, "application/octet-stream")})
            assert res.status_code == 200
        calls = task.apply_async.call_args_list
        assert [c.kwargs["queue"] for c in calls] == ["heavy", "giant"]
        assert calls[0].kwargs["soft_time_limit"] == _HEAVY_SOFT_LIMIT
        assert calls[1].kwargs["soft_time_limit"] == _GIANT_SOFT_LIMIT
        assert calls[1].kwargs["args"][2] == source_object_key(job_id, 1, "g.xlsm")
        assert len(api.storage.objects) == 2

    def test_storage_failure_marks_slot_error(self, api):
        store = self._job_store(["a.xlsx"])
        job_id = self._register(api, ["a.xlsx"], store)
        api.storage.fail_with = StorageUnavailable("minio down")
        with patch("spir_dynamic.app.batch_router.get_settings", return_value=api.cfg), \
             patch("spir_dynamic.app.batch_router.get_job_store", return_value=store), \
             patch("spir_dynamic.app.batch_router._dispatch_celery") as dispatch:
            res = api.client.post(f"/api/batch/{job_id}/upload", data={"file_idx": "0"},
                                  files={"file": ("a.xlsx", b"y", "application/octet-stream")})
        assert res.status_code == 503 and "minio down" in res.json()["detail"]
        dispatch.assert_not_called()
        slot = store.get(job_id).results[0]
        assert slot.status == "error" and "minio down" in slot.error   # poller does not stall
        assert _no_temp_left()

    def test_broker_failure_after_store_discards_object_and_marks_error(self, api):
        store = self._job_store(["a.xlsx"])
        job_id = self._register(api, ["a.xlsx"], store)
        task = MagicMock()
        task.apply_async.side_effect = RuntimeError("broker down")
        with patch("spir_dynamic.app.batch_router.get_settings", return_value=api.cfg), \
             patch("spir_dynamic.app.batch_router.get_job_store", return_value=store), \
             patch("spir_dynamic.tasks.extraction_tasks.process_file_task", task), \
             patch("spir_dynamic.monitoring.metrics.GIANT_FILES_ROUTED"):
            res = api.client.post(f"/api/batch/{job_id}/upload", data={"file_idx": "0"},
                                  files={"file": ("a.xlsx", b"y", "application/octet-stream")})
        assert res.status_code == 503
        assert api.storage.objects == {}
        assert store.get(job_id).results[0].status == "error"

    def test_batch_extract_partial_dispatch_failure(self, api):
        """/api/batch/extract: files after the failing slot are discarded and marked error."""
        store = self._job_store([])
        task = MagicMock()
        task.apply_async.side_effect = [None, RuntimeError("broker down")]
        with patch("spir_dynamic.app.batch_router.get_settings", return_value=api.cfg), \
             patch("spir_dynamic.app.batch_router.get_job_store", return_value=store), \
             patch("spir_dynamic.app.batch_router._persist_job_to_db", new=AsyncMock()), \
             patch("spir_dynamic.tasks.extraction_tasks.process_file_task", task), \
             patch("spir_dynamic.monitoring.metrics.GIANT_FILES_ROUTED"):
            res = api.client.post("/api/batch/extract", files=[
                ("files", ("a.xlsx", b"1", "application/octet-stream")),
                ("files", ("b.xlsx", b"2", "application/octet-stream")),
                ("files", ("c.xlsx", b"3", "application/octet-stream")),
            ])
        assert res.status_code == 200
        job_id = res.json()["job_id"]
        results = store.get(job_id).results
        assert [r.status for r in results] == ["pending", "error", "error"]
        assert set(api.storage.objects) == {source_object_key(job_id, 0, "a.xlsx")}   # queued file kept

    def test_celery_disabled_upload_uses_fallback_not_celery(self, api):
        cfg = api.cfg.model_copy(update={"celery_enabled": False})
        store = self._job_store()
        job_id = self._register(api, ["a.xlsx"], store)
        fallback = AsyncMock()
        with patch("spir_dynamic.app.batch_router.get_settings", return_value=cfg), \
             patch("spir_dynamic.app.batch_router.get_job_store", return_value=store), \
             patch("spir_dynamic.app.batch_router._process_batch_from_storage", fallback), \
             patch("spir_dynamic.app.batch_router._dispatch_celery") as dispatch:
            res = api.client.post(f"/api/batch/{job_id}/upload", data={"file_idx": "0"},
                                  files={"file": ("a.xlsx", b"AAA", "application/octet-stream")})
        assert res.status_code == 200
        dispatch.assert_not_called()
        key = source_object_key(job_id, 0, "a.xlsx")
        assert api.storage.objects == {key: b"AAA"}
        assert fallback.call_args.args == (job_id, [(key, "a.xlsx", 3)])


def test_fallback_processes_from_storage_and_discards(tmp_path):
    """celery_enabled=False path: the same staging bridge feeds run_pipeline, then the object goes."""
    import asyncio
    from spir_dynamic.app.batch_router import _process_batch_from_storage
    from spir_dynamic.services.job_store import JobStore

    storage = MemoryObjectStorage()
    storage.put_bytes("job_000_a.xlsx", b"AAA")
    storage.put_bytes("job_001_b.xlsx", b"BBB")
    store = JobStore()
    store.create("job", ["a.xlsx", "b.xlsx"])
    seen = {}

    def fake_pipeline(path, filename):
        seen[filename] = (Path(path).read_bytes(), Path(path).parent)
        if filename == "b.xlsx":
            raise ValueError("bad workbook")
        return dict(_RESULT)

    with patch(_P.src_storage, return_value=storage), \
         patch("spir_dynamic.app.batch_router.get_job_store", return_value=store), \
         patch("spir_dynamic.app.batch_router.run_pipeline", side_effect=fake_pipeline), \
         patch("spir_dynamic.services.source_objects.scratch_dir", return_value=tmp_path / "scratch"):
        asyncio.run(_process_batch_from_storage(
            "job", [("job_000_a.xlsx", "a.xlsx", 3), ("job_001_b.xlsx", "b.xlsx", 3)],
        ))

    assert seen["a.xlsx"][0] == b"AAA" and seen["a.xlsx"][1] == tmp_path / "scratch"
    results = store.get("job").results
    assert results[0].status == "ok" and results[1].status == "error" and "bad workbook" in results[1].error
    assert storage.objects == {}                                        # discarded after ok AND error
    assert list((tmp_path / "scratch").iterdir()) == []


# ── MinIO integration ────────────────────────────────────────────────────────

def _compose_config() -> dict | None:
    if shutil.which("docker") is None:
        return None
    try:
        r = subprocess.run(["docker", "compose", "config", "--format", "json"],
                           cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return json.loads(r.stdout) if r.returncode == 0 else None


def _minio_target() -> dict | None:
    env = {k: os.environ.get(k) for k in ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY")}
    if all(env.values()):
        target = {"endpoint": env["MINIO_ENDPOINT"], "access_key": env["MINIO_ACCESS_KEY"],
                  "secret_key": env["MINIO_SECRET_KEY"], "bucket": os.environ.get("MINIO_BUCKET", "spir-files")}
    else:
        cfg = _compose_config()
        if not cfg:
            return None
        minio_env = cfg["services"].get("minio", {}).get("environment", {})
        init_env = cfg["services"].get("minio-init", {}).get("environment", {})
        target = {"endpoint": "http://127.0.0.1:9000", "access_key": minio_env.get("MINIO_ROOT_USER", ""),
                  "secret_key": minio_env.get("MINIO_ROOT_PASSWORD", ""), "bucket": init_env.get("MINIO_BUCKET", "spir-files")}
    if not (target["access_key"] and target["secret_key"]):
        return None
    try:
        with urllib.request.urlopen(target["endpoint"].rstrip("/") + "/minio/health/live", timeout=3) as r:
            if r.status != 200:
                return None
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return target


_MINIO = _minio_target()
_COMPOSE = _compose_config() if _MINIO else None


@pytest.fixture
def minio_scratch():
    if _MINIO is None:
        pytest.skip("local MinIO (Compose stack) is not reachable")
    prefix = f"{TEST_PREFIX_ROOT}{uuid.uuid4()}/"
    st = MinioObjectStorage(**_MINIO, prefix=prefix)
    yield st
    for obj in list(st.list_objects()):
        st.delete(obj.key)
    assert list(st.list_objects()) == []


class TestMinioIntegration:
    def test_store_stage_discard_roundtrip(self, minio_scratch, tmp_path):
        st = minio_scratch
        key = source_object_key(str(uuid.uuid4()), 0, "roundtrip.xlsm")
        tmp = tmp_path / "spir_upload_rt.xlsm"
        payload = os.urandom(3 * MB)
        tmp.write_bytes(payload)

        info = store_source_upload(tmp, key, storage=st)
        assert info.size == len(payload) and not tmp.exists()
        assert st.exists(key)

        scratch = tmp_path / "scratch"
        with staged_source(key, storage=st, scratch=scratch) as p:
            assert p.parent == scratch and p.name.startswith(SCRATCH_PREFIX) and p.suffix == ".xlsm"
            assert p.read_bytes() == payload
        assert list(scratch.iterdir()) == []
        assert st.exists(key)                                          # staging never deletes the source

        assert discard_source_object(key, storage=st) is True
        assert not st.exists(key)
        with pytest.raises(ObjectNotFound):
            with staged_source(key, storage=st, scratch=scratch):
                pass

    def test_retry_overwrites_same_key(self, minio_scratch, tmp_path):
        st = minio_scratch
        key = source_object_key("job-r", 0, "f.xlsm")
        for payload in (b"first", b"second-longer"):
            tmp = tmp_path / "t.xlsm"
            tmp.write_bytes(payload)
            store_source_upload(tmp, key, storage=st)
        objs = list(st.list_objects())
        assert [o.key for o in objs] == [key] and objs[0].size == len(b"second-longer")

    def test_wrong_credentials_are_controlled(self, minio_scratch, tmp_path):
        bad = MinioObjectStorage(**dict(_MINIO, secret_key="definitely-wrong"), prefix=minio_scratch.prefix)
        tmp = tmp_path / "t.xlsm"
        tmp.write_bytes(b"1")
        with pytest.raises(StorageUnavailable):
            store_source_upload(tmp, "j_000_t.xlsm", storage=bad)
        assert not tmp.exists()
        with pytest.raises(StorageUnavailable):
            with staged_source("j_000_t.xlsm", storage=bad, scratch=tmp_path):
                pass
        assert not any(p.name.startswith(SCRATCH_PREFIX) for p in tmp_path.iterdir())


# ── Docker end-to-end ────────────────────────────────────────────────────────

API = os.environ.get("SPIR_E2E_API", "http://localhost:8000")
WORKBOOK_223 = REPO_ROOT / "templates" / "inputs" / "VEN-4460-DGEN-5-43-0001-1.xlsm"
WORKBOOK_SMALL = REPO_ROOT / "templates" / "inputs" / "15.VEN-4142-RLCSF3-4-43-0500-A.xlsx"


def _stack_running() -> bool:
    if _MINIO is None:
        return False
    try:
        r = subprocess.run(["docker", "compose", "ps", "--format", "json", "--status", "running"],
                           cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        names = {json.loads(line)["Service"] for line in r.stdout.splitlines() if line.strip()}
    except Exception:
        return False
    if not {"api", "minio", "redis", "worker-normal", "worker-heavy"} <= names:
        return False
    try:
        with urllib.request.urlopen(f"{API}/api/health", timeout=5) as resp:
            return json.loads(resp.read()).get("upload_storage_backend") == "minio"
    except Exception:
        return False


def _http(method: str, path: str, *, token: str | None = None, body: bytes | None = None,
          content_type: str | None = None, timeout: float = 600) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(f"{API}{path}", data=body, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def _multipart(fields: dict[str, str], file_field: str, filename: str, path: Path) -> tuple[bytes, str]:
    boundary = f"----spir{uuid.uuid4().hex}"
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
              f"Content-Type: application/octet-stream\r\n\r\n".encode())
    buf.write(path.read_bytes())
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def _compose_exec(service: str, code: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "compose", "exec", "-T", service, "python", "-c", code],
                          cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout)


def _worker_logs(service: str, since: str) -> str:
    r = subprocess.run(["docker", "compose", "logs", "--no-log-prefix", "--since", since, service],
                       cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    return r.stdout + r.stderr


_USER_SQL_CREATE = """
import asyncio, sys
from sqlalchemy import text
from spir_dynamic.app.config import get_settings
from spir_dynamic.db.database import setup_engine, get_session_factory
from spir_dynamic.app.auth import _hash_password
setup_engine(get_settings().database_url)
async def main():
    async with get_session_factory()() as db:
        await db.execute(text("INSERT INTO users (id, username, password_hash, role, is_active, created_at) "
                              "VALUES (:id, :u, :p, 'user', true, now())"),
                         {"id": sys.argv[1], "u": sys.argv[2], "p": _hash_password(sys.argv[3])})
        await db.commit()
asyncio.run(main())
print("ok")
"""

_USER_SQL_DELETE = """
import asyncio, sys
from sqlalchemy import text
from spir_dynamic.app.config import get_settings
from spir_dynamic.db.database import setup_engine, get_session_factory
setup_engine(get_settings().database_url)
async def main():
    async with get_session_factory()() as db:
        await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": sys.argv[1]})
        await db.commit()
asyncio.run(main())
print("ok")
"""


@pytest.fixture(scope="module")
def e2e_user():
    """A throwaway login in the Docker DB; every row it creates cascades away with it."""
    if not _stack_running():
        pytest.skip("SPIR docker compose stack with UPLOAD_STORAGE_BACKEND=minio is not running")
    user_id, username, password = str(uuid.uuid4()), f"e2e_3c_{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    r = subprocess.run(["docker", "compose", "exec", "-T", "api", "python", "-c", _USER_SQL_CREATE,
                        user_id, username, password], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr
    form = f"username={username}&password={password}".encode()
    status, _, body = _http("POST", "/api/login", body=form, content_type="application/x-www-form-urlencoded")
    assert status == 200, body
    token = json.loads(body)["access_token"]
    yield SimpleNamespace(id=user_id, username=username, token=token)
    # Remove the history rows through the API first so their JSON/Redis payloads go with them.
    st, _, hist = _http("GET", "/api/history", token=token)
    if st == 200:
        ids = [h["id"] for h in json.loads(hist)]
        if ids:
            _http("DELETE", "/api/history", token=token, body=json.dumps({"history_ids": ids}).encode(),
                  content_type="application/json")
    subprocess.run(["docker", "compose", "exec", "-T", "api", "python", "-c", _USER_SQL_DELETE, user_id],
                   cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)


def _poll_result(token: str, job_id: str, timeout_s: float = 900) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st, _, body = _http("GET", f"/api/batch/{job_id}/result", token=token)
        assert st == 200, body
        data = json.loads(body)
        if data["status"] != "processing":
            return data
        time.sleep(2)
    raise AssertionError("timed out waiting for the worker")


def _poll_batch(token: str, job_id: str, timeout_s: float = 900) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st, _, body = _http("GET", f"/api/batch/{job_id}", token=token)
        assert st == 200, body
        data = json.loads(body)
        if data["status"] != "processing":
            return data
        time.sleep(2)
    raise AssertionError("timed out waiting for the batch")


_SCRATCH_LS = """
import glob, tempfile
print(sorted(glob.glob(tempfile.gettempdir() + '/spir_src_*') + glob.glob(tempfile.gettempdir() + '/san_*')))
"""


@pytest.mark.skipif(not _stack_running(), reason="SPIR docker compose stack with UPLOAD_STORAGE_BACKEND=minio is not running")
class TestDockerEndToEnd:
    def test_thresholds_unchanged_in_compose(self):
        env = _COMPOSE["services"]["api"]["environment"]
        assert env["LARGE_FILE_THRESHOLD_MB"] == "100" and env["GIANT_FILE_THRESHOLD_MB"] == "500"
        assert env["UPLOAD_STORAGE_BACKEND"] == "minio" and env.get("STORAGE_BACKEND", "filesystem") == "filesystem"
        assert _COMPOSE["services"]["api"]["ports"][0]["published"] == "8000"
        assert _COMPOSE["services"]["frontend"]["ports"][0]["published"] == "3000"

    def test_small_file_is_synchronous(self, e2e_user):
        assert WORKBOOK_SMALL.exists()
        body, ctype = _multipart({}, "file", WORKBOOK_SMALL.name, WORKBOOK_SMALL)
        status, _, resp = _http("POST", "/api/extract", token=e2e_user.token, body=body, content_type=ctype)
        assert status == 200, resp[:300]
        data = json.loads(resp)
        assert data["status"] == "done" and data["file_id"] and data["total_rows"] > 0
        # nothing of it went through MinIO
        bucket = MinioObjectStorage(**_MINIO, prefix="batch_uploads/")
        assert not any("RLCSF3" in o.key for o in bucket.list_objects())

    def test_large_file_through_minio_heavy_queue(self, e2e_user):
        assert WORKBOOK_223.exists() and WORKBOOK_223.stat().st_size > 100 * MB
        since = "60m"
        body, ctype = _multipart({}, "file", WORKBOOK_223.name, WORKBOOK_223)
        status, _, resp = _http("POST", "/api/extract", token=e2e_user.token, body=body, content_type=ctype)
        assert status == 202, resp[:300]
        queued = json.loads(resp)
        assert queued["status"] == "queued" and queued["queue"] == "heavy"
        job_id = queued["job_id"]
        key = source_object_key(job_id, 0, WORKBOOK_223.name)

        result = _poll_result(e2e_user.token, job_id)
        assert result["status"] == "done", result
        assert result["total_rows"] > 0 and result["file_id"]

        # The worker took it from MinIO: object stored under batch_uploads/<key>,
        # staged to a temp file, extracted, temp removed, object discarded.
        logs = _worker_logs("worker-heavy", since)
        assert "source.staged" in logs and key in logs, logs[-3000:]
        assert "source.deleted" in logs
        assert "extraction.complete" in logs
        bucket = MinioObjectStorage(**_MINIO, prefix="batch_uploads/")
        assert not bucket.exists(key)                                   # cleanup policy: deleted after success
        api_logs = _worker_logs("api", since)
        assert "source.stored" in api_logs and key in api_logs
        r = _compose_exec("worker-heavy", _SCRATCH_LS)
        assert r.returncode == 0 and r.stdout.strip() == "[]", r.stdout   # no temp file left

        # existing result / history / download behaviour
        st, _, hist = _http("GET", "/api/history", token=e2e_user.token)
        assert st == 200
        entries = [h for h in json.loads(hist) if h["file_id"] == result["file_id"]]
        assert len(entries) == 1 and entries[0]["filename"].startswith(WORKBOOK_223.stem)   # history keeps the output name
        st, headers, data = _http("GET", f"/api/download/{result['file_id']}", token=e2e_user.token)
        assert st == 200 and data[:2] == b"PK" and "attachment" in headers.get("content-disposition", "")

    def test_batch_normal_and_heavy_through_minio(self, e2e_user):
        since = "60m"
        names = [WORKBOOK_SMALL.name, WORKBOOK_223.name]
        st, _, resp = _http("POST", "/api/batch/register", token=e2e_user.token,
                            body=json.dumps({"filenames": names}).encode(), content_type="application/json")
        assert st == 200, resp
        job_id = json.loads(resp)["job_id"]
        for idx, wb in enumerate((WORKBOOK_SMALL, WORKBOOK_223)):
            body, ctype = _multipart({"file_idx": str(idx)}, "file", wb.name, wb)
            st, _, resp = _http("POST", f"/api/batch/{job_id}/upload", token=e2e_user.token, body=body, content_type=ctype)
            assert st == 200 and json.loads(resp)["status"] == "queued", resp[:300]

        job = _poll_batch(e2e_user.token, job_id)
        assert job["status"] == "done", job
        assert all(r["status"] == "ok" and r["file_id"] for r in job["results"])

        keys = [source_object_key(job_id, i, n) for i, n in enumerate(names)]
        assert keys[0] in _worker_logs("worker-normal", since)
        assert keys[1] in _worker_logs("worker-heavy", since)
        bucket = MinioObjectStorage(**_MINIO, prefix="batch_uploads/")
        assert not any(bucket.exists(k) for k in keys)

        st, headers, data = _http("GET", f"/api/batch/{job_id}/download", token=e2e_user.token)
        assert st == 200 and data[:2] == b"PK" and headers.get("content-type", "").startswith("application/zip")
        for svc in ("worker-normal", "worker-heavy"):
            r = _compose_exec(svc, _SCRATCH_LS)
            assert r.returncode == 0 and r.stdout.strip() == "[]", (svc, r.stdout)

    def test_no_stray_test_objects_left(self):
        bucket = MinioObjectStorage(**_MINIO, prefix="batch_uploads/")
        assert not [o.key for o in bucket.list_objects() if "e2e" in o.key]
        assert MinioObjectStorage(**_MINIO, prefix="").ping() is None   # bucket still there
