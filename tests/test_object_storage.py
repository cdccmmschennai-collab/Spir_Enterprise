"""
Phase 3B — storage abstraction tests.

Layers:

  Unit (always run)
    - object key rules
    - Settings: STORAGE_BACKEND default / env / validation, AVATAR_DIR
    - factory: area -> backend mapping, default is filesystem, caching
    - LocalFilesystemStorage against a temporary directory (layout, contract)
    - MinioObjectStorage configuration + failure handling without a server

  Contract (parametrised over both backends)
    The same scenario list runs against LocalFilesystemStorage (tmp dir) and
    MinioObjectStorage (the Phase 3A Compose MinIO, when reachable).

  MinIO integration (skipped when MinIO is not reachable)
    Runs against the real `spir-files` bucket under a test-only key prefix
    that is removed afterwards. The bucket itself is never touched.

  Docker (skipped when the Compose stack is not running)
    The api container can build the minio backend from its own MINIO_*
    settings and round-trip an object — proves the image carries the client
    library and the wiring works over the Compose network.

Run:   pytest tests/test_object_storage.py -v
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from spir_dynamic.app.config import Settings
from spir_dynamic.services import object_storage as os_pkg
from spir_dynamic.services.object_storage import (
    BACKEND_FILESYSTEM,
    BACKEND_MINIO,
    InvalidObjectKey,
    LocalFilesystemStorage,
    MinioObjectStorage,
    ObjectInfo,
    ObjectNotFound,
    ObjectStorage,
    StorageArea,
    StorageConfigError,
    StorageError,
    StorageUnavailable,
    build_object_storage,
    get_object_storage,
    normalize_key,
    reset_object_storage,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_PREFIX_ROOT = "_tests/object_storage/"   # every MinIO test object lives under here

_STORAGE_ENV = (
    "STORAGE_BACKEND", "AVATAR_DIR", "ROWS_STORAGE_PATH", "BATCH_UPLOAD_DIR",
    "MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_BUCKET", "MINIO_SECURE",
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Unit tests must see neither the Compose env nor a cached backend from another test."""
    for k in _STORAGE_ENV:
        monkeypatch.delenv(k, raising=False)
    reset_object_storage()
    yield
    reset_object_storage()


def _settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


# ── MinIO test-target discovery ──────────────────────────────────────────────

def _compose_config() -> dict | None:
    if shutil.which("docker") is None:
        return None
    try:
        r = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return json.loads(r.stdout) if r.returncode == 0 else None


def _minio_target() -> dict | None:
    """
    Connection details for the local Phase 3A MinIO, or None if unreachable.
    Explicit MINIO_* env wins; otherwise the Compose defaults on 127.0.0.1:9000.
    """
    env = {k: os.environ.get(k) for k in ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY")}
    if all(env.values()):
        target = {
            "endpoint": env["MINIO_ENDPOINT"],
            "access_key": env["MINIO_ACCESS_KEY"],
            "secret_key": env["MINIO_SECRET_KEY"],
            "bucket": os.environ.get("MINIO_BUCKET", "spir-files"),
        }
    else:
        cfg = _compose_config()
        if not cfg:
            return None
        minio_env = cfg["services"].get("minio", {}).get("environment", {})
        init_env = cfg["services"].get("minio-init", {}).get("environment", {})
        target = {
            "endpoint": "http://127.0.0.1:9000",
            "access_key": minio_env.get("MINIO_ROOT_USER", ""),
            "secret_key": minio_env.get("MINIO_ROOT_PASSWORD", ""),
            "bucket": init_env.get("MINIO_BUCKET", "spir-files"),
        }
    if not (target["access_key"] and target["secret_key"]):
        return None
    try:
        with urllib.request.urlopen(target["endpoint"].rstrip("/") + "/minio/health/live", timeout=3) as r:
            if r.status != 200:
                return None
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return target


_MINIO = _minio_target()   # evaluated once at collection time (env is the process env)
minio_required = pytest.mark.skipif(_MINIO is None, reason="local MinIO (Compose stack) is not reachable")


def _minio_storage(prefix: str, **overrides) -> MinioObjectStorage:
    assert _MINIO is not None
    kwargs = dict(_MINIO, prefix=prefix)
    kwargs.update(overrides)
    return MinioObjectStorage(**kwargs)


@pytest.fixture
def minio_scratch():
    """A MinIO backend scoped to a unique test prefix; everything under it is removed afterwards."""
    if _MINIO is None:
        pytest.skip("local MinIO (Compose stack) is not reachable")
    prefix = f"{TEST_PREFIX_ROOT}{uuid.uuid4()}/"
    st = _minio_storage(prefix)
    yield st
    for obj in list(st.list_objects()):
        st.delete(obj.key)
    assert list(st.list_objects()) == []


# ── Unit: key rules ──────────────────────────────────────────────────────────

class TestKeyRules:
    @pytest.mark.parametrize("key", [
        "a", "a.json", "uploads/job-1/original.xlsx", "extracted_rows/abc.json",
        "with space.txt", "üñí/cödé.bin", "a" * 1024,
    ])
    def test_valid_keys_pass_through_unchanged(self, key):
        assert normalize_key(key) == key

    @pytest.mark.parametrize("key", [
        "", "/a", "a/", "a//b", "./a", "a/./b", "../a", "a/../b", "..", ".",
        "a\\b", "a\x00b", "a" * 1025, None, 123,
    ])
    def test_invalid_keys_rejected(self, key):
        with pytest.raises(InvalidObjectKey):
            normalize_key(key)  # type: ignore[arg-type]

    def test_invalid_key_is_a_storage_error_and_a_value_error(self):
        with pytest.raises(StorageError):
            normalize_key("../x")
        with pytest.raises(ValueError):
            normalize_key("../x")


# ── Unit: Settings ───────────────────────────────────────────────────────────

class TestSettings:
    def test_default_backend_is_filesystem(self):
        s = _settings()
        assert s.storage_backend == BACKEND_FILESYSTEM
        assert s.avatar_dir == "storage/avatars"          # CWD-relative, as before

    def test_backend_from_env(self, monkeypatch):
        monkeypatch.setenv("STORAGE_BACKEND", "MinIO")
        assert _settings().storage_backend == BACKEND_MINIO

    @pytest.mark.parametrize("bad", ["s3", "local", "", "disk"])
    def test_invalid_backend_rejected(self, bad):
        with pytest.raises(ValueError):
            _settings(storage_backend=bad)

    def test_avatar_dir_from_env(self, monkeypatch):
        monkeypatch.setenv("AVATAR_DIR", "/srv/avatars")
        assert _settings().avatar_dir == "/srv/avatars"

    def test_existing_storage_paths_unchanged(self):
        s = _settings()
        assert s.rows_storage_path.endswith(os.path.join("storage", "extracted_rows"))
        assert s.batch_upload_dir.endswith(os.path.join("storage", "batch_uploads"))
        assert s.large_file_threshold_mb == 100 and s.giant_file_threshold_mb == 500


# ── Unit: factory ────────────────────────────────────────────────────────────

class TestFactory:
    def test_default_builds_filesystem_backend_per_area(self):
        s = _settings()
        expected = {
            StorageArea.EXTRACTED_ROWS: s.rows_storage_path,
            StorageArea.BATCH_UPLOADS: s.batch_upload_dir,
            StorageArea.AVATARS: s.avatar_dir,
        }
        for area, root in expected.items():
            st = build_object_storage(area, s)
            assert isinstance(st, LocalFilesystemStorage)
            assert st.backend == BACKEND_FILESYSTEM
            assert st.root == Path(root)

    def test_minio_backend_selected_by_setting(self):
        s = _settings(
            storage_backend="minio", minio_endpoint="http://minio:9000",
            minio_access_key="u", minio_secret_key="p", minio_bucket="b",
        )
        st = build_object_storage(StorageArea.BATCH_UPLOADS, s)
        assert isinstance(st, MinioObjectStorage)
        assert st.backend == BACKEND_MINIO
        assert st.bucket == "b"
        assert st.prefix == "batch_uploads/"
        assert st.endpoint_url == "http://minio:9000"

    def test_minio_backend_without_credentials_is_a_config_error(self):
        s = _settings(storage_backend="minio")
        with pytest.raises(StorageConfigError):
            build_object_storage(StorageArea.AVATARS, s)

    def test_get_object_storage_uses_settings_and_caches(self, monkeypatch, tmp_path):
        from spir_dynamic.app import config as config_mod
        monkeypatch.setattr(config_mod, "get_settings", lambda: _settings(rows_storage_path=str(tmp_path)))
        monkeypatch.setattr(os_pkg.factory, "get_settings", lambda: _settings(rows_storage_path=str(tmp_path)))
        a = get_object_storage(StorageArea.EXTRACTED_ROWS)
        b = get_object_storage(StorageArea.EXTRACTED_ROWS)
        assert a is b
        assert isinstance(a, LocalFilesystemStorage) and a.root == tmp_path
        reset_object_storage()
        assert get_object_storage(StorageArea.EXTRACTED_ROWS) is not a

    def test_both_backends_satisfy_the_protocol(self, tmp_path):
        assert isinstance(LocalFilesystemStorage(tmp_path), ObjectStorage)
        m = MinioObjectStorage(endpoint="h:1", access_key="u", secret_key="p", bucket="b")
        assert isinstance(m, ObjectStorage)


# ── Contract: both backends ──────────────────────────────────────────────────

@pytest.fixture(params=[BACKEND_FILESYSTEM, BACKEND_MINIO])
def storage(request, tmp_path):
    if request.param == BACKEND_FILESYSTEM:
        yield LocalFilesystemStorage(tmp_path / "area")
    else:
        yield request.getfixturevalue("minio_scratch")


class TestContract:
    def test_put_get_roundtrip(self, storage):
        info = storage.put_bytes("a.json", b'{"x": 1}')
        assert isinstance(info, ObjectInfo)
        assert info.key == "a.json" and info.size == 8
        assert info.last_modified.tzinfo is not None
        assert abs((datetime.now(timezone.utc) - info.last_modified).total_seconds()) < 120
        assert storage.get_bytes("a.json") == b'{"x": 1}'

    def test_exists_and_missing(self, storage):
        assert storage.exists("nope.bin") is False
        storage.put_bytes("yes.bin", b"1")
        assert storage.exists("yes.bin") is True

    @pytest.mark.parametrize("op", ["get_bytes", "stat", "open_read"])
    def test_missing_object_raises_object_not_found(self, storage, op):
        with pytest.raises(ObjectNotFound) as ei:
            getattr(storage, op)("missing/object.txt")
        assert ei.value.key == "missing/object.txt"

    def test_get_file_missing_raises_and_leaves_no_dest(self, storage, tmp_path):
        dest = tmp_path / "out" / "x.bin"
        with pytest.raises(ObjectNotFound):
            storage.get_file("missing.bin", dest)
        assert not dest.exists()

    def test_delete_semantics(self, storage):
        assert storage.delete("ghost") is False          # never raises for missing
        storage.put_bytes("real", b"x")
        assert storage.delete("real") is True
        assert storage.exists("real") is False
        assert storage.delete("real") is False

    def test_overwrite_replaces_content(self, storage):
        storage.put_bytes("k", b"first version")
        info = storage.put_bytes("k", b"v2")
        assert info.size == 2
        assert storage.get_bytes("k") == b"v2"
        assert storage.stat("k").size == 2

    def test_keys_with_prefixes(self, storage):
        storage.put_bytes("uploads/job-1/original.xlsx", b"A")
        storage.put_bytes("uploads/job-1/sanitized.xlsx", b"BB")
        storage.put_bytes("uploads/job-2/original.xlsx", b"CCC")
        storage.put_bytes("rows/r.json", b"D")
        assert storage.get_bytes("uploads/job-1/original.xlsx") == b"A"
        keys = {o.key for o in storage.list_objects("uploads/job-1/")}
        assert keys == {"uploads/job-1/original.xlsx", "uploads/job-1/sanitized.xlsx"}
        assert {o.key for o in storage.list_objects("uploads/")} == keys | {"uploads/job-2/original.xlsx"}
        assert {o.key for o in storage.list_objects()} == keys | {"uploads/job-2/original.xlsx", "rows/r.json"}
        assert list(storage.list_objects("nothing/")) == []
        sizes = {o.key: o.size for o in storage.list_objects("uploads/")}
        assert sizes["uploads/job-2/original.xlsx"] == 3

    @pytest.mark.parametrize("bad", ["../escape", "/abs", "a\\b", "", "a/../b"])
    def test_invalid_keys_rejected_before_any_io(self, storage, bad):
        for op in ("get_bytes", "exists", "delete", "stat", "open_read"):
            with pytest.raises(InvalidObjectKey):
                getattr(storage, op)(bad)
        with pytest.raises(InvalidObjectKey):
            storage.put_bytes(bad, b"x")

    def test_put_file_and_get_file_stream_local_files(self, storage, tmp_path):
        src = tmp_path / "src.xlsx"
        payload = os.urandom(3 * 1024 * 1024 + 17)   # > 1 MB so chunked paths are exercised
        src.write_bytes(payload)
        info = storage.put_file("uploads/j/original.xlsx", src)
        assert info.size == len(payload)
        assert src.read_bytes() == payload            # source untouched
        dest = tmp_path / "down" / "copy.xlsx"
        assert storage.get_file("uploads/j/original.xlsx", dest) == dest
        assert dest.read_bytes() == payload

    def test_put_file_missing_source(self, storage, tmp_path):
        with pytest.raises(FileNotFoundError):
            storage.put_file("k", tmp_path / "does-not-exist")
        assert storage.exists("k") is False

    def test_open_read_is_a_binary_stream(self, storage):
        storage.put_bytes("s.bin", b"0123456789" * 1000)
        with storage.open_read("s.bin") as fh:
            assert fh.read(4) == b"0123"
            rest = fh.read()
        assert len(rest) == 10_000 - 4
        assert fh.closed

    def test_stat_metadata(self, storage):
        storage.put_bytes("meta/x.json", b"{}", content_type="application/json")
        info = storage.stat("meta/x.json")
        assert info == ObjectInfo(
            key="meta/x.json", size=2, last_modified=info.last_modified, content_type="application/json",
        )
        assert info.last_modified.utcoffset().total_seconds() == 0

    def test_ping_ok(self, storage):
        if isinstance(storage, LocalFilesystemStorage):
            storage.root.mkdir(parents=True, exist_ok=True)
        storage.ping()

    def test_empty_object(self, storage):
        assert storage.put_bytes("empty", b"").size == 0
        assert storage.get_bytes("empty") == b""
        assert storage.exists("empty")


# ── Unit: filesystem specifics ───────────────────────────────────────────────

class TestLocalFilesystemStorage:
    def test_layout_matches_existing_call_sites(self, tmp_path):
        """<root>/<key> — exactly where routes/_save_rows_to_disk and the worker write today."""
        st = LocalFilesystemStorage(tmp_path)
        st.put_bytes("abc123.json", b"{}")
        assert (tmp_path / "abc123.json").read_bytes() == b"{}"
        assert st.path_for("abc123.json") == tmp_path / "abc123.json"
        st.put_bytes("job_000_file.xlsx", b"x")
        assert (tmp_path / "job_000_file.xlsx").is_file()

    def test_nested_key_creates_directories(self, tmp_path):
        st = LocalFilesystemStorage(tmp_path)
        st.put_bytes("a/b/c.txt", b"1")
        assert (tmp_path / "a" / "b" / "c.txt").read_bytes() == b"1"

    def test_reads_files_written_by_other_code(self, tmp_path):
        (tmp_path / "legacy.json").write_text('{"legacy": true}', encoding="utf-8")
        st = LocalFilesystemStorage(tmp_path)
        assert st.exists("legacy.json")
        assert json.loads(st.get_bytes("legacy.json")) == {"legacy": True}

    def test_root_kept_as_given(self, tmp_path):
        assert LocalFilesystemStorage("storage/avatars").root == Path("storage/avatars")
        assert LocalFilesystemStorage(str(tmp_path)).root == tmp_path

    def test_directory_is_not_an_object(self, tmp_path):
        st = LocalFilesystemStorage(tmp_path)
        (tmp_path / "dir").mkdir()
        assert st.exists("dir") is False
        with pytest.raises(ObjectNotFound):
            st.stat("dir")
        with pytest.raises(ObjectNotFound):
            st.get_file("dir", tmp_path / "x")

    def test_list_on_missing_root_is_empty(self, tmp_path):
        assert list(LocalFilesystemStorage(tmp_path / "absent").list_objects()) == []

    def test_ping_fails_when_root_missing(self, tmp_path):
        with pytest.raises(StorageUnavailable):
            LocalFilesystemStorage(tmp_path / "absent").ping()
        assert not (tmp_path / "absent").exists()   # not auto-created

    def test_ping_leaves_no_probe_behind(self, tmp_path):
        LocalFilesystemStorage(tmp_path).ping()
        assert list(tmp_path.iterdir()) == []

    def test_content_type_derived_from_extension(self, tmp_path):
        st = LocalFilesystemStorage(tmp_path)
        assert st.put_bytes("a.json", b"{}").content_type == "application/json"
        assert st.put_bytes("u.jpg", b"x").content_type == "image/jpeg"
        assert st.put_bytes("noext", b"x").content_type is None

    def test_io_errors_become_storage_errors(self, tmp_path):
        st = LocalFilesystemStorage(tmp_path)
        (tmp_path / "blocker").write_bytes(b"file, not a dir")
        with pytest.raises(StorageError):
            st.put_bytes("blocker/child.txt", b"x")   # parent is a file -> OSError


# ── Unit: MinIO specifics that need no server ────────────────────────────────

class TestMinioConfiguration:
    def test_from_settings_requires_full_configuration(self):
        with pytest.raises(StorageConfigError):
            MinioObjectStorage.from_settings(_settings())
        with pytest.raises(StorageConfigError):
            MinioObjectStorage.from_settings(_settings(minio_endpoint="http://x:9000", minio_access_key="u"))

    def test_from_settings_uses_phase_3a_settings_verbatim(self):
        s = _settings(
            minio_endpoint="http://minio:9000", minio_access_key="ak", minio_secret_key="sk",
            minio_bucket="spir-files", minio_secure=False,
        )
        st = MinioObjectStorage.from_settings(s, prefix="avatars/")
        assert st.endpoint_url == "http://minio:9000"
        assert st.bucket == "spir-files"
        assert st.prefix == "avatars/"

    @pytest.mark.parametrize("endpoint,secure,expected", [
        ("http://minio:9000", False, "http://minio:9000"),
        ("http://minio:9000/", True, "http://minio:9000"),     # explicit scheme wins
        ("minio:9000", False, "http://minio:9000"),
        ("minio:9000", True, "https://minio:9000"),
        ("https://s3.example.com", False, "https://s3.example.com"),
    ])
    def test_endpoint_url_and_secure_flag(self, endpoint, secure, expected):
        st = MinioObjectStorage(endpoint=endpoint, access_key="u", secret_key="p", bucket="b", secure=secure)
        assert st.endpoint_url == expected

    def test_missing_constructor_values_are_config_errors(self):
        with pytest.raises(StorageConfigError):
            MinioObjectStorage(endpoint="", access_key="u", secret_key="p", bucket="b")
        with pytest.raises(StorageConfigError):
            MinioObjectStorage(endpoint="h", access_key="u", secret_key="p", bucket="")

    def test_invalid_prefix_rejected(self):
        with pytest.raises(InvalidObjectKey):
            MinioObjectStorage(endpoint="h", access_key="u", secret_key="p", bucket="b", prefix="../x/")

    def test_no_client_created_until_used(self):
        st = MinioObjectStorage(endpoint="h", access_key="u", secret_key="p", bucket="b")
        assert st._client is None

    def test_unreachable_endpoint_is_storage_unavailable(self):
        # RFC 5737 TEST-NET address: unroutable, so this fails fast on the connect timeout.
        st = MinioObjectStorage(
            endpoint="http://192.0.2.1:9", access_key="u", secret_key="p", bucket="b",
            connect_timeout=0.5, read_timeout=0.5, max_attempts=1,
        )
        for op in ("ping", lambda: st.exists("k"), lambda: st.get_bytes("k"),
                   lambda: st.put_bytes("k", b"x"), lambda: st.delete("k"), lambda: list(st.list_objects())):
            with pytest.raises(StorageUnavailable) as ei:
                (getattr(st, op) if isinstance(op, str) else op)()
            assert "botocore" not in type(ei.value).__module__

    def test_get_file_unreachable_leaves_no_partial_dest(self, tmp_path):
        st = MinioObjectStorage(
            endpoint="http://192.0.2.1:9", access_key="u", secret_key="p", bucket="b",
            connect_timeout=0.5, read_timeout=0.5, max_attempts=1,
        )
        dest = tmp_path / "d" / "x"
        with pytest.raises(StorageUnavailable):
            st.get_file("k", dest)
        assert not dest.exists()


# ── Integration: real MinIO (Compose stack) ─────────────────────────────────

@minio_required
class TestMinioIntegration:
    def test_objects_live_under_prefix_in_the_bucket(self, minio_scratch):
        minio_scratch.put_bytes("uploads/job-1/original.xlsx", b"data")
        raw = _minio_storage(prefix="")                       # unprefixed view of the same bucket
        full_key = minio_scratch.prefix + "uploads/job-1/original.xlsx"
        assert raw.exists(full_key)
        assert raw.get_bytes(full_key) == b"data"
        assert full_key.startswith(TEST_PREFIX_ROOT)

    def test_prefixes_isolate_instances(self, minio_scratch):
        other = _minio_storage(prefix=minio_scratch.prefix + "other/")
        minio_scratch.put_bytes("k", b"mine")
        other.put_bytes("k", b"theirs")
        assert minio_scratch.get_bytes("k") == b"mine"
        assert other.get_bytes("k") == b"theirs"
        assert {o.key for o in minio_scratch.list_objects()} == {"k", "other/k"}
        assert {o.key for o in other.list_objects()} == {"k"}

    def test_content_type_is_stored(self, minio_scratch):
        # .json / .png are in Python's built-in mimetypes table on every platform
        # (the slim Docker image has no /etc/mime.types, so .xlsx is not).
        assert minio_scratch.put_bytes("a.json", b"{}").content_type == "application/json"
        assert minio_scratch.put_bytes("u.png", b"x").content_type == "image/png"
        assert minio_scratch.put_bytes("blob", b"x").content_type == "application/octet-stream"
        assert minio_scratch.put_bytes("blob", b"x", content_type="image/webp").content_type == "image/webp"

    def test_wrong_credentials_are_storage_unavailable(self, minio_scratch):
        bad = _minio_storage(prefix=minio_scratch.prefix, access_key="nobody", secret_key="wrong-password")
        with pytest.raises(StorageUnavailable):
            bad.exists("k")
        with pytest.raises(StorageUnavailable):
            bad.put_bytes("k", b"x")

    def test_missing_bucket_is_storage_unavailable(self, minio_scratch):
        nb = _minio_storage(prefix=minio_scratch.prefix, bucket=f"no-such-bucket-{uuid.uuid4().hex[:8]}")
        with pytest.raises(StorageUnavailable):
            nb.ping()
        with pytest.raises(StorageUnavailable):
            nb.put_bytes("k", b"x")

    def test_factory_builds_working_minio_backend(self, minio_scratch):
        s = _settings(
            storage_backend="minio", minio_endpoint=_MINIO["endpoint"],
            minio_access_key=_MINIO["access_key"], minio_secret_key=_MINIO["secret_key"],
            minio_bucket=_MINIO["bucket"],
        )
        st = build_object_storage(StorageArea.AVATARS, s)
        assert isinstance(st, MinioObjectStorage) and st.prefix == "avatars/"
        st.ping()
        # Only probe — the real avatars/ prefix must not receive test objects.

    def test_application_bucket_untouched_by_tests(self, minio_scratch):
        raw = _minio_storage(prefix="")
        raw.ping()
        for area in StorageArea:
            for obj in raw.list_objects(f"{area.value}/"):
                assert not obj.key.startswith(TEST_PREFIX_ROOT)


# ── Docker: backend usable from inside the api container ────────────────────

def _stack_available() -> bool:
    cfg = _compose_config()
    if not cfg:
        return False
    try:
        r = subprocess.run(
            ["docker", "compose", "ps", "--format", "json", "--status", "running"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if r.returncode != 0:
        return False
    services = {json.loads(line)["Service"] for line in r.stdout.splitlines() if line.strip()}
    return {"minio", "api"} <= services


_IN_CONTAINER_ROUNDTRIP = f"""
import uuid
from spir_dynamic.app.config import get_settings
from spir_dynamic.services.object_storage import MinioObjectStorage, ObjectNotFound, get_object_storage, StorageArea
cfg = get_settings()
print("active:", cfg.storage_backend, type(get_object_storage(StorageArea.EXTRACTED_ROWS)).__name__)
st = MinioObjectStorage.from_settings(cfg, prefix="{TEST_PREFIX_ROOT}container-" + uuid.uuid4().hex + "/")
st.ping()
st.put_bytes("uploads/j/original.xlsx", b"hello from api")
assert st.get_bytes("uploads/j/original.xlsx") == b"hello from api"
assert st.delete("uploads/j/original.xlsx") is True
try:
    st.get_bytes("uploads/j/original.xlsx")
except ObjectNotFound:
    print("roundtrip: ok")
"""


@pytest.mark.skipif(not _stack_available(), reason="SPIR docker compose stack (minio + api) is not running")
class TestDockerStack:
    def test_api_container_can_use_minio_backend_but_runs_filesystem(self):
        r = subprocess.run(
            ["docker", "compose", "exec", "-T", "api", "python", "-c", _IN_CONTAINER_ROUNDTRIP],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        )
        assert r.returncode == 0, r.stderr + r.stdout
        lines = r.stdout.strip().splitlines()
        assert "active: filesystem LocalFilesystemStorage" in lines
        assert lines[-1] == "roundtrip: ok"

    def test_health_reports_filesystem_backend(self):
        with urllib.request.urlopen("http://localhost:8000/api/health", timeout=10) as resp:
            body = json.loads(resp.read())
        assert resp.status == 200
        assert body["storage_backend"] == "filesystem"
        assert body["extraction_dir"] == "ok"
