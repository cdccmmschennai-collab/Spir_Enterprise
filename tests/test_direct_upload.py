"""
Phase 3D — direct browser-to-MinIO upload.

Layers:

  Unit (always run)
    - Settings: MINIO_PUBLIC_ENDPOINT / presign creds / part size / TTL,
      direct_upload_configured only when everything lines up
    - part arithmetic + server-side part verification (pure functions)
    - MinioObjectStorage presigning is offline: URL targets the PUBLIC
      endpoint, carries key + uploadId + partNumber + expiry, PUT only,
      signed with the presign key id, never the secret; filesystem backend
      is not a DirectUploadStorage
    - job store upload records (in-memory): set/get/clear + atomic transition
    - lifecycle cleanup: abandoned multipart uploads reclaimed through the contract

  API (needs starlette TestClient / httpx)
    - auth: no token -> 401 on initiate / parts / complete / abort
    - ownership: another user's job -> 403 (super_admin allowed)
    - object key: server-derived (source_object_key), client cannot choose it,
      traversal in the filename cannot escape the area
    - mode "api" for <= threshold, Celery off, no direct backend
    - initiate -> parts -> complete -> 202 + Celery args carry the Phase 3C key
    - batch slot initiate (registered name wins), slot status "uploading"
    - re-presign, out-of-range parts, duplicate completion, incomplete
      upload (missing / wrong-size parts -> 409 + retry), wrong final size ->
      422 + object gone, abort -> parts gone + slot error, broker failure ->
      object gone + slot error, MinIO down -> 503 and upload still open
    - the API never receives the file body on the direct path (ASGI body meter)
    - /api/batch/{job}/upload refuses a slot that is uploading directly
    - existing sync / heavy / giant behaviour of /api/extract unchanged

  MinIO integration (skipped when the Compose MinIO is not reachable)
    - real multipart round trip through presigned PUTs from this host
    - expired URL rejected, wrong-size part detected, abort, list + cleanup
    - scoped uploader credential cannot write outside batch_uploads/

  Docker end-to-end (skipped unless the Compose stack is up with direct upload)
    - 223 MB workbook: initiate -> parts PUT to MinIO from the host ->
      complete -> heavy worker -> rows/tags -> source object gone -> history
      -> download; API container RX bytes prove the body bypassed FastAPI
    - small file still synchronous through /api/extract
    - CORS preflight from the frontend origin only

Run:   pytest tests/test_direct_upload.py -v
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spir_dynamic.app.config import Settings
from spir_dynamic.services import direct_upload as du
from spir_dynamic.services.direct_upload import (
    STATE_COMPLETING,
    STATE_UPLOADING,
    UploadIncomplete,
    UploadSession,
    UploadVerificationFailed,
    expected_part_size,
    finalize_upload,
    part_count,
    plan_upload,
    verify_parts,
)
from spir_dynamic.services.job_store import FileResult, JobStore
from spir_dynamic.services.object_storage import (
    DirectUploadStorage,
    LocalFilesystemStorage,
    MinioObjectStorage,
    MultipartUploadInfo,
    MultipartUploadNotFound,
    ObjectInfo,
    StorageConfigError,
    StorageUnavailable,
    UploadedPart,
    reset_object_storage,
)
from spir_dynamic.services.source_objects import source_object_key
from tests.test_source_objects import MemoryObjectStorage, _compose_config, _minio_target

REPO_ROOT = Path(__file__).resolve().parent.parent
MB = 1024 * 1024
PART = 5 * MB   # smallest legal S3 part; keeps the API-level fixtures small


# ── Test double ──────────────────────────────────────────────────────────────

class MemoryDirectStorage(MemoryObjectStorage):
    """MemoryObjectStorage + the DirectUploadStorage contract. Parts are written by the test ("the browser")."""

    backend: ClassVar[str] = "memory"

    def __init__(self, public: str = "http://public.test:9000") -> None:
        super().__init__()
        self.public = public
        self.uploads: dict[tuple[str, str], dict] = {}   # (key, upload_id) -> {"parts": {n: bytes}, "initiated": dt}
        self.aborted: list[tuple[str, str]] = []

    def supports_direct_upload(self) -> bool:
        return bool(self.public)

    def create_multipart_upload(self, key, *, content_type=None):
        self._check()
        uid = uuid.uuid4().hex
        self.uploads[(key, uid)] = {"parts": {}, "initiated": datetime.now(timezone.utc)}
        return uid

    def presign_upload_part(self, key, upload_id, part_number, *, expires_in):
        self._check()
        return (f"{self.public}/bucket/batch_uploads/{key}?uploadId={upload_id}&partNumber={part_number}"
                f"&X-Amz-Expires={expires_in}&X-Amz-Signature=deadbeef")

    def _upload(self, key, upload_id):
        try:
            return self.uploads[(key, upload_id)]
        except KeyError:
            raise MultipartUploadNotFound(key, upload_id)

    def list_parts(self, key, upload_id):
        self._check()
        parts = self._upload(key, upload_id)["parts"]
        return [UploadedPart(n, len(b), f'"etag{n}"') for n, b in sorted(parts.items())]

    def complete_multipart_upload(self, key, upload_id, parts):
        self._check()
        up = self._upload(key, upload_id)
        data = b"".join(up["parts"][p.part_number] for p in sorted(parts, key=lambda p: p.part_number))
        del self.uploads[(key, upload_id)]
        return self.put_bytes(key, data)

    def abort_multipart_upload(self, key, upload_id):
        self._check()
        self.aborted.append((key, upload_id))
        return self.uploads.pop((key, upload_id), None) is not None

    def list_multipart_uploads(self, prefix="") -> Iterator[MultipartUploadInfo]:
        self._check()
        for (key, uid), up in list(self.uploads.items()):
            if key.startswith(prefix):
                yield MultipartUploadInfo(key=key, upload_id=uid, initiated=up["initiated"])

    # "browser" helpers
    def put_part(self, key, upload_id, n, data: bytes) -> None:
        self._upload(key, upload_id)["parts"][n] = bytes(data)

    def put_all_parts(self, key, upload_id, payload: bytes, part_size: int) -> None:
        for n in range(1, part_count(len(payload), part_size) + 1):
            self.put_part(key, upload_id, n, payload[(n - 1) * part_size: n * part_size])

    def only_upload(self) -> tuple[str, str]:
        (k,) = self.uploads.keys()
        return k


_ENV = (
    "STORAGE_BACKEND", "UPLOAD_STORAGE_BACKEND", "MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY",
    "MINIO_BUCKET", "MINIO_SECURE", "MINIO_PUBLIC_ENDPOINT", "MINIO_PRESIGN_ACCESS_KEY",
    "MINIO_PRESIGN_SECRET_KEY", "DIRECT_UPLOAD_ENABLED", "DIRECT_UPLOAD_PART_SIZE_MB",
    "DIRECT_UPLOAD_URL_TTL_SECONDS", "CELERY_ENABLED", "REDIS_URL",
)


class FakeRedis:
    """The four hash operations the open-upload index uses."""

    def __init__(self) -> None:
        self.h: dict[str, dict[str, str]] = {}
        self.fail = False

    def _chk(self):
        if self.fail:
            raise ConnectionError("redis down")

    def hset(self, k, f, v):
        self._chk()
        self.h.setdefault(k, {})[f] = v

    def hdel(self, k, f):
        self._chk()
        self.h.get(k, {}).pop(f, None)

    def hgetall(self, k):
        self._chk()
        return dict(self.h.get(k, {}))

    def expire(self, k, ttl):
        self._chk()

    def entries(self) -> dict[str, str]:
        return dict(self.h.get(du._INDEX_KEY, {}))


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    reset_object_storage()
    fake = FakeRedis()
    with patch.object(du, "_redis", return_value=fake):
        yield fake
    reset_object_storage()


def _settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


_MINIO_KW = dict(minio_endpoint="http://minio:9000", minio_access_key="root", minio_secret_key="rootsecret")


# ── Unit: settings ───────────────────────────────────────────────────────────

class TestSettings:
    def test_defaults_off(self):
        s = _settings()
        assert s.minio_public_endpoint == "" and s.minio_presign_access_key == ""
        assert s.direct_upload_enabled is True
        assert s.direct_upload_part_size_mb == 16 and s.direct_upload_url_ttl_seconds == 3600
        assert s.direct_upload_configured is False

    def test_configured_needs_everything(self):
        base = dict(_MINIO_KW, minio_public_endpoint="http://localhost:9000", upload_storage_backend="minio")
        assert _settings(**base).direct_upload_configured is True
        assert _settings(**dict(base, upload_storage_backend="")).direct_upload_configured is False
        assert _settings(**dict(base, upload_storage_backend="", storage_backend="minio")).direct_upload_configured is True
        assert _settings(**dict(base, minio_public_endpoint="")).direct_upload_configured is False
        assert _settings(**dict(base, direct_upload_enabled=False)).direct_upload_configured is False
        assert _settings(**dict(base, minio_secret_key="")).direct_upload_configured is False

    def test_internal_endpoint_untouched_by_public_one(self):
        s = _settings(**_MINIO_KW, minio_public_endpoint="http://localhost:9000")
        assert s.minio_endpoint == "http://minio:9000"

    def test_env_and_validation(self, monkeypatch):
        monkeypatch.setenv("MINIO_PUBLIC_ENDPOINT", "https://files.example.com")
        monkeypatch.setenv("MINIO_PRESIGN_ACCESS_KEY", "up")
        monkeypatch.setenv("MINIO_PRESIGN_SECRET_KEY", "upsecret")
        monkeypatch.setenv("DIRECT_UPLOAD_PART_SIZE_MB", "8")
        monkeypatch.setenv("DIRECT_UPLOAD_URL_TTL_SECONDS", "900")
        s = _settings()
        assert (s.minio_public_endpoint, s.minio_presign_access_key, s.minio_presign_secret_key) == \
               ("https://files.example.com", "up", "upsecret")
        assert (s.direct_upload_part_size_mb, s.direct_upload_url_ttl_seconds) == (8, 900)
        with pytest.raises(ValueError):
            _settings(direct_upload_part_size_mb=4)
        with pytest.raises(ValueError):
            _settings(direct_upload_url_ttl_seconds=10)
        with pytest.raises(ValueError):
            _settings(direct_upload_url_ttl_seconds=8 * 86400)

    def test_queue_thresholds_and_limits_unchanged(self):
        s = _settings()
        assert (s.large_file_threshold_mb, s.giant_file_threshold_mb) == (100, 500)
        assert (s.max_file_size_mb, s.absolute_max_file_size_mb) == (2048, 1500)


# ── Unit: part arithmetic + verification ────────────────────────────────────

class TestParts:
    @pytest.mark.parametrize("size,ps,n", [(1, 5, 1), (5, 5, 1), (6, 5, 2), (10, 5, 2), (11, 5, 3), (223 * MB, 16 * MB, 14)])
    def test_part_count(self, size, ps, n):
        assert part_count(size, ps) == n

    def test_part_count_limits(self):
        with pytest.raises(ValueError):
            part_count(0, 5)
        with pytest.raises(ValueError):
            part_count(10_001 * 5, 5)

    def test_expected_part_size(self):
        assert [expected_part_size(11, 5, n) for n in (1, 2, 3)] == [5, 5, 1]
        assert expected_part_size(10, 5, 2) == 5
        with pytest.raises(ValueError):
            expected_part_size(11, 5, 4)

    def test_verify_parts(self):
        ok = [UploadedPart(1, 5, "a"), UploadedPart(2, 5, "b"), UploadedPart(3, 1, "c")]
        assert verify_parts(ok, 11, 5) == ([], [])
        assert verify_parts(ok[:2], 11, 5) == ([3], [])
        assert verify_parts([ok[0], UploadedPart(2, 4, "b"), ok[2]], 11, 5) == ([], [2])
        assert verify_parts(ok + [UploadedPart(4, 5, "d")], 11, 5) == ([], [4])      # extra part
        assert verify_parts([], 11, 5) == ([1, 2, 3], [])

    def test_session_roundtrip(self):
        s = UploadSession("j", 0, "f.xlsm", 11, "j_000_f.xlsm", "u", 5, 3, "user", "heavy")
        assert UploadSession.from_dict(s.to_dict()) == s
        assert UploadSession.from_dict({**s.to_dict(), "unknown": 1}) == s        # forward compatible


# ── Unit: MinIO presigning is offline and browser-facing ─────────────────────

def _minio(**kw) -> MinioObjectStorage:
    base = dict(endpoint="http://minio:9000", access_key="root", secret_key="rootsecret", bucket="spir-files",
                prefix="batch_uploads/", public_endpoint="http://localhost:9000")
    return MinioObjectStorage(**{**base, **kw})


class TestMinioPresign:
    def test_is_direct_upload_storage_only_with_public_endpoint(self):
        assert isinstance(_minio(), DirectUploadStorage) and _minio().supports_direct_upload()
        assert _minio(public_endpoint="").supports_direct_upload() is False
        assert not isinstance(LocalFilesystemStorage("x"), DirectUploadStorage)
        with pytest.raises(StorageConfigError):
            _minio(public_endpoint="").presign_upload_part("k", "u", 1, expires_in=60)

    def test_url_targets_public_endpoint_with_scoped_params(self):
        st = _minio(presign_access_key="spir_uploader", presign_secret_key="uploader-secret")
        url = st.presign_upload_part("job-1_000_a.xlsm", "UPLOAD1", 7, expires_in=600)
        u = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(u.query)
        assert (u.scheme, u.netloc) == ("http", "localhost:9000")                    # browser-reachable, not minio:9000
        assert u.path == "/spir-files/batch_uploads/job-1_000_a.xlsm"              # area prefix + Phase 3C key
        assert q["uploadId"] == ["UPLOAD1"] and q["partNumber"] == ["7"]
        assert q["X-Amz-Expires"] == ["600"] and q["X-Amz-SignedHeaders"] == ["host"]
        assert q["X-Amz-Credential"][0].startswith("spir_uploader/")               # scoped key id, not root
        for secret in ("rootsecret", "uploader-secret", "root/"):
            assert secret not in url
        assert st.endpoint_url == "http://minio:9000"                                # server side unchanged
        assert st._client is None                                                    # no server call made

    def test_presign_falls_back_to_main_credentials(self):
        url = _minio().presign_upload_part("k.xlsm", "U", 1, expires_in=60)
        assert "X-Amz-Credential=root%2F" in url and "rootsecret" not in url

    def test_public_endpoint_scheme_follows_secure_flag(self):
        assert _minio(public_endpoint="files.example.com", secure=True).public_endpoint_url == "https://files.example.com"
        assert _minio(public_endpoint="https://files.example.com").public_endpoint_url == "https://files.example.com"

    def test_presign_credentials_must_come_in_pairs(self):
        with pytest.raises(StorageConfigError):
            _minio(presign_access_key="u")

    def test_from_settings_reads_phase_3d_settings(self):
        s = _settings(**_MINIO_KW, minio_public_endpoint="http://localhost:9000",
                      minio_presign_access_key="up", minio_presign_secret_key="ups")
        st = MinioObjectStorage.from_settings(s, prefix="batch_uploads/")
        assert st.public_endpoint_url == "http://localhost:9000" and st._presign_access_key == "up"

    def test_invalid_part_numbers_rejected(self):
        for n in (0, 10_001):
            with pytest.raises(Exception):
                _minio().presign_upload_part("k", "u", n, expires_in=60)

    def test_multipart_ops_unreachable_are_controlled(self):
        st = _minio(endpoint="http://192.0.2.1:9", connect_timeout=0.5, read_timeout=0.5, max_attempts=1)
        for op in (lambda: st.create_multipart_upload("k"), lambda: st.list_parts("k", "u"),
                   lambda: st.abort_multipart_upload("k", "u"), lambda: list(st.list_multipart_uploads()),
                   lambda: st.complete_multipart_upload("k", "u", [UploadedPart(1, 1, "e")])):
            with pytest.raises(StorageUnavailable) as ei:
                op()
            assert "botocore" not in type(ei.value).__module__


# ── Unit: job store upload records ───────────────────────────────────────────

class TestJobStoreUploadRecords:
    def test_set_get_clear_and_transition(self):
        js = JobStore()
        js.create("j", ["a.xlsm"], user_id="u")
        assert js.get_upload("j", 0) is None
        js.set_upload("j", 0, {"state": STATE_UPLOADING, "x": 1})
        assert js.get_upload("j", 0) == {"state": STATE_UPLOADING, "x": 1}
        assert js.transition_upload("j", 0, STATE_UPLOADING, STATE_COMPLETING) is True
        assert js.transition_upload("j", 0, STATE_UPLOADING, STATE_COMPLETING) is False   # only one winner
        assert js.get_upload("j", 0)["state"] == STATE_COMPLETING
        js.clear_upload("j", 0)
        assert js.get_upload("j", 0) is None and js.transition_upload("j", 0, "a", "b") is False

    def test_uploading_slot_keeps_job_processing_and_has_no_queue_position(self):
        js = JobStore()
        js.create("j", ["a.xlsm", "b.xlsm"])
        js.update_result("j", 0, FileResult(filename="a.xlsm", status="uploading"))
        d = js.get("j").to_dict()
        assert d["status"] == "processing" and d["completed"] == 0
        assert d["results"][0]["queue_position"] is None and d["results"][1]["queue_position"] == 1

    def test_redis_store_has_same_interface(self):
        from spir_dynamic.services.redis_job_store import RedisJobStore
        for name in ("set_upload", "get_upload", "clear_upload", "transition_upload"):
            assert callable(getattr(RedisJobStore, name))


# ── Unit: service functions against the double ──────────────────────────────

class TestService:
    def test_plan_creates_multipart_and_signs_every_part(self):
        st = MemoryDirectStorage()
        session, urls = plan_upload(st, job_id="job-1", file_idx=2, filename="../x/big file.xlsm", size=11,
                                    user_id="u", queue="heavy", part_size=5, url_ttl=60)
        assert session.source_key == source_object_key("job-1", 2, "../x/big file.xlsm") == "job-1_002_big file.xlsm"
        assert session.part_count == 3 and [p["part_number"] for p in urls] == [1, 2, 3]
        assert all("X-Amz-Expires=60" in p["url"] for p in urls)
        assert st.only_upload() == (session.source_key, session.upload_id)

    def test_plan_failure_after_create_aborts_multipart(self):
        st = MemoryDirectStorage()
        with patch.object(MemoryDirectStorage, "presign_upload_part", side_effect=StorageUnavailable("down")):
            with pytest.raises(StorageUnavailable):
                plan_upload(st, job_id="j", file_idx=0, filename="a.xlsm", size=1, user_id="", queue="heavy",
                            part_size=5, url_ttl=60)
        assert st.uploads == {} and len(st.aborted) == 1

    def _session(self, st, size=11):
        s, _ = plan_upload(st, job_id="j", file_idx=0, filename="a.xlsm", size=size, user_id="u", queue="heavy",
                           part_size=5, url_ttl=60)
        return s

    def test_finalize_verifies_and_assembles(self):
        st = MemoryDirectStorage()
        s = self._session(st)
        st.put_all_parts(s.source_key, s.upload_id, b"x" * 11, 5)
        info = finalize_upload(st, s)
        assert info.size == 11 and st.objects[s.source_key] == b"x" * 11 and st.uploads == {}

    def test_finalize_reports_missing_and_wrong_parts_and_keeps_upload_open(self):
        st = MemoryDirectStorage()
        s = self._session(st)
        st.put_part(s.source_key, s.upload_id, 1, b"x" * 5)
        st.put_part(s.source_key, s.upload_id, 2, b"x" * 3)
        with pytest.raises(UploadIncomplete) as ei:
            finalize_upload(st, s)
        assert ei.value.extra == {"missing_parts": [3], "wrong_parts": [2]}
        assert st.only_upload() == (s.source_key, s.upload_id) and s.source_key not in st.objects

    def test_finalize_size_mismatch_discards_object(self):
        st = MemoryDirectStorage()
        s = self._session(st)
        st.put_all_parts(s.source_key, s.upload_id, b"x" * 11, 5)
        with patch.object(MemoryDirectStorage, "complete_multipart_upload",
                          return_value=ObjectInfo(s.source_key, 12, datetime.now(timezone.utc))):
            st.objects[s.source_key] = b"x" * 12
            with pytest.raises(UploadVerificationFailed) as ei:
                finalize_upload(st, s)
        assert ei.value.extra == {"expected_size": 11, "actual_size": 12}
        assert s.source_key not in st.objects

    def test_direct_upload_storage_resolution(self, tmp_path):
        with patch.object(du, "get_object_storage", return_value=LocalFilesystemStorage(tmp_path)):
            assert du.direct_upload_storage() is None
        with patch.object(du, "get_object_storage", return_value=_minio(public_endpoint="")):
            assert du.direct_upload_storage() is None
        with patch.object(du, "get_object_storage", return_value=_minio()):
            assert du.direct_upload_storage() is not None


# ── Unit: cleanup reclaims abandoned multipart uploads ───────────────────────

class TestOpenUploadIndex:
    def test_plan_records_finalize_and_abort_remove(self, _isolate):
        st = MemoryDirectStorage()
        s, _ = plan_upload(st, job_id="j", file_idx=0, filename="a.xlsm", size=11, user_id="", queue="heavy",
                           part_size=5, url_ttl=60)
        assert list(_isolate.entries()) == [f"{s.source_key}\n{s.upload_id}"]
        assert [(u.key, u.upload_id) for u in du.open_uploads()] == [(s.source_key, s.upload_id)]
        st.put_all_parts(s.source_key, s.upload_id, b"x" * 11, 5)
        finalize_upload(st, s)
        assert _isolate.entries() == {}
        s2, _ = plan_upload(st, job_id="j2", file_idx=0, filename="b.xlsm", size=11, user_id="", queue="heavy",
                            part_size=5, url_ttl=60)
        du.abort_upload(st, s2, context="t")
        assert _isolate.entries() == {}

    def test_index_failures_never_break_the_upload(self, _isolate):
        _isolate.fail = True
        st = MemoryDirectStorage()
        s, urls = plan_upload(st, job_id="j", file_idx=0, filename="a.xlsm", size=11, user_id="", queue="heavy",
                              part_size=5, url_ttl=60)
        assert len(urls) == 3 and du.open_uploads() == []
        st.put_all_parts(s.source_key, s.upload_id, b"x" * 11, 5)
        assert finalize_upload(st, s).size == 11


class TestCleanup:
    def _aged(self, fake: FakeRedis, key: str, upload_id: str, hours: float) -> None:
        fake.hset(du._INDEX_KEY, f"{key}\n{upload_id}", str(time.time() - hours * 3600))

    def test_stale_multipart_uploads_aborted_recent_kept(self, _isolate):
        from spir_dynamic.tasks.cleanup_tasks import _purge_stale_multipart_uploads, _takes_direct_uploads
        st = MemoryDirectStorage()
        old = st.create_multipart_upload("old_000_a.xlsm")
        mid = st.create_multipart_upload("mid_000_b.xlsm")
        new = st.create_multipart_upload("new_000_c.xlsm")
        self._aged(_isolate, "old_000_a.xlsm", old, 30)
        self._aged(_isolate, "mid_000_b.xlsm", mid, 5)
        self._aged(_isolate, "new_000_c.xlsm", new, 0.1)
        self._aged(_isolate, "gone_000_d.xlsm", "vanished", 40)      # MinIO already expired it

        assert _takes_direct_uploads(st) and not _takes_direct_uploads(MemoryObjectStorage())
        with patch("spir_dynamic.monitoring.metrics.CLEANUP_DELETED_FILES"):
            dry = _purge_stale_multipart_uploads(st, stale_hours=24, dry_run=True)
            assert dry["aborted"] == 0 and len(st.uploads) == 3 and len(_isolate.entries()) == 4
            wet = _purge_stale_multipart_uploads(st, stale_hours=24, dry_run=False)
        assert wet == {"backend": "memory", "stale_hours": 24, "aborted": 1, "skipped_recent": 1,
                       "dropped_stale_entries": 1}
        assert set(k for k, _ in st.uploads) == {"mid_000_b.xlsm", "new_000_c.xlsm"}
        assert set(f.split("\n")[0] for f in _isolate.entries()) == {"mid_000_b.xlsm", "new_000_c.xlsm"}

    def test_lifecycle_task_runs_phase_only_on_direct_backend(self, tmp_path):
        from spir_dynamic.tasks.cleanup_tasks import lifecycle_cleanup_task
        cfg = SimpleNamespace(cleanup_dry_run=True, rows_storage_path=str(tmp_path / "rows"),
                              batch_upload_dir=str(tmp_path / "up"), cleanup_json_retention_days=14,
                              cleanup_upload_stale_hours=24, database_url="")
        with patch("spir_dynamic.app.config.get_settings", return_value=cfg), \
             patch("spir_dynamic.monitoring.metrics.CLEANUP_DURATION"), \
             patch("spir_dynamic.tasks.cleanup_tasks._source_object_storage", return_value=MemoryDirectStorage()):
            summary = lifecycle_cleanup_task.run(dry_run=True)
        assert "stale_multipart_uploads" in summary["phases"]
        with patch("spir_dynamic.app.config.get_settings", return_value=cfg), \
             patch("spir_dynamic.monitoring.metrics.CLEANUP_DURATION"), \
             patch("spir_dynamic.tasks.cleanup_tasks._source_object_storage", return_value=MemoryObjectStorage()):
            summary = lifecycle_cleanup_task.run(dry_run=True)
        assert "stale_multipart_uploads" not in summary["phases"]


# ── API ──────────────────────────────────────────────────────────────────────

def _testclient_available() -> bool:
    try:
        from fastapi.testclient import TestClient  # noqa: F401
        import httpx  # noqa: F401
        return True
    except Exception:
        return False


class BodyMeter:
    """ASGI wrapper counting request-body bytes that reach the FastAPI app."""

    def __init__(self, app) -> None:
        self.app = app
        self.bytes = 0
        self.by_path: dict[str, int] = {}

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")

        async def rx():
            msg = await receive()
            if msg["type"] == "http.request":
                n = len(msg.get("body", b""))
                self.bytes += n
                self.by_path[path] = self.by_path.get(path, 0) + n
            return msg

        await self.app(scope, rx, send)


_P = SimpleNamespace(
    settings="spir_dynamic.app.upload_router.get_settings",
    storage="spir_dynamic.app.upload_router.direct_upload_storage",
    job_store="spir_dynamic.app.upload_router.get_job_store",
    dispatch="spir_dynamic.app.upload_router._dispatch_celery",
    persist="spir_dynamic.app.upload_router._persist_job_to_db",
    batch_settings="spir_dynamic.app.batch_router.get_settings",
    batch_store="spir_dynamic.app.batch_router.get_job_store",
    batch_persist="spir_dynamic.app.batch_router._persist_job_to_db",
    routes_settings="spir_dynamic.app.routes.get_settings",
)


@pytest.fixture
def api(tmp_path):
    if not _testclient_available():
        pytest.skip("starlette TestClient / httpx not installed in this environment")
    from fastapi.testclient import TestClient
    from spir_dynamic.app.auth import TokenData, get_current_user
    from spir_dynamic.app.config import get_settings
    from spir_dynamic.app.main import app

    storage = MemoryDirectStorage()
    store = JobStore()
    cfg = get_settings().model_copy(update={
        "rows_storage_path": str(tmp_path / "rows"), "batch_upload_dir": str(tmp_path / "uploads"),
        "max_file_size_mb": 2048, "absolute_max_file_size_mb": 1500,
        # 10 MB threshold so the 11 MB / 5 MB-part payloads below count as "large"
        # (the Docker E2E exercises the real 100 MB default).
        "large_file_threshold_mb": 10, "giant_file_threshold_mb": 500, "celery_enabled": True,
        "minio_endpoint": "http://minio:9000", "minio_access_key": "root", "minio_secret_key": "rootsecret",
        "minio_public_endpoint": "http://localhost:9000", "upload_storage_backend": "minio",
        "direct_upload_part_size_mb": 5, "direct_upload_url_ttl_seconds": 600, "direct_upload_enabled": True,
    })
    user = {"td": TokenData("tester", "user-1", "jti-1")}
    app.dependency_overrides[get_current_user] = lambda: user["td"]
    meter = BodyMeter(app)
    dispatch = MagicMock(return_value=["heavy"])
    with patch(_P.settings, return_value=cfg), patch(_P.storage, return_value=storage), \
         patch(_P.job_store, return_value=store), patch(_P.dispatch, dispatch), \
         patch(_P.persist, new=AsyncMock()), patch(_P.batch_settings, return_value=cfg), \
         patch(_P.batch_store, return_value=store), patch(_P.batch_persist, new=AsyncMock()), \
         patch("spir_dynamic.services.source_objects.get_source_storage", return_value=storage):
        try:
            yield SimpleNamespace(client=TestClient(meter), cfg=cfg, storage=storage, store=store,
                                  dispatch=dispatch, meter=meter, user=user, app=app)
        finally:
            app.dependency_overrides.clear()


def _initiate(api, size=11 * MB, name="big.xlsm", **extra):
    return api.client.post("/api/uploads/initiate", json={"filename": name, "size": size, **extra})


def _upload_all(api, plan, payload: bytes):
    key = source_object_key(plan["job_id"], plan["file_idx"], plan["filename"])
    (k, uid), = [ku for ku in api.storage.uploads if ku[0] == key]
    api.storage.put_all_parts(k, uid, payload, plan["part_size"])
    return k, uid


class TestAuth:
    def test_unauthenticated_requests_rejected(self, api):
        from spir_dynamic.app.auth import get_current_user
        api.app.dependency_overrides.pop(get_current_user)
        c = api.client
        assert _initiate(api).status_code == 401
        assert c.post("/api/uploads/j/0/parts", json={"part_numbers": [1]}).status_code == 401
        assert c.post("/api/uploads/j/0/complete").status_code == 401
        assert c.delete("/api/uploads/j/0").status_code == 401
        assert api.storage.uploads == {}


class TestOwnership:
    def _foreign_job(self, api):
        api.store.create("theirs", ["theirs.xlsm"], user_id="user-2")
        api.store.set_upload("theirs", 0, UploadSession("theirs", 0, "theirs.xlsm", 11 * MB, "theirs_000_theirs.xlsm",
                                                        "U", 5 * MB, 3, "user-2", "heavy").to_dict())
        api.store.update_result("theirs", 0, FileResult(filename="theirs.xlsm", status="uploading"))

    def test_cannot_touch_another_users_job(self, api):
        self._foreign_job(api)
        assert _initiate(api, size=200 * MB, job_id="theirs", file_idx=0).status_code == 403
        assert _initiate(api, size=1 * MB, job_id="theirs", file_idx=0).status_code == 403   # even when "api"
        assert api.client.post("/api/uploads/theirs/0/parts", json={"part_numbers": [1]}).status_code == 403
        assert api.client.post("/api/uploads/theirs/0/complete").status_code == 403
        assert api.client.delete("/api/uploads/theirs/0").status_code == 403
        assert api.store.get("theirs").results[0].status == "uploading"          # untouched

    def test_super_admin_may(self, api):
        from spir_dynamic.app.auth import SUPER_ADMIN, TokenData
        self._foreign_job(api)
        api.user["td"] = TokenData("root", "admin-1", "jti", role=SUPER_ADMIN)
        assert api.client.delete("/api/uploads/theirs/0").status_code == 200

    def test_unknown_job_and_bad_slot(self, api):
        assert api.client.post("/api/uploads/nope/0/complete").status_code == 404
        api.store.create("j", ["a.xlsm"], user_id="user-1")
        assert api.client.post("/api/uploads/j/5/complete").status_code == 400
        assert api.client.post("/api/uploads/j/0/complete").status_code == 409   # pending slot, nothing to complete
        api.store.update_result("j", 0, FileResult(filename="a.xlsm", status="error", error="x"))
        assert api.client.post("/api/uploads/j/0/complete").status_code == 404   # nothing in progress
        assert api.client.delete("/api/uploads/j/0").status_code == 404


class TestMode:
    def test_small_file_uses_api(self, api):
        r = _initiate(api, size=10 * MB)                                      # exactly at the threshold
        assert r.status_code == 200 and r.json() == {"mode": "api", "reason": "small_file", "queue": "normal"}
        assert api.storage.uploads == {} and api.dispatch.call_count == 0

    def test_first_byte_over_threshold_is_direct_heavy(self, api):
        r = _initiate(api, size=10 * MB + 1)
        assert r.status_code == 200 and r.json()["mode"] == "direct" and r.json()["queue"] == "heavy"

    def test_giant_is_direct_giant(self, api):
        assert _initiate(api, size=500 * MB + 1).json()["queue"] == "giant"

    def test_celery_off_uses_api(self, api):
        with patch(_P.settings, return_value=api.cfg.model_copy(update={"celery_enabled": False})):
            assert _initiate(api).json()["reason"] == "direct_upload_unavailable"

    def test_no_direct_backend_uses_api(self, api):
        with patch(_P.storage, return_value=None):
            assert _initiate(api).json()["reason"] == "direct_upload_unavailable"
        with patch(_P.settings, return_value=api.cfg.model_copy(update={"minio_public_endpoint": ""})):
            assert _initiate(api).json()["mode"] == "api"
        with patch(_P.settings, return_value=api.cfg.model_copy(update={"direct_upload_enabled": False})):
            assert _initiate(api).json()["mode"] == "api"

    def test_size_limits(self, api):
        assert _initiate(api, size=0).status_code == 422
        assert _initiate(api, size=1501 * MB).status_code == 413          # absolute_max_file_size_mb
        assert _initiate(api, size=11 * MB, job_id="x").status_code == 400  # job_id without file_idx


class TestDirectFlow:
    def test_initiate_upload_complete(self, api):
        r = _initiate(api, size=11 * MB, name="../../etc/VEN big:file.xlsm")
        assert r.status_code == 200
        plan = r.json()
        job_id = plan["job_id"]
        # server-owned key: traversal stripped, job/slot encoded, extension kept
        key = source_object_key(job_id, 0, "../../etc/VEN big:file.xlsm")
        assert key == f"{job_id}_000_VEN big_file.xlsm"
        assert api.storage.only_upload()[0] == key
        assert plan["part_count"] == 3 and plan["part_size"] == 5 * MB and plan["url_expires_in"] == 600
        assert [p["part_number"] for p in plan["parts"]] == [1, 2, 3]
        assert all(p["url"].startswith("http://public.test:9000/") and key in p["url"] for p in plan["parts"])
        assert "rootsecret" not in r.text and "upload_id" not in plan          # nothing secret / internal leaks
        assert api.store.get(job_id).results[0].status == "uploading"
        assert api.store.get(job_id).user_id == "user-1"

        # "browser" writes the parts straight to storage, then completes
        _upload_all(api, plan, b"x" * (11 * MB))
        done = api.client.post(f"/api/uploads/{job_id}/0/complete")
        assert done.status_code == 202
        body = done.json()
        assert body["status"] == "queued" and body["queue"] == "heavy" and body["job_id"] == job_id
        assert body["filename"] == "../../etc/VEN big:file.xlsm" and body["size_mb"] == 11.0

        assert api.storage.objects[key] == b"x" * (11 * MB) and api.storage.uploads == {}
        api.dispatch.assert_called_once()
        _job, file_data, _cfg, user_id = api.dispatch.call_args.args
        assert _job == job_id and user_id == "user-1" and api.dispatch.call_args.kwargs == {"idx_offset": 0}
        assert file_data == [(key, "../../etc/VEN big:file.xlsm", 11 * MB)]      # Phase 3C contract
        assert api.store.get(job_id).results[0].status == "pending"
        assert api.store.get_upload(job_id, 0) is None
        # /result behaves as for any queued single-file job
        assert api.client.get(f"/api/batch/{job_id}/result").json()["status"] == "processing"

    def test_api_never_receives_the_file_body(self, api):
        payload = os.urandom(11 * MB)
        plan = _initiate(api, size=len(payload)).json()
        _upload_all(api, plan, payload)
        assert api.client.post(f"/api/uploads/{plan['job_id']}/0/complete").status_code == 202
        assert api.storage.objects[source_object_key(plan["job_id"], 0, "big.xlsm")] == payload
        assert api.meter.bytes < 4096, api.meter.by_path                       # control requests only
        assert all("/api/uploads" in p for p in api.meter.by_path)

    def test_result_endpoint_reports_uploading_as_processing(self, api):
        plan = _initiate(api).json()
        r = api.client.get(f"/api/batch/{plan['job_id']}/result").json()
        assert r["status"] == "processing" and r["phase"] == "uploading"

    def test_batch_slot_uses_registered_name_and_marks_slot(self, api):
        reg = api.client.post("/api/batch/register", json={"filenames": ["s.xlsx", "registered.xlsm"]})
        job_id = reg.json()["job_id"]
        r = _initiate(api, size=11 * MB, name="client-says-otherwise.xlsm", job_id=job_id, file_idx=1)
        assert r.status_code == 200 and r.json()["filename"] == "registered.xlsm"
        key = source_object_key(job_id, 1, "registered.xlsm")
        assert api.storage.only_upload()[0] == key
        d = api.store.get(job_id).to_dict()
        assert d["results"][1]["status"] == "uploading" and d["results"][0]["queue_position"] == 1
        # the classic batch upload refuses the slot while the direct upload owns it
        up = api.client.post(f"/api/batch/{job_id}/upload", data={"file_idx": "1"},
                             files={"file": ("registered.xlsm", b"x", "application/octet-stream")})
        assert up.status_code == 409
        _upload_all(api, r.json(), b"y" * (11 * MB))
        assert api.client.post(f"/api/uploads/{job_id}/1/complete").status_code == 202
        assert api.dispatch.call_args.kwargs == {"idx_offset": 1}
        assert api.dispatch.call_args.args[1] == [(key, "registered.xlsm", 11 * MB)]

    def test_batch_slot_not_pending_is_conflict(self, api):
        api.store.create("j", ["a.xlsm"], user_id="user-1")
        api.store.update_result("j", 0, FileResult(filename="a.xlsm", status="ok"))
        assert _initiate(api, job_id="j", file_idx=0).status_code == 409

    def test_reinitiate_aborts_previous_upload(self, api):
        api.store.create("j", ["a.xlsm"], user_id="user-1")
        first = _initiate(api, job_id="j", file_idx=0).json()
        second = _initiate(api, job_id="j", file_idx=0).json()
        assert first["parts"][0]["url"] != second["parts"][0]["url"]
        assert len(api.storage.uploads) == 1 and len(api.storage.aborted) == 1

    def test_presign_more_parts(self, api):
        plan = _initiate(api).json()
        r = api.client.post(f"/api/uploads/{plan['job_id']}/0/parts", json={"part_numbers": [3, 1, 3]})
        assert r.status_code == 200
        assert [p["part_number"] for p in r.json()["parts"]] == [1, 3]
        assert api.client.post(f"/api/uploads/{plan['job_id']}/0/parts", json={"part_numbers": [4]}).status_code == 400
        assert api.client.post(f"/api/uploads/{plan['job_id']}/0/parts", json={"part_numbers": []}).status_code == 422

    def test_incomplete_upload_is_409_with_retry_hint_and_stays_open(self, api):
        plan = _initiate(api).json()
        key, uid = api.storage.only_upload()
        api.storage.put_part(key, uid, 1, b"x" * (5 * MB))
        api.storage.put_part(key, uid, 2, b"x" * (5 * MB - 1))
        r = api.client.post(f"/api/uploads/{plan['job_id']}/0/complete")
        assert r.status_code == 409
        assert r.json()["missing_parts"] == [3] and r.json()["wrong_parts"] == [2]
        assert api.store.get_upload(plan["job_id"], 0)["state"] == STATE_UPLOADING   # client may retry
        api.dispatch.assert_not_called()
        # retry the reported parts, then complete succeeds
        api.storage.put_part(key, uid, 2, b"x" * (5 * MB))
        api.storage.put_part(key, uid, 3, b"x" * MB)
        assert api.client.post(f"/api/uploads/{plan['job_id']}/0/complete").status_code == 202

    def test_duplicate_completion(self, api):
        plan = _initiate(api).json()
        _upload_all(api, plan, b"x" * (11 * MB))
        assert api.client.post(f"/api/uploads/{plan['job_id']}/0/complete").status_code == 202
        again = api.client.post(f"/api/uploads/{plan['job_id']}/0/complete")
        assert again.status_code == 409 and "already completed" in again.json()["detail"]
        api.dispatch.assert_called_once()                                        # never queued twice

    def test_concurrent_completion_only_one_wins(self, api):
        plan = _initiate(api).json()
        assert api.store.transition_upload(plan["job_id"], 0, STATE_UPLOADING, STATE_COMPLETING)
        r = api.client.post(f"/api/uploads/{plan['job_id']}/0/complete")
        assert r.status_code == 409 and "being completed" in r.json()["detail"]
        assert api.client.post(f"/api/uploads/{plan['job_id']}/0/parts", json={"part_numbers": [1]}).status_code == 409
        assert api.client.delete(f"/api/uploads/{plan['job_id']}/0").status_code == 409

    def test_wrong_final_size_fails_and_removes_object(self, api):
        plan = _initiate(api).json()
        key, uid = api.storage.only_upload()
        # parts pass the per-part check but the backend assembles a different object
        api.storage.put_all_parts(key, uid, b"x" * (11 * MB), 5 * MB)
        with patch.object(MemoryDirectStorage, "complete_multipart_upload",
                          side_effect=lambda k, u, p: api.storage.put_bytes(k, b"z" * 10)):
            r = api.client.post(f"/api/uploads/{plan['job_id']}/0/complete")
        assert r.status_code == 422 and "verification failed" in r.json()["detail"]
        assert key not in api.storage.objects and api.store.get_upload(plan["job_id"], 0) is None
        slot = api.store.get(plan["job_id"]).results[0]
        assert slot.status == "error" and "verification" in slot.error
        api.dispatch.assert_not_called()

    def test_upload_vanished_is_conflict(self, api):
        plan = _initiate(api).json()
        key, uid = api.storage.only_upload()
        del api.storage.uploads[(key, uid)]                                      # expired / swept
        r = api.client.post(f"/api/uploads/{plan['job_id']}/0/complete")
        assert r.status_code == 409 and api.store.get(plan["job_id"]).results[0].status == "error"

    def test_abort_discards_parts_and_fails_slot(self, api):
        plan = _initiate(api).json()
        key, uid = api.storage.only_upload()
        api.storage.put_part(key, uid, 1, b"x" * (5 * MB))
        r = api.client.delete(f"/api/uploads/{plan['job_id']}/0")
        assert r.status_code == 200 and r.json()["status"] == "aborted"
        assert api.storage.uploads == {} and key not in api.storage.objects
        slot = api.store.get(plan["job_id"]).results[0]
        assert slot.status == "error" and slot.error == "Upload cancelled"
        assert api.client.delete(f"/api/uploads/{plan['job_id']}/0").status_code == 404

    def test_broker_failure_after_verification_discards_object(self, api):
        plan = _initiate(api).json()
        key, _ = _upload_all(api, plan, b"x" * (11 * MB))
        api.dispatch.side_effect = RuntimeError("broker down")
        r = api.client.post(f"/api/uploads/{plan['job_id']}/0/complete")
        assert r.status_code == 503 and "broker down" in r.json()["detail"]
        assert key not in api.storage.objects
        slot = api.store.get(plan["job_id"]).results[0]
        assert slot.status == "error" and "queue" in slot.error

    def test_storage_down_on_initiate_is_503_and_creates_nothing(self, api):
        api.storage.fail_with = StorageUnavailable("minio down")
        r = _initiate(api)
        assert r.status_code == 503 and "minio down" in r.json()["detail"]
        assert api.store._jobs == {} and api.storage.uploads == {}

    def test_storage_down_on_complete_keeps_upload_open(self, api):
        plan = _initiate(api).json()
        _upload_all(api, plan, b"x" * (11 * MB))
        api.storage.fail_with = StorageUnavailable("minio down")
        assert api.client.post(f"/api/uploads/{plan['job_id']}/0/complete").status_code == 503
        api.storage.fail_with = None
        assert api.store.get_upload(plan["job_id"], 0)["state"] == STATE_UPLOADING
        assert api.client.post(f"/api/uploads/{plan['job_id']}/0/complete").status_code == 202

    def test_health_reports_direct_upload(self, api):
        with patch(_P.routes_settings, return_value=api.cfg), \
             patch("spir_dynamic.app.routes.get_object_storage", return_value=MemoryObjectStorage()):
            assert api.client.get("/api/health").json()["direct_upload"] == "enabled"


class TestExistingWorkflowsUnchanged:
    """The classic endpoints behave exactly as in Phase 3C when the client does not use direct upload."""

    _RESULT = {"status": "done", "file_id": "fid-1", "filename": "x_Extraction.xlsx", "spir_no": "S",
               "total_rows": 1, "total_tags": 1, "preview_cols": [], "preview_rows": []}

    def _post(self, api, size):
        return api.client.post("/api/extract", files={"file": ("f.xlsm", b"x" * size, "application/octet-stream")})

    def test_small_stays_sync(self, api):
        with patch(_P.routes_settings, return_value=api.cfg), \
             patch("spir_dynamic.app.routes.run_pipeline", return_value=dict(self._RESULT)) as pipe, \
             patch("spir_dynamic.app.routes._dispatch_celery") as dispatch:
            r = self._post(api, 10)
        assert r.status_code == 200 and pipe.called and not dispatch.called and api.storage.objects == {}

    @pytest.mark.parametrize("large,giant,queue", [(0, 500, "heavy"), (0, 0, "giant")])
    def test_large_still_accepted_through_api(self, api, large, giant, queue):
        cfg = api.cfg.model_copy(update={"large_file_threshold_mb": large, "giant_file_threshold_mb": giant})
        task = MagicMock()
        with patch(_P.routes_settings, return_value=cfg), patch("spir_dynamic.app.routes.run_pipeline") as pipe, \
             patch("spir_dynamic.tasks.extraction_tasks.process_file_task", task), \
             patch("spir_dynamic.monitoring.metrics.GIANT_FILES_ROUTED"), \
             patch("spir_dynamic.app.routes.get_job_store", return_value=api.store), \
             patch("spir_dynamic.app.routes._persist_job_to_db", new=AsyncMock()):
            r = self._post(api, 10)
        assert r.status_code == 202 and r.json()["queue"] == queue and not pipe.called
        assert task.apply_async.call_args.kwargs["queue"] == queue
        assert len(api.storage.objects) == 1

    def test_batch_upload_endpoint_unchanged(self, api):
        job_id = api.client.post("/api/batch/register", json={"filenames": ["a.xlsx"]}).json()["job_id"]
        task = MagicMock()
        with patch("spir_dynamic.tasks.extraction_tasks.process_file_task", task), \
             patch("spir_dynamic.monitoring.metrics.GIANT_FILES_ROUTED"):
            r = api.client.post(f"/api/batch/{job_id}/upload", data={"file_idx": "0"},
                                files={"file": ("a.xlsx", b"x" * 10, "application/octet-stream")})
        assert r.status_code == 200 and r.json()["status"] == "queued"
        assert task.apply_async.call_args.kwargs["queue"] == "normal"
        assert task.apply_async.call_args.kwargs["args"] == [job_id, 0, source_object_key(job_id, 0, "a.xlsx"), "a.xlsx", "user-1"]


# ── MinIO integration ────────────────────────────────────────────────────────

_MINIO = _minio_target()
_COMPOSE = _compose_config() if _MINIO else None
TEST_PREFIX_ROOT = "batch_uploads/_tests/direct_upload/"   # inside the uploader's allowed prefix


def _uploader_creds() -> tuple[str, str] | None:
    if not _COMPOSE:
        return None
    env = _COMPOSE["services"].get("api", {}).get("environment", {})
    ak, sk = env.get("MINIO_PRESIGN_ACCESS_KEY"), env.get("MINIO_PRESIGN_SECRET_KEY")
    return (ak, sk) if ak and sk else None


def _put(url: str, data: bytes, timeout: float = 120) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=data, method="PUT")
    req.add_header("Content-Type", "application/octet-stream")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}


@pytest.fixture
def minio_direct():
    if _MINIO is None:
        pytest.skip("local MinIO (Compose stack) is not reachable")
    prefix = f"{TEST_PREFIX_ROOT}{uuid.uuid4()}/"
    st = MinioObjectStorage(**_MINIO, prefix=prefix, public_endpoint=_MINIO["endpoint"])
    yield st
    # MinIO lists multipart uploads per exact key only, so reclaim through the index.
    for up in du.open_uploads():
        st.abort_multipart_upload(up.key, up.upload_id)
        du.index_close_upload(up.key, up.upload_id)
    for obj in list(st.list_objects()):
        st.delete(obj.key)
    assert list(st.list_objects()) == [] and du.open_uploads() == []


class TestMinioIntegration:
    def test_presigned_multipart_roundtrip_from_host(self, minio_direct):
        st = minio_direct
        payload = os.urandom(11 * MB)
        key = source_object_key(str(uuid.uuid4()), 0, "roundtrip.xlsm")
        session, urls = plan_upload(st, job_id="j", file_idx=0, filename="roundtrip.xlsm", size=len(payload),
                                    user_id="u", queue="heavy", part_size=PART, url_ttl=300)
        key = session.source_key
        assert [u.upload_id for u in st.list_multipart_uploads(key)] == [session.upload_id]   # exact key
        assert list(st.list_multipart_uploads()) == []                                        # MinIO: no prefix listing
        assert [(u.key, u.upload_id) for u in du.open_uploads()] == [(key, session.upload_id)]  # hence the index
        for p in urls:                                      # "the browser": plain PUTs, no credentials
            n = p["part_number"]
            status, headers = _put(p["url"], payload[(n - 1) * PART: n * PART])
            assert status == 200 and headers.get("etag"), (n, status)
        parts = st.list_parts(key, session.upload_id)
        assert [(p.part_number, p.size) for p in parts] == [(1, PART), (2, PART), (3, MB)]
        info = finalize_upload(st, session)
        assert info.size == len(payload) and st.get_bytes(key) == payload
        assert list(st.list_multipart_uploads(key)) == [] and du.open_uploads() == []
        with pytest.raises(MultipartUploadNotFound):
            st.list_parts(key, session.upload_id)

    def test_wrong_part_size_detected_then_fixed(self, minio_direct):
        st = minio_direct
        payload = os.urandom(6 * MB)
        session, urls = plan_upload(st, job_id="j2", file_idx=0, filename="w.xlsm", size=len(payload),
                                    user_id="u", queue="heavy", part_size=PART, url_ttl=300)
        assert _put(urls[0]["url"], payload[:PART])[0] == 200
        assert _put(urls[1]["url"], payload[PART:PART + 10])[0] == 200        # short last part
        with pytest.raises(UploadIncomplete) as ei:
            finalize_upload(st, session)
        assert ei.value.extra == {"missing_parts": [], "wrong_parts": [2]}
        assert _put(urls[1]["url"], payload[PART:])[0] == 200                 # retry same URL
        assert finalize_upload(st, session).size == len(payload)

    def test_expired_url_is_rejected_and_refreshed(self, minio_direct):
        st = minio_direct
        uid = st.create_multipart_upload("exp_000_e.xlsm")
        url = st.presign_upload_part("exp_000_e.xlsm", uid, 1, expires_in=1)
        time.sleep(2.5)
        assert _put(url, b"x")[0] == 403
        fresh = st.presign_upload_part("exp_000_e.xlsm", uid, 1, expires_in=60)
        assert _put(fresh, b"x")[0] == 200
        assert st.abort_multipart_upload("exp_000_e.xlsm", uid) is True
        assert st.abort_multipart_upload("exp_000_e.xlsm", uid) is False

    def test_url_is_scoped_to_its_part_and_method(self, minio_direct):
        st = minio_direct
        uid = st.create_multipart_upload("scope_000_s.xlsm")
        url = st.presign_upload_part("scope_000_s.xlsm", uid, 1, expires_in=60)
        assert _put(url.replace("partNumber=1", "partNumber=2"), b"x")[0] == 403
        req = urllib.request.Request(url, method="GET")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=10)
        assert ei.value.code == 403
        st.abort_multipart_upload("scope_000_s.xlsm", uid)

    def test_cleanup_reclaims_abandoned_upload(self, minio_direct, _isolate):
        from spir_dynamic.tasks.cleanup_tasks import _purge_stale_multipart_uploads
        st = minio_direct
        session, _ = plan_upload(st, job_id="aband", file_idx=0, filename="a.xlsm", size=MB, user_id="",
                                 queue="heavy", part_size=PART, url_ttl=60)
        with patch("spir_dynamic.monitoring.metrics.CLEANUP_DELETED_FILES"):
            recent = _purge_stale_multipart_uploads(st, stale_hours=24, dry_run=False)
            assert recent["aborted"] == 0 and recent["skipped_recent"] == 1        # just started: kept
            assert list(st.list_multipart_uploads(session.source_key))
            _isolate.hset(du._INDEX_KEY, f"{session.source_key}\n{session.upload_id}", str(time.time() - 30 * 3600))
            stale = _purge_stale_multipart_uploads(st, stale_hours=24, dry_run=False)
        assert stale["aborted"] == 1
        assert list(st.list_multipart_uploads(session.source_key)) == [] and du.open_uploads() == []

    def test_scoped_uploader_cannot_write_outside_batch_uploads(self, minio_direct):
        creds = _uploader_creds()
        if creds is None:
            pytest.skip("no MINIO_PRESIGN_* in compose config")
        ak, sk = creds
        inside = MinioObjectStorage(**_MINIO, prefix=minio_direct.prefix, public_endpoint=_MINIO["endpoint"],
                                    presign_access_key=ak, presign_secret_key=sk)
        uid = inside.create_multipart_upload("ok_000_o.xlsm")
        assert _put(inside.presign_upload_part("ok_000_o.xlsm", uid, 1, expires_in=60), b"x")[0] == 200
        inside.abort_multipart_upload("ok_000_o.xlsm", uid)

        outside = MinioObjectStorage(**_MINIO, prefix="_tests/direct_upload_outside/", public_endpoint=_MINIO["endpoint"],
                                     presign_access_key=ak, presign_secret_key=sk)
        uid2 = outside.create_multipart_upload("no_000_n.xlsm")               # root creates it
        try:
            assert _put(outside.presign_upload_part("no_000_n.xlsm", uid2, 1, expires_in=60), b"x")[0] == 403
            # and the uploader can neither read nor delete anything
            ro = MinioObjectStorage(endpoint=_MINIO["endpoint"], access_key=ak, secret_key=sk,
                                    bucket=_MINIO["bucket"], prefix="")
            with pytest.raises(StorageUnavailable):
                list(ro.list_objects())
        finally:
            outside.abort_multipart_upload("no_000_n.xlsm", uid2)


# ── Docker end-to-end ────────────────────────────────────────────────────────

API = os.environ.get("SPIR_E2E_API", "http://localhost:8000")
FRONTEND_ORIGIN = "http://localhost:3000"
WORKBOOK_223 = REPO_ROOT / "templates" / "inputs" / "VEN-4460-DGEN-5-43-0001-1.xlsm"
WORKBOOK_SMALL = REPO_ROOT / "templates" / "inputs" / "15.VEN-4142-RLCSF3-4-43-0500-A.xlsx"
EXPECTED_223 = {"total_rows": 1077, "total_tags": 381}


def _health() -> dict | None:
    try:
        with urllib.request.urlopen(f"{API}/api/health", timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _stack_running() -> bool:
    if _MINIO is None:
        return False
    try:
        r = subprocess.run(["docker", "compose", "ps", "--format", "json", "--status", "running"],
                           cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        names = {json.loads(line)["Service"] for line in r.stdout.splitlines() if line.strip()}
    except Exception:
        return False
    if not {"api", "minio", "redis", "worker-heavy"} <= names:
        return False
    h = _health() or {}
    return h.get("upload_storage_backend") == "minio" and h.get("direct_upload") == "enabled"


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


def _json(method, path, token, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    st, h, raw = _http(method, path, token=token, body=body, content_type="application/json" if body else None)
    try:
        return st, json.loads(raw)
    except Exception:
        return st, {"raw": raw[:300]}


def _api_rx_bytes() -> int:
    r = subprocess.run(["docker", "compose", "exec", "-T", "api", "cat", "/proc/net/dev"],
                       cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    total = 0
    for line in r.stdout.splitlines():
        if ":" in line and not line.strip().startswith("lo:"):
            total += int(line.split(":", 1)[1].split()[0])
    return total


def _worker_logs(service: str, since: str) -> str:
    r = subprocess.run(["docker", "compose", "logs", "--no-log-prefix", "--since", since, service],
                       cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    return r.stdout + r.stderr


@pytest.fixture(scope="module")
def e2e_user():
    """A throwaway login in the Docker DB (same pattern as the Phase 3C suite)."""
    if not _stack_running():
        pytest.skip("SPIR docker compose stack with direct upload enabled is not running")
    from tests.test_source_objects import _USER_SQL_CREATE, _USER_SQL_DELETE
    user_id, username, password = str(uuid.uuid4()), f"e2e_3d_{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    r = subprocess.run(["docker", "compose", "exec", "-T", "api", "python", "-c", _USER_SQL_CREATE,
                        user_id, username, password], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr
    st, _, body = _http("POST", "/api/login", body=f"username={username}&password={password}".encode(),
                        content_type="application/x-www-form-urlencoded")
    assert st == 200, body
    token = json.loads(body)["access_token"]
    yield SimpleNamespace(id=user_id, username=username, token=token)
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
        st, data = _json("GET", f"/api/batch/{job_id}/result", token)
        assert st == 200, data
        if data["status"] != "processing":
            return data
        time.sleep(2)
    raise AssertionError("timed out waiting for the worker")


@pytest.mark.skipif(not _stack_running(), reason="SPIR docker compose stack with direct upload enabled is not running")
class TestDockerEndToEnd:
    def test_compose_wiring(self):
        env = _COMPOSE["services"]["api"]["environment"]
        assert env["MINIO_ENDPOINT"] == "http://minio:9000"                     # container address untouched
        assert env["MINIO_PUBLIC_ENDPOINT"].startswith("http://localhost:9000")
        assert env["MINIO_PRESIGN_ACCESS_KEY"] != env["MINIO_ACCESS_KEY"]        # scoped signer, not root
        assert env["LARGE_FILE_THRESHOLD_MB"] == "100" and env["GIANT_FILE_THRESHOLD_MB"] == "500"
        assert _COMPOSE["services"]["minio"]["environment"]["MINIO_API_CORS_ALLOW_ORIGIN"] == FRONTEND_ORIGIN
        assert _COMPOSE["services"]["api"]["ports"][0]["published"] == "8000"
        assert _COMPOSE["services"]["frontend"]["ports"][0]["published"] == "3000"
        assert _COMPOSE["services"]["minio"]["ports"][0]["host_ip"] == "127.0.0.1"

    def test_minio_cors_restricted_to_frontend_origin(self):
        def preflight(origin):
            req = urllib.request.Request(f"{_MINIO['endpoint']}/spir-files/batch_uploads/x", method="OPTIONS")
            req.add_header("Origin", origin)
            req.add_header("Access-Control-Request-Method", "PUT")
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, {k.lower(): v for k, v in r.headers.items()}
            except urllib.error.HTTPError as e:
                return e.code, {k.lower(): v for k, v in e.headers.items()}
        st, h = preflight(FRONTEND_ORIGIN)
        assert st in (200, 204) and h.get("access-control-allow-origin") == FRONTEND_ORIGIN
        assert "PUT" in h.get("access-control-allow-methods", "")
        st, h = preflight("http://evil.example")
        assert "access-control-allow-origin" not in h

    def test_small_file_still_synchronous(self, e2e_user):
        st, data = _json("POST", "/api/uploads/initiate", e2e_user.token,
                         {"filename": WORKBOOK_SMALL.name, "size": WORKBOOK_SMALL.stat().st_size})
        assert st == 200 and data == {"mode": "api", "reason": "small_file", "queue": "normal"}
        from tests.test_source_objects import _multipart
        body, ctype = _multipart({}, "file", WORKBOOK_SMALL.name, WORKBOOK_SMALL)
        st, _, resp = _http("POST", "/api/extract", token=e2e_user.token, body=body, content_type=ctype)
        assert st == 200 and json.loads(resp)["status"] == "done", resp[:300]

    def test_223mb_direct_upload_end_to_end(self, e2e_user):
        assert WORKBOOK_223.exists() and WORKBOOK_223.stat().st_size > 100 * MB
        size = WORKBOOK_223.stat().st_size
        since = "60m"
        rx_before = _api_rx_bytes()
        t0 = time.perf_counter()

        # 1. small control request
        st, plan = _json("POST", "/api/uploads/initiate", e2e_user.token, {"filename": WORKBOOK_223.name, "size": size})
        assert st == 200 and plan["mode"] == "direct" and plan["queue"] == "heavy", plan
        job_id = plan["job_id"]
        key = source_object_key(job_id, 0, WORKBOOK_223.name)
        assert plan["part_count"] == part_count(size, 16 * MB) == 14
        for p in plan["parts"]:
            assert p["url"].startswith("http://localhost:9000/spir-files/batch_uploads/")
            assert "X-Amz-Credential=spir_uploader" in p["url"] and "spir_minio" not in p["url"]

        # 2. the file goes to MinIO, not to the API
        t_up = time.perf_counter()
        with WORKBOOK_223.open("rb") as fh:
            for p in plan["parts"]:
                fh.seek((p["part_number"] - 1) * plan["part_size"])
                status, _ = _put(p["url"], fh.read(plan["part_size"]))
                assert status == 200, (p["part_number"], status)
        upload_s = time.perf_counter() - t_up

        # 3. small control request -> server verification -> worker
        st, done = _json("POST", f"/api/uploads/{job_id}/0/complete", e2e_user.token)
        assert st == 202 and done["status"] == "queued" and done["queue"] == "heavy", done
        assert _json("POST", f"/api/uploads/{job_id}/0/complete", e2e_user.token)[0] == 409   # duplicate

        result = _poll_result(e2e_user.token, job_id)
        total_s = time.perf_counter() - t0
        assert result["status"] == "done", result
        assert (result["total_rows"], result["total_tags"]) == (EXPECTED_223["total_rows"], EXPECTED_223["total_tags"])

        # 4. FastAPI did not carry the body: its container received far less than the file
        rx_delta = _api_rx_bytes() - rx_before
        assert rx_delta < 20 * MB, f"api container received {rx_delta / MB:.1f} MB during a {size / MB:.0f} MB upload"
        print(f"\n[3D E2E] upload {size / MB:.1f} MB direct to MinIO in {upload_s:.1f}s; "
              f"api RX during flow {rx_delta / MB:.1f} MB; total to result {total_s:.1f}s")

        # 5. same worker path as Phase 3C: staged from MinIO, extracted, object gone
        logs = _worker_logs("worker-heavy", since)
        assert "source.staged" in logs and key in logs and "extraction.complete" in logs
        api_logs = _worker_logs("api", since)
        assert "upload.initiated" in api_logs and "upload.completed" in api_logs and key in api_logs
        assert "X-Amz-Signature" not in api_logs                                   # URLs never logged
        bucket = MinioObjectStorage(**_MINIO, prefix="batch_uploads/")
        assert not bucket.exists(key)
        assert list(bucket.list_multipart_uploads(key)) == []

        # 6. history + download
        st, _, hist = _http("GET", "/api/history", token=e2e_user.token)
        assert st == 200 and [h for h in json.loads(hist) if h["file_id"] == result["file_id"]]
        st, headers, data = _http("GET", f"/api/download/{result['file_id']}", token=e2e_user.token)
        assert st == 200 and data[:2] == b"PK" and "attachment" in headers.get("content-disposition", "")

    def test_abort_leaves_nothing_behind(self, e2e_user):
        size = WORKBOOK_223.stat().st_size
        st, plan = _json("POST", "/api/uploads/initiate", e2e_user.token, {"filename": "abort.xlsm", "size": size})
        assert st == 200 and plan["mode"] == "direct"
        with WORKBOOK_223.open("rb") as fh:
            assert _put(plan["parts"][0]["url"], fh.read(plan["part_size"]))[0] == 200
        st, body = _json("DELETE", f"/api/uploads/{plan['job_id']}/0", e2e_user.token)
        assert st == 200 and body["status"] == "aborted"
        st, res = _json("GET", f"/api/batch/{plan['job_id']}/result", e2e_user.token)
        assert res["status"] == "error" and "cancelled" in res["error"]
        bucket = MinioObjectStorage(**_MINIO, prefix="batch_uploads/")
        key = source_object_key(plan["job_id"], 0, "abort.xlsm")
        assert not bucket.exists(key) and list(bucket.list_multipart_uploads(key)) == []

    def test_no_stray_test_state_left(self):
        bucket = MinioObjectStorage(**_MINIO, prefix="batch_uploads/")
        assert not [o.key for o in bucket.list_objects() if "e2e" in o.key or "_tests/" in o.key]
