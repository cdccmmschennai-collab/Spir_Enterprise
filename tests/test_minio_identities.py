"""
MinIO source-object lifecycle: separate backend and browser identities.

Production ran the API and the workers under the *uploader* credential — the
one whose policy grants PutObject on batch_uploads/* and nothing else — so
every DeleteObject after a successful extraction came back AccessDenied and
8 GB of processed source objects piled up in the bucket. Two things had to be
true and were not: the server has to sign its own calls with an identity that
may delete, and the nightly sweep that catches the leftovers has to reach a
worker at all (Beat published it to the default "celery" queue, which no
deployed worker consumes).

What is pinned here:

  Settings            MINIO_BACKEND_* is the server-side identity,
                      MINIO_PRESIGN_* the browser's; both fall back to the
                      single MINIO_* pair; the pair rule; minio_configured.

  Storage wiring      server-side calls (delete included) are signed with the
                      backend credential, presigned URLs with the presign one;
                      a backend identity + a public endpoint with no presign
                      credential is refused instead of handing the browser a
                      delete-capable key id; no secret in the repr.

  Deletion            discard_source_object deletes through the backend
                      credential, and a refused delete is counted, logged and
                      returned as False WITHOUT turning a successful
                      extraction into a failed one.

  Stale sweep         dry-run deletes nothing; a live run deletes only what is
                      past the stale threshold; recent objects survive; a
                      denied delete is reported rather than read as "nothing
                      to do".

  Scheduling          lifecycle_cleanup routes to CLEANUP_QUEUE, the Beat entry
                      carries the same queue, and a worker in the deployed
                      configuration consumes it.

Run:  pytest tests/test_minio_identities.py -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import IO, ClassVar, Iterator
from unittest.mock import MagicMock, patch

import pytest

from spir_dynamic.app.config import Settings
from spir_dynamic.services.object_storage import (
    MinioObjectStorage,
    ObjectInfo,
    ObjectNotFound,
    StorageConfigError,
    StorageUnavailable,
    normalize_key,
    reset_object_storage,
)
from spir_dynamic.services.source_objects import discard_source_object

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_PREFIX_ROOT = "_tests/minio_identities/"

_MINIO_ENV = (
    "MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_BUCKET", "MINIO_SECURE",
    "MINIO_BACKEND_ACCESS_KEY", "MINIO_BACKEND_SECRET_KEY",
    "MINIO_PRESIGN_ACCESS_KEY", "MINIO_PRESIGN_SECRET_KEY", "MINIO_PUBLIC_ENDPOINT",
    "STORAGE_BACKEND", "UPLOAD_STORAGE_BACKEND", "CLEANUP_QUEUE", "CLEANUP_DRY_RUN",
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Neither the Compose env nor a cached backend from another test may leak in."""
    for k in _MINIO_ENV:
        monkeypatch.delenv(k, raising=False)
    reset_object_storage()
    yield
    reset_object_storage()


def _settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


_BASE = {"minio_endpoint": "http://minio:9000", "minio_bucket": "spir-files"}
_SPLIT = {
    **_BASE,
    "minio_backend_access_key": "spir_backend", "minio_backend_secret_key": "backend-secret",
    "minio_presign_access_key": "spir_uploader", "minio_presign_secret_key": "uploader-secret",
}


# ── Test doubles ─────────────────────────────────────────────────────────────

class RecordingS3:
    """Stands in for a boto3 S3 client and remembers which credential built it."""

    def __init__(self, endpoint_url: str, access_key: str, secret_key: str, calls: list) -> None:
        self.endpoint_url = endpoint_url
        self.access_key = access_key
        self.secret_key = secret_key
        self._calls = calls
        self.deny: set[str] = set()

    def _record(self, op: str, **kw):
        self._calls.append(SimpleNamespace(op=op, access_key=self.access_key,
                                           endpoint=self.endpoint_url, kwargs=kw))
        if op in self.deny:
            raise _access_denied(op)

    def head_object(self, **kw):
        self._record("head_object", **kw)
        return {"ContentLength": 3, "LastModified": datetime.now(timezone.utc), "ContentType": "x/y"}

    def delete_object(self, **kw):
        self._record("delete_object", **kw)
        return {}

    def generate_presigned_url(self, ClientMethod, Params, ExpiresIn, HttpMethod):   # noqa: N803 - boto3 spelling
        self._record("presign", client_method=ClientMethod, params=Params)
        return f"{self.endpoint_url}/{Params['Bucket']}/{Params['Key']}?X-Amz-Credential={self.access_key}"


def _access_denied(op: str) -> Exception:
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "Access Denied."}}, op)


@contextlib.contextmanager
def recording_clients(storage: MinioObjectStorage, deny: set[str] | None = None):
    """Replace boto3 client construction; yields the list of calls made, with the signing key id."""
    calls: list = []

    def make(endpoint_url, access_key, secret_key):
        client = RecordingS3(endpoint_url, access_key, secret_key, calls)
        client.deny = set(deny or ())
        return client

    with patch.object(MinioObjectStorage, "_make_client", side_effect=make, autospec=False):
        yield calls


class MemoryObjectStorage:
    """Minimal non-filesystem ObjectStorage for the cleanup sweep (no path_for)."""

    backend: ClassVar[str] = "memory"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.mtimes: dict[str, datetime] = {}
        self.deny_delete: set[str] = set()

    def _info(self, key: str) -> ObjectInfo:
        return ObjectInfo(key=key, size=len(self.objects[key]), last_modified=self.mtimes[key])

    def put(self, key: str, data: bytes, age_hours: float = 0.0) -> None:
        normalize_key(key)
        self.objects[key] = data
        self.mtimes[key] = datetime.now(timezone.utc) - timedelta(hours=age_hours)

    def put_bytes(self, key, data, *, content_type=None):
        self.put(key, bytes(data))
        return self._info(key)

    def put_file(self, key, source, *, content_type=None):
        return self.put_bytes(key, Path(source).read_bytes())

    def get_bytes(self, key):
        if key not in self.objects:
            raise ObjectNotFound(key)
        return self.objects[key]

    def get_file(self, key, dest):
        Path(dest).write_bytes(self.get_bytes(key))
        return Path(dest)

    def open_read(self, key) -> IO[bytes]:
        return io.BytesIO(self.get_bytes(key))

    def exists(self, key):
        return key in self.objects

    def delete(self, key):
        if key in self.deny_delete:
            raise StorageUnavailable("AccessDenied: DeleteObject")
        self.mtimes.pop(key, None)
        return self.objects.pop(key, None) is not None

    def stat(self, key):
        if key not in self.objects:
            raise ObjectNotFound(key)
        return self._info(key)

    def list_objects(self, prefix="") -> Iterator[ObjectInfo]:
        for key in list(self.objects):
            if key.startswith(prefix):
                yield self._info(key)

    def ping(self):
        return None


# ── Settings: which credential is which ──────────────────────────────────────

class TestIdentitySettings:
    def test_single_identity_is_still_the_default(self):
        """A deployment that only ever set MINIO_ACCESS_KEY keeps working, unchanged."""
        s = _settings(**_BASE, minio_access_key="root", minio_secret_key="root-secret")
        assert s.minio_server_access_key == "root" and s.minio_browser_access_key == "root"
        assert s.minio_server_secret_key == "root-secret" and s.minio_browser_secret_key == "root-secret"
        assert s.minio_identities_separated is False
        assert s.minio_configured is True

    def test_backend_key_takes_over_the_server_side_only(self):
        s = _settings(**_SPLIT)
        assert s.minio_server_access_key == "spir_backend"
        assert s.minio_server_secret_key == "backend-secret"
        assert s.minio_browser_access_key == "spir_uploader"
        assert s.minio_browser_secret_key == "uploader-secret"
        assert s.minio_identities_separated is True

    def test_backend_key_does_not_displace_the_presign_key(self):
        """The browser must never inherit the server identity just because one exists."""
        s = _settings(**_SPLIT)
        assert s.minio_browser_access_key != s.minio_server_access_key

    def test_backend_credentials_come_in_pairs(self):
        with pytest.raises(ValueError):
            _settings(**_BASE, minio_backend_access_key="spir_backend")
        with pytest.raises(ValueError):
            _settings(**_BASE, minio_backend_secret_key="backend-secret")

    def test_backend_credentials_alone_are_a_complete_configuration(self):
        """No legacy MINIO_ACCESS_KEY needed once the dedicated identity is configured."""
        s = _settings(**_BASE, minio_backend_access_key="spir_backend",
                      minio_backend_secret_key="backend-secret")
        assert s.minio_configured is True
        assert s.minio_access_key == "" and s.minio_server_access_key == "spir_backend"

    def test_env_vars_reach_the_settings(self, monkeypatch):
        monkeypatch.setenv("MINIO_BACKEND_ACCESS_KEY", "be")
        monkeypatch.setenv("MINIO_BACKEND_SECRET_KEY", "bs")
        monkeypatch.setenv("MINIO_PRESIGN_ACCESS_KEY", "up")
        monkeypatch.setenv("MINIO_PRESIGN_SECRET_KEY", "us")
        s = Settings(_env_file=None, **_BASE)
        assert (s.minio_server_access_key, s.minio_browser_access_key) == ("be", "up")

    def test_endpoint_alone_is_not_configured(self):
        assert _settings(**_BASE).minio_configured is False


# ── Storage: which credential signs which call ───────────────────────────────

class TestStorageIdentityWiring:
    def test_from_settings_binds_each_identity_to_its_audience(self):
        st = MinioObjectStorage.from_settings(
            _settings(**_SPLIT, minio_public_endpoint="https://files.example.com"),
            prefix="batch_uploads/",
        )
        assert st.access_key_id == "spir_backend"
        assert st.presign_key_id == "spir_uploader"

    def test_server_side_delete_is_signed_with_the_backend_credential(self):
        st = MinioObjectStorage.from_settings(_settings(**_SPLIT), prefix="batch_uploads/")
        with recording_clients(st) as calls:
            assert st.delete("job-1_000_a.xlsm") is True
        # the HEAD probe and the DELETE itself both go out as the backend user,
        # against the container-side endpoint
        assert [c.op for c in calls] == ["head_object", "delete_object"]
        assert {c.access_key for c in calls} == {"spir_backend"}
        assert {c.endpoint for c in calls} == {"http://minio:9000"}
        assert calls[-1].kwargs["Key"] == "batch_uploads/job-1_000_a.xlsm"
        assert calls[-1].kwargs["Bucket"] == "spir-files"

    def test_presigned_url_is_signed_with_the_browser_credential(self):
        st = MinioObjectStorage.from_settings(
            _settings(**_SPLIT, minio_public_endpoint="https://files.example.com"),
            prefix="batch_uploads/",
        )
        with recording_clients(st) as calls:
            url = st.presign_upload_part("job-1_000_a.xlsm", "UP1", 1, expires_in=60)
        assert [c.op for c in calls] == ["presign"]
        assert calls[0].access_key == "spir_uploader"           # never the backend identity
        assert calls[0].endpoint == "https://files.example.com"  # browser-reachable host
        assert "spir_backend" not in url

    def test_both_clients_coexist_without_crossing_over(self):
        st = MinioObjectStorage.from_settings(
            _settings(**_SPLIT, minio_public_endpoint="https://files.example.com"),
            prefix="batch_uploads/",
        )
        with recording_clients(st) as calls:
            st.presign_upload_part("k.xlsm", "UP1", 1, expires_in=60)
            st.delete("k.xlsm")
        by_op = {c.op: c.access_key for c in calls}
        assert by_op["presign"] == "spir_uploader"
        assert by_op["delete_object"] == "spir_backend"

    def test_backend_identity_without_a_presign_identity_is_refused(self):
        """Rather than silently signing browser URLs with the delete-capable key."""
        cfg = _settings(**_BASE, minio_backend_access_key="spir_backend",
                        minio_backend_secret_key="backend-secret",
                        minio_public_endpoint="https://files.example.com")
        with pytest.raises(StorageConfigError) as exc:
            MinioObjectStorage.from_settings(cfg, prefix="batch_uploads/")
        assert "MINIO_PRESIGN_ACCESS_KEY" in str(exc.value)

    def test_backend_identity_equal_to_the_presign_identity_is_refused(self):
        cfg = _settings(**_BASE, minio_backend_access_key="same",
                        minio_backend_secret_key="s",
                        minio_presign_access_key="same", minio_presign_secret_key="s",
                        minio_public_endpoint="https://files.example.com")
        with pytest.raises(StorageConfigError):
            MinioObjectStorage.from_settings(cfg, prefix="batch_uploads/")

    def test_no_public_endpoint_means_no_browser_identity_to_protect(self):
        """Without direct upload nothing is presigned, so a backend-only config is fine."""
        cfg = _settings(**_BASE, minio_backend_access_key="spir_backend",
                        minio_backend_secret_key="backend-secret")
        st = MinioObjectStorage.from_settings(cfg, prefix="batch_uploads/")
        assert st.access_key_id == "spir_backend" and st.supports_direct_upload() is False

    def test_repr_carries_key_ids_but_no_secret(self):
        st = MinioObjectStorage.from_settings(
            _settings(**_SPLIT, minio_public_endpoint="https://files.example.com"),
            prefix="batch_uploads/",
        )
        text = repr(st)
        assert "spir_backend" in text and "spir_uploader" in text
        assert "backend-secret" not in text and "uploader-secret" not in text


# ── Immediate deletion after a successful extraction ─────────────────────────

class TestSourceDeletion:
    KEY = "job-1_000_big.xlsm"

    def test_delete_succeeds_when_the_credential_may_delete(self):
        st = MinioObjectStorage.from_settings(_settings(**_SPLIT), prefix="batch_uploads/")
        with recording_clients(st) as calls:
            assert discard_source_object(self.KEY, log_context="job=1", storage=st) is True
        assert calls[-1].op == "delete_object" and calls[-1].access_key == "spir_backend"

    def test_denied_delete_is_counted_and_logged_and_never_raises(self):
        """The production symptom: DeleteObject -> AccessDenied. It must be loud, not fatal."""
        st = MinioObjectStorage.from_settings(_settings(**_SPLIT), prefix="batch_uploads/")
        with recording_clients(st, deny={"delete_object"}), \
             patch("spir_dynamic.monitoring.metrics.SOURCE_DELETE_FAILURES") as metric, \
             patch("spir_dynamic.services.source_objects.log") as logger:
            assert discard_source_object(self.KEY, log_context="job=1 idx=0", storage=st) is False
        metric.labels.assert_called_once_with(stage="task")
        metric.labels.return_value.inc.assert_called_once()
        logger.error.assert_called_once()
        event, kwargs = logger.error.call_args.args[0], logger.error.call_args.kwargs
        assert event == "source.delete_failed"
        assert kwargs["key"] == self.KEY and kwargs["context"] == "job=1 idx=0"
        assert "Access Denied" in kwargs["exc_message"]   # the object is identifiable from the log alone

    def test_missing_object_is_not_a_failure(self):
        st = MemoryObjectStorage()
        with patch("spir_dynamic.monitoring.metrics.SOURCE_DELETE_FAILURES") as metric:
            assert discard_source_object("gone_000_x.xlsm", storage=st) is False
        metric.labels.assert_not_called()

    def test_failed_cleanup_does_not_fail_a_successful_extraction(self, tmp_path):
        """Rows were extracted; a bucket that refuses the delete does not undo that."""
        from tests.test_source_objects import _RESULT, _slot_updates, worker_ctx

        st = MemoryObjectStorage()
        st.put(self.KEY, b"workbook")
        st.deny_delete.add(self.KEY)

        with patch("spir_dynamic.monitoring.metrics.SOURCE_DELETE_FAILURES"):
            with worker_ctx(tmp_path, st, pipeline=MagicMock(return_value=dict(_RESULT))) as (task, store, _):
                out = task.run(job_id="job-1", file_idx=0, source_key=self.KEY,
                               filename="big.xlsm", user_id="u")

        assert out["status"] == "ok" and out["total_rows"] == 7
        assert [u.status for u in _slot_updates(store)] == ["running", "ok"]
        # the source survived the refused delete and is now the stale sweep's problem
        assert self.KEY in st.objects


# ── Scheduled stale sweep ────────────────────────────────────────────────────

class TestStaleSweep:
    def _storage(self) -> MemoryObjectStorage:
        st = MemoryObjectStorage()
        st.put("stale_000_a.xlsm", b"1" * 2048, age_hours=30)   # past the 24 h threshold
        st.put("mid_000_b.xlsm", b"1" * 512, age_hours=5)       # orphan, but not stale yet
        st.put("fresh_000_c.xlsm", b"1" * 256, age_hours=0)     # inside the 1 h guard
        return st

    def test_dry_run_deletes_nothing(self):
        from spir_dynamic.tasks.cleanup_tasks import _purge_stale_source_objects
        st = self._storage()
        with patch("spir_dynamic.monitoring.metrics.CLEANUP_DELETED_FILES"):
            result = _purge_stale_source_objects(st, stale_hours=24, dry_run=True)
        assert result["deleted"] == 0 and result["failed"] == 0
        assert len(st.objects) == 3

    def test_live_run_deletes_only_what_is_past_the_threshold(self):
        from spir_dynamic.tasks.cleanup_tasks import _purge_stale_source_objects
        st = self._storage()
        with patch("spir_dynamic.monitoring.metrics.CLEANUP_DELETED_FILES") as counter:
            result = _purge_stale_source_objects(st, stale_hours=24, dry_run=False)
        assert result["deleted"] == 1 and result["failed"] == 0
        assert result["skipped_recent"] == 1          # the one inside the 1 h guard
        assert set(st.objects) == {"mid_000_b.xlsm", "fresh_000_c.xlsm"}
        counter.labels.assert_called_with(reason="stale_upload")

    def test_an_active_upload_is_never_selected(self):
        """A file being extracted right now is minutes old; the guard and the threshold both protect it."""
        from spir_dynamic.tasks.cleanup_tasks import _purge_stale_source_objects
        st = MemoryObjectStorage()
        st.put("running_000_now.xlsm", b"x", age_hours=0.05)
        st.put("queued_000_soon.xlsm", b"x", age_hours=3)
        with patch("spir_dynamic.monitoring.metrics.CLEANUP_DELETED_FILES"):
            result = _purge_stale_source_objects(st, stale_hours=24, dry_run=False)
        assert result["deleted"] == 0
        assert set(st.objects) == {"running_000_now.xlsm", "queued_000_soon.xlsm"}

    def test_denied_delete_is_reported_and_does_not_stop_the_sweep(self):
        from spir_dynamic.tasks.cleanup_tasks import _purge_stale_source_objects
        st = MemoryObjectStorage()
        st.put("denied_000_a.xlsm", b"x", age_hours=40)
        st.put("allowed_000_b.xlsm", b"x", age_hours=40)
        st.deny_delete.add("denied_000_a.xlsm")

        with patch("spir_dynamic.monitoring.metrics.CLEANUP_DELETED_FILES"), \
             patch("spir_dynamic.monitoring.metrics.SOURCE_DELETE_FAILURES") as metric:
            result = _purge_stale_source_objects(st, stale_hours=24, dry_run=False)

        assert result["deleted"] == 1 and result["failed"] == 1
        assert set(st.objects) == {"denied_000_a.xlsm"}
        metric.labels.assert_called_with(stage="cleanup")

    def test_task_honours_cleanup_dry_run_from_config(self, tmp_path):
        """CLEANUP_DRY_RUN=true wins even when the Beat entry asks for a live run."""
        from spir_dynamic.tasks import cleanup_tasks as ct

        rows, uploads = tmp_path / "rows", tmp_path / "uploads"
        rows.mkdir()
        uploads.mkdir()
        st = self._storage()
        cfg = SimpleNamespace(
            rows_storage_path=str(rows), batch_upload_dir=str(uploads),
            cleanup_json_retention_days=14, cleanup_upload_stale_hours=24,
            cleanup_dry_run=True, database_url="",
        )
        with patch("spir_dynamic.app.config.get_settings", return_value=cfg), \
             patch.object(ct, "_source_object_storage", return_value=st), \
             patch("spir_dynamic.monitoring.metrics.CLEANUP_DELETED_FILES"):
            summary = ct.lifecycle_cleanup_task.run(dry_run=False)

        assert summary["dry_run"] is True
        assert summary["phases"]["stale_uploads"]["deleted"] == 0
        assert len(st.objects) == 3

    def test_task_deletes_stale_objects_when_dry_run_is_off(self, tmp_path):
        from spir_dynamic.tasks import cleanup_tasks as ct

        rows, uploads = tmp_path / "rows", tmp_path / "uploads"
        rows.mkdir()
        uploads.mkdir()
        st = self._storage()
        cfg = SimpleNamespace(
            rows_storage_path=str(rows), batch_upload_dir=str(uploads),
            cleanup_json_retention_days=14, cleanup_upload_stale_hours=24,
            cleanup_dry_run=False, database_url="",
        )
        with patch("spir_dynamic.app.config.get_settings", return_value=cfg), \
             patch.object(ct, "_source_object_storage", return_value=st), \
             patch("spir_dynamic.monitoring.metrics.CLEANUP_DELETED_FILES"):
            summary = ct.lifecycle_cleanup_task.run(dry_run=False)

        assert summary["dry_run"] is False
        phase = summary["phases"]["stale_uploads"]
        assert phase["deleted"] == 1 and phase["failed"] == 0 and phase["backend"] == "memory"
        assert "stale_000_a.xlsm" not in st.objects


# ── The sweep has to reach a worker at all ───────────────────────────────────

class TestCleanupRouting:
    TASK = "spir_dynamic.tasks.lifecycle_cleanup"

    def test_default_queue_is_one_the_workers_read(self):
        assert _settings().cleanup_queue == "normal"

    def test_cleanup_queue_is_configurable(self, monkeypatch):
        monkeypatch.setenv("CLEANUP_QUEUE", "maintenance")
        assert Settings(_env_file=None).cleanup_queue == "maintenance"

    def test_empty_cleanup_queue_is_rejected(self):
        with pytest.raises(ValueError):
            _settings(cleanup_queue="  ")

    def test_task_is_routed_away_from_the_unconsumed_default_queue(self):
        from spir_dynamic.app.config import get_settings
        from spir_dynamic.celery_app import celery_app

        expected = get_settings().cleanup_queue
        assert celery_app.conf.task_routes[self.TASK] == {"queue": expected}
        resolved = celery_app.amqp.router.route({}, self.TASK)
        assert resolved["queue"].name == expected != "celery"

    def test_beat_entry_carries_the_same_queue(self):
        from spir_dynamic.app.config import get_settings
        from spir_dynamic.celery_app import celery_app

        entry = celery_app.conf.beat_schedule["lifecycle-cleanup-daily"]
        assert entry["task"] == self.TASK
        assert entry["options"]["queue"] == get_settings().cleanup_queue

    def test_extraction_routing_is_untouched(self):
        """batch_router still picks the queue per file size; only cleanup got a route."""
        from spir_dynamic.celery_app import celery_app
        assert list(celery_app.conf.task_routes) == [self.TASK]

    def test_a_compose_worker_consumes_the_cleanup_queue(self):
        """The deployed topology, not just the route: some worker must read that queue."""
        from spir_dynamic.app.config import get_settings

        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        queues: set[str] = set()
        lines = compose.splitlines()
        for i, line in enumerate(lines):
            if line.strip() == "- -Q" and i + 1 < len(lines):
                queues.update(q.strip() for q in lines[i + 1].strip().lstrip("- ").split(","))
        assert queues, "no -Q arguments found in docker-compose.yml"
        assert get_settings().cleanup_queue in queues


# ── Integration: the real bucket, when the Compose stack is up ───────────────

def _compose_env() -> dict | None:
    """The api service's resolved environment, or None when Compose is unavailable."""
    if shutil.which("docker") is None:
        return None
    try:
        r = subprocess.run(["docker", "compose", "config", "--format", "json"],
                           cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)["services"]["api"]["environment"]
    except (KeyError, ValueError):
        return None


def _minio_live(endpoint: str) -> bool:
    try:
        with urllib.request.urlopen(endpoint.rstrip("/") + "/minio/health/live", timeout=3) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


_ENV = _compose_env()
_HOST_ENDPOINT = "http://127.0.0.1:9000"
_LIVE = bool(_ENV) and _minio_live(_HOST_ENDPOINT)
minio_required = pytest.mark.skipif(not _LIVE, reason="local MinIO (Compose stack) is not reachable")


class _AgedListing:
    """
    A real backend whose listing reports every object as `hours` old. Only
    last_modified is faked; delete() and backend go straight through, so the
    sweep's deletion is the genuine one against MinIO.
    """

    def __init__(self, storage, *, hours: float) -> None:
        self._storage = storage
        self._hours = hours

    @property
    def backend(self) -> str:
        return self._storage.backend

    def list_objects(self, prefix: str = ""):
        shift = timedelta(hours=self._hours)
        for obj in self._storage.list_objects(prefix):
            yield ObjectInfo(key=obj.key, size=obj.size,
                             last_modified=obj.last_modified - shift,
                             content_type=obj.content_type)

    def delete(self, key: str) -> bool:
        return self._storage.delete(key)


def _identity_storage(which: str, prefix: str) -> MinioObjectStorage:
    """A backend bound to one of the two provisioned identities, scoped to a test prefix."""
    assert _ENV is not None
    ak = _ENV["MINIO_BACKEND_ACCESS_KEY"] if which == "backend" else _ENV["MINIO_PRESIGN_ACCESS_KEY"]
    sk = _ENV["MINIO_BACKEND_SECRET_KEY"] if which == "backend" else _ENV["MINIO_PRESIGN_SECRET_KEY"]
    return MinioObjectStorage(endpoint=_HOST_ENDPOINT, access_key=ak, secret_key=sk,
                              bucket=_ENV.get("MINIO_BUCKET", "spir-files"), prefix=prefix)


@minio_required
class TestProvisionedIdentities:
    """
    Exercises the two MinIO users minio-init creates, under a throwaway prefix
    inside batch_uploads/ — the application's own objects are never touched.
    """

    @pytest.fixture
    def prefix(self) -> str:
        return f"batch_uploads/{TEST_PREFIX_ROOT}{uuid.uuid4()}/"

    def test_compose_gives_the_two_roles_different_identities(self):
        assert _ENV is not None
        assert _ENV["MINIO_BACKEND_ACCESS_KEY"] != _ENV["MINIO_PRESIGN_ACCESS_KEY"]

    def test_backend_identity_can_write_read_and_delete(self, prefix):
        st = _identity_storage("backend", prefix)
        st.put_bytes("probe.bin", b"hello")
        assert st.get_bytes("probe.bin") == b"hello"
        assert st.delete("probe.bin") is True
        assert st.exists("probe.bin") is False

    def test_browser_identity_cannot_delete(self, prefix):
        backend = _identity_storage("backend", prefix)
        browser = _identity_storage("uploader", prefix)
        backend.put_bytes("victim.bin", b"hello")
        try:
            with pytest.raises(StorageUnavailable):
                browser.delete("victim.bin")
            assert backend.exists("victim.bin") is True      # still there
        finally:
            backend.delete("victim.bin")

    def test_backend_identity_lists_only_the_source_area(self, prefix):
        st = _identity_storage("backend", prefix)
        st.put_bytes("one.bin", b"1")
        try:
            assert [o.key for o in st.list_objects()] == ["one.bin"]
        finally:
            st.delete("one.bin")

    def test_stale_sweep_deletes_a_real_object_through_the_backend_identity(self, prefix):
        """End to end on the real bucket: dry-run keeps it, live run removes it."""
        from spir_dynamic.tasks.cleanup_tasks import _purge_stale_source_objects

        st = _identity_storage("backend", prefix)
        st.put_bytes("aged.bin", b"stale")
        try:
            with patch("spir_dynamic.monitoring.metrics.CLEANUP_DELETED_FILES"):
                # stale_hours=0 makes the freshly written object eligible, but the
                # 1 h recency guard still protects it — that guard is the safety net.
                guarded = _purge_stale_source_objects(st, stale_hours=0, dry_run=False)
                assert guarded["deleted"] == 0 and guarded["skipped_recent"] == 1
                assert st.exists("aged.bin") is True

                # An object cannot be aged 24 h inside a test, so only the
                # LISTING is backdated — the delete still goes to the real
                # bucket, signed with the real backend identity.
                aged = _AgedListing(st, hours=48)
                dry = _purge_stale_source_objects(aged, stale_hours=24, dry_run=True)
                assert dry["deleted"] == 0 and st.exists("aged.bin") is True
                live = _purge_stale_source_objects(aged, stale_hours=24, dry_run=False)
            assert live["deleted"] == 1 and live["failed"] == 0
            assert st.exists("aged.bin") is False
        finally:
            with contextlib.suppress(Exception):
                st.delete("aged.bin")
