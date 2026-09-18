"""
Phase 3A — MinIO infrastructure tests.

Two layers:

  Unit (always run, no Docker needed)
    - MINIO_* environment variables land in Settings with the expected types
    - Settings.minio_configured is False when nothing is set (native runs)
    - object_storage helpers build the health URL and fail soft when the
      endpoint is missing or unreachable

  Integration (run only when the SPIR Compose stack is up with `minio`)
    Real container-level checks through `docker compose` / `docker`:
    - compose config is valid and declares minio / minio-init / minio_data
    - minio container is healthy, its named volume exists
    - exactly one `spir-files` bucket exists, and re-running minio-init is
      idempotent (exit 0, still exactly one bucket)
    - the api container reaches MinIO over the Compose network via Settings
    - existing services are untouched: api healthy on 8000, frontend on 3000,
      db / redis healthy, all three workers + beat running, /api/health == 200

Run:   pytest tests/test_minio_infra.py -v
Skip:  integration tests self-skip when Docker or the stack is unavailable.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from spir_dynamic.app.config import Settings
from spir_dynamic.services.object_storage import (
    MinioProbe,
    check_minio_reachable,
    minio_health_url,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PROJECT = "spir"
BUCKET = "spir-files"
VOLUME = f"{COMPOSE_PROJECT}_minio_data"


# ── Unit: Settings + helpers ─────────────────────────────────────────────────

_MINIO_ENV = ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_BUCKET", "MINIO_SECURE")


@pytest.fixture(autouse=True)
def _clean_minio_env(monkeypatch):
    """Unit tests must not see the Compose-injected MINIO_* vars when run in-container."""
    for k in _MINIO_ENV:
        monkeypatch.delenv(k, raising=False)


def _settings(**env: str) -> Settings:
    """Settings built from explicit values only (no .env file)."""
    return Settings(_env_file=None, **env)


class TestMinioSettings:
    def test_defaults_are_not_configured(self):
        s = _settings()
        assert s.minio_endpoint == ""
        assert s.minio_bucket == BUCKET
        assert s.minio_secure is False
        assert s.minio_configured is False

    def test_env_vars_are_read(self, monkeypatch):
        monkeypatch.setenv("MINIO_ENDPOINT", "http://minio:9000")
        monkeypatch.setenv("MINIO_ACCESS_KEY", "u")
        monkeypatch.setenv("MINIO_SECRET_KEY", "p")
        monkeypatch.setenv("MINIO_BUCKET", "other-bucket")
        monkeypatch.setenv("MINIO_SECURE", "true")
        s = _settings()
        assert s.minio_endpoint == "http://minio:9000"
        assert s.minio_access_key == "u"
        assert s.minio_secret_key == "p"
        assert s.minio_bucket == "other-bucket"
        assert s.minio_secure is True
        assert s.minio_configured is True

    def test_partial_credentials_are_not_configured(self):
        s = _settings(minio_endpoint="http://minio:9000", minio_access_key="u")
        assert s.minio_configured is False

    def test_existing_settings_unchanged(self):
        # Phase 2 routing thresholds must not move because of the new fields.
        s = _settings()
        assert s.large_file_threshold_mb == 100
        assert s.giant_file_threshold_mb == 500


class TestObjectStorageHelpers:
    def test_health_url(self):
        s = _settings(minio_endpoint="http://minio:9000/")
        assert minio_health_url(s) == "http://minio:9000/minio/health/live"

    def test_health_url_requires_endpoint(self):
        with pytest.raises(ValueError):
            minio_health_url(_settings())

    def test_probe_not_configured_does_not_touch_network(self):
        probe = check_minio_reachable(_settings())
        assert probe == MinioProbe(reachable=False, url="", error="minio not configured")

    def test_probe_unreachable_fails_soft(self):
        # RFC 5737 TEST-NET address: guaranteed unroutable, short timeout.
        s = _settings(minio_endpoint="http://192.0.2.1:9", minio_access_key="u", minio_secret_key="p")
        probe = check_minio_reachable(s, timeout=0.5)
        assert probe.reachable is False
        assert probe.error


# ── Integration: real containers ─────────────────────────────────────────────

def _run(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args), cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout,
    )


def _compose(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return _run("docker", "compose", *args, timeout=timeout)


def _stack_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        r = _compose("ps", "--format", "json", "--status", "running", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if r.returncode != 0:
        return False
    services = {json.loads(line)["Service"] for line in r.stdout.splitlines() if line.strip()}
    return {"minio", "api"} <= services


pytestmark_integration = pytest.mark.skipif(
    not _stack_available(),
    reason="SPIR docker compose stack (with minio + api) is not running",
)


def _inspect(container: str, fmt: str) -> str:
    r = _run("docker", "inspect", container, "--format", fmt)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _compose_ps() -> dict[str, dict]:
    r = _compose("ps", "-a", "--format", "json")
    assert r.returncode == 0, r.stderr
    return {row["Service"]: row for row in (json.loads(l) for l in r.stdout.splitlines() if l.strip())}


def _list_buckets() -> list[str]:
    # Authenticated `mc` inside the server container — no host-side mc needed.
    r = _compose(
        "exec", "-T", "minio", "sh", "-c",
        'mc alias set local http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null '
        "&& mc ls local/",
    )
    assert r.returncode == 0, r.stderr
    # mc ls line format: "[date time UTC]     0B name/"
    return [line.split()[-1].rstrip("/") for line in r.stdout.splitlines() if line.strip()]


def _http_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytestmark_integration
class TestMinioContainers:
    def test_compose_config_valid_and_declares_minio(self):
        r = _compose("config", "--format", "json")
        assert r.returncode == 0, r.stderr
        cfg = json.loads(r.stdout)
        assert {"minio", "minio-init"} <= set(cfg["services"])
        assert cfg["volumes"]["minio_data"]["name"] == VOLUME
        # host ports are loopback-only; container port 9000 is the S3 API
        ports = {(p["host_ip"], p["published"], p["target"]) for p in cfg["services"]["minio"]["ports"]}
        assert ("127.0.0.1", "9000", 9000) in ports
        assert ("127.0.0.1", "9001", 9001) in ports
        assert cfg["services"]["minio"]["restart"] == "unless-stopped"
        assert cfg["services"]["minio-init"]["restart"] == "no"
        # api/workers receive the service-name endpoint, never localhost
        for svc in ("api", "worker-normal", "worker-heavy", "worker-giant", "beat"):
            env = cfg["services"][svc]["environment"]
            assert env["MINIO_ENDPOINT"] == "http://minio:9000", svc
            assert env["MINIO_BUCKET"] == BUCKET, svc

    def test_minio_running_and_healthy(self):
        ps = _compose_ps()
        assert ps["minio"]["State"] == "running"
        assert _inspect(ps["minio"]["Name"], "{{.State.Health.Status}}") == "healthy"

    def test_minio_on_spir_network(self):
        ps = _compose_ps()
        nets = _inspect(ps["minio"]["Name"], "{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}").split()
        api_nets = _inspect(ps["api"]["Name"], "{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}").split()
        assert nets and set(nets) & set(api_nets), (nets, api_nets)

    def test_minio_volume_exists_and_is_mounted(self):
        r = _run("docker", "volume", "inspect", VOLUME, "--format", "{{.Name}}")
        assert r.returncode == 0 and r.stdout.strip() == VOLUME
        ps = _compose_ps()
        mounts = _inspect(ps["minio"]["Name"], "{{range .Mounts}}{{.Name}}:{{.Destination}} {{end}}")
        assert f"{VOLUME}:/data" in mounts.split()

    def test_minio_init_completed_and_bucket_exists_once(self):
        ps = _compose_ps()
        assert ps["minio-init"]["State"] == "exited"
        assert ps["minio-init"]["ExitCode"] == 0
        buckets = _list_buckets()
        assert buckets.count(BUCKET) == 1, buckets

    def test_minio_init_is_idempotent(self):
        before = _list_buckets()
        r = _compose("run", "--rm", "--no-deps", "minio-init", timeout=180)
        assert r.returncode == 0, r.stderr + r.stdout
        assert f"bucket '{BUCKET}' ready" in r.stdout
        after = _list_buckets()
        assert after == before
        assert after.count(BUCKET) == 1

    def test_api_container_reaches_minio_via_settings(self):
        r = _compose(
            "exec", "-T", "api", "python", "-c",
            "from spir_dynamic.services.object_storage import check_minio_reachable as c;"
            "p=c(); print(p.reachable, p.status, p.url)",
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip().splitlines()[-1] == "True 200 http://minio:9000/minio/health/live"

    def test_worker_container_reaches_minio(self):
        r = _compose(
            "exec", "-T", "worker-normal", "python", "-c",
            "from spir_dynamic.services.object_storage import check_minio_reachable as c; print(c().reachable)",
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip().splitlines()[-1] == "True"

    def test_minio_host_port_is_loopback_only(self):
        assert _http_status("http://127.0.0.1:9000/minio/health/live") == 200


@pytestmark_integration
class TestExistingServicesUntouched:
    def test_core_services_running(self):
        ps = _compose_ps()
        for svc in ("api", "frontend", "db", "redis", "worker-normal", "worker-heavy", "worker-giant", "beat"):
            assert ps[svc]["State"] == "running", svc
        for svc in ("api", "db", "redis"):
            assert _inspect(ps[svc]["Name"], "{{.State.Health.Status}}") == "healthy", svc
        assert ps["db-init"]["State"] == "exited" and ps["db-init"]["ExitCode"] == 0

    def test_published_ports_unchanged(self):
        ps = _compose_ps()
        assert "0.0.0.0:3000->3000" in ps["frontend"]["Ports"]
        assert "0.0.0.0:8000->8000" in ps["api"]["Ports"]
        assert "0.0.0.0:6379->6379" in ps["redis"]["Ports"]
        assert "0.0.0.0:5434->5432" in ps["db"]["Ports"]

    def test_api_health_endpoint(self):
        assert _http_status("http://localhost:8000/api/health") == 200

    def test_frontend_responds(self):
        assert _http_status("http://localhost:3000/") in (200, 307, 308)

    def test_api_does_not_depend_on_minio(self):
        # Phase 3A: existing workflows must keep working even if MinIO is down.
        r = _compose("config", "--format", "json")
        cfg = json.loads(r.stdout)
        for svc in ("api", "worker-normal", "worker-heavy", "worker-giant", "beat", "frontend"):
            assert "minio" not in cfg["services"][svc].get("depends_on", {}), svc
