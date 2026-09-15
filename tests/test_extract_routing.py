"""
Tests for size-based sync/async routing of the single-file /api/extract endpoint.

Routing authority: batch_router.route_queue() — the same function the batch
workflow uses. /api/extract keeps the in-process sync path for anything that
routes to 'normal' (≤ large_file_threshold_mb) and hands 'heavy' / 'giant'
files to the existing Celery workers via _dispatch_celery.

Threshold values under test are the repository defaults (config.py):
    large_file_threshold_mb = 100
    giant_file_threshold_mb = 500
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spir_dynamic.app.batch_router import (
    _GIANT_HARD_LIMIT,
    _GIANT_SOFT_LIMIT,
    _HEAVY_HARD_LIMIT,
    _HEAVY_SOFT_LIMIT,
    _dispatch_celery,
    batch_upload_path,
    route_queue,
)

MB = 1024 * 1024
_CFG = SimpleNamespace(large_file_threshold_mb=100, giant_file_threshold_mb=500)


# ── route_queue: the single size → queue authority ───────────────────────────

class TestRouteQueue:
    @pytest.mark.parametrize(
        "size_bytes, expected",
        [
            (0, "normal"),
            (1, "normal"),
            (71 * MB, "normal"),
            (100 * MB, "normal"),          # exactly at the boundary stays normal
            (100 * MB + 1, "heavy"),       # first byte over → heavy
            (128 * MB, "heavy"),
            (223 * MB, "heavy"),           # VEN-4460-DGEN-5-43-0001-1.xlsm class
            (500 * MB, "heavy"),           # exactly at giant boundary stays heavy
            (500 * MB + 1, "giant"),       # first byte over → giant
            (1200 * MB, "giant"),
        ],
    )
    def test_boundaries(self, size_bytes, expected):
        queue, _ = route_queue(size_bytes, _CFG)
        assert queue == expected

    def test_normal_has_no_time_limit_override(self):
        _, kwargs = route_queue(50 * MB, _CFG)
        assert kwargs == {}

    def test_heavy_time_limits(self):
        _, kwargs = route_queue(223 * MB, _CFG)
        assert kwargs == {"soft_time_limit": _HEAVY_SOFT_LIMIT, "time_limit": _HEAVY_HARD_LIMIT}

    def test_giant_time_limits(self):
        _, kwargs = route_queue(600 * MB, _CFG)
        assert kwargs == {"soft_time_limit": _GIANT_SOFT_LIMIT, "time_limit": _GIANT_HARD_LIMIT}

    def test_thresholds_come_from_config(self):
        cfg = SimpleNamespace(large_file_threshold_mb=10, giant_file_threshold_mb=20)
        assert route_queue(10 * MB, cfg)[0] == "normal"
        assert route_queue(10 * MB + 1, cfg)[0] == "heavy"
        assert route_queue(20 * MB + 1, cfg)[0] == "giant"


# ── _dispatch_celery: uses route_queue and reports the queue chosen ──────────

class TestDispatchCelery:
    def test_each_file_goes_to_its_queue(self):
        task = MagicMock()
        files = [
            (Path("/u/a.xlsx"), "a.xlsx", 5 * MB),
            (Path("/u/b.xlsm"), "b.xlsm", 223 * MB),
            (Path("/u/c.xlsm"), "c.xlsm", 700 * MB),
        ]
        with patch("spir_dynamic.tasks.extraction_tasks.process_file_task", task), \
             patch("spir_dynamic.monitoring.metrics.GIANT_FILES_ROUTED") as giant_metric:
            queues = _dispatch_celery("job-1", files, _CFG, user_id="u1")

        assert queues == ["normal", "heavy", "giant"]
        sent = [c.kwargs["queue"] for c in task.apply_async.call_args_list]
        assert sent == ["normal", "heavy", "giant"]
        # args carry job_id, idx, path, filename, user_id — unchanged contract
        assert task.apply_async.call_args_list[1].kwargs["args"] == ["job-1", 1, "/u/b.xlsm", "b.xlsm", "u1"]
        assert task.apply_async.call_args_list[1].kwargs["soft_time_limit"] == _HEAVY_SOFT_LIMIT
        giant_metric.inc.assert_called_once()

    def test_idx_offset_respected(self):
        task = MagicMock()
        with patch("spir_dynamic.tasks.extraction_tasks.process_file_task", task), \
             patch("spir_dynamic.monitoring.metrics.GIANT_FILES_ROUTED"):
            _dispatch_celery("job-2", [(Path("/u/x.xlsx"), "x.xlsx", 1)], _CFG, idx_offset=3)
        assert task.apply_async.call_args.kwargs["args"][1] == 3


def test_batch_upload_path_naming():
    p = batch_upload_path(Path("/uploads"), "abc", 0, "../we ird/name:1.xlsm")
    assert p.parent == Path("/uploads")
    assert p.name == "abc_000_name_1.xlsm"


# ── /api/extract: sync for 'normal', async hand-off for 'heavy' / 'giant' ────

@pytest.fixture
def api(tmp_path):
    """TestClient with auth bypassed, no lifespan (no DB), isolated storage dirs."""
    from fastapi.testclient import TestClient
    from spir_dynamic.app.auth import TokenData, get_current_user
    from spir_dynamic.app.config import get_settings
    from spir_dynamic.app.main import app

    base_cfg = get_settings().model_copy(update={
        "rows_storage_path": str(tmp_path / "rows"),
        "batch_upload_dir": str(tmp_path / "uploads"),
        "max_file_size_mb": 2048,
        "large_file_threshold_mb": 100,
        "giant_file_threshold_mb": 500,
    })
    app.dependency_overrides[get_current_user] = lambda: TokenData("tester", "user-1", "jti-1")
    try:
        yield SimpleNamespace(client=TestClient(app), cfg=base_cfg, tmp=tmp_path)
    finally:
        app.dependency_overrides.clear()


_FAKE_RESULT = {
    "status": "done", "file_id": "fid-1", "filename": "small_Extraction.xlsx",
    "spir_no": "S-1", "total_rows": 3, "total_tags": 2, "preview_cols": [], "preview_rows": [],
}


def _post(client, name: str, size: int):
    return client.post("/api/extract", files={"file": (name, b"x" * size, "application/octet-stream")})


class TestExtractEndpointRouting:
    def test_small_file_stays_sync(self, api):
        cfg = api.cfg.model_copy(update={"celery_enabled": True})
        with patch("spir_dynamic.app.routes.get_settings", return_value=cfg), \
             patch("spir_dynamic.app.routes.run_pipeline", return_value=dict(_FAKE_RESULT)) as pipeline, \
             patch("spir_dynamic.app.routes._dispatch_celery") as dispatch, \
             patch("spir_dynamic.app.routes.get_job_store") as job_store:
            res = _post(api.client, "small.xlsx", 10)

        assert res.status_code == 200
        assert res.json()["file_id"] == "fid-1"
        pipeline.assert_called_once()                 # extraction ran in-process
        dispatch.assert_not_called()                  # nothing enqueued
        job_store.assert_not_called()                 # no job created
        assert not (api.tmp / "uploads").exists()     # nothing staged for a worker

    def test_large_file_is_queued(self, api):
        # Shrink the threshold so a 10-byte upload counts as "> large": route_queue
        # must pick 'heavy' (100 MB < size ≤ 500 MB analogue) and the file must be
        # handed to the batch worker path instead of run_pipeline.
        cfg = api.cfg.model_copy(update={"celery_enabled": True, "large_file_threshold_mb": 0})
        store = MagicMock()
        with patch("spir_dynamic.app.routes.get_settings", return_value=cfg), \
             patch("spir_dynamic.app.routes.run_pipeline") as pipeline, \
             patch("spir_dynamic.app.routes._dispatch_celery", return_value=["heavy"]) as dispatch, \
             patch("spir_dynamic.app.routes.get_job_store", return_value=store), \
             patch("spir_dynamic.app.routes._persist_job_to_db", new=AsyncMock()):
            res = _post(api.client, "big.xlsm", 10)

        assert res.status_code == 202
        body = res.json()
        assert body["status"] == "queued"
        assert body["queue"] == "heavy"
        assert body["filename"] == "big.xlsm"
        job_id = body["job_id"]

        pipeline.assert_not_called()                  # FastAPI did NOT extract
        store.create.assert_called_once_with(job_id, ["big.xlsm"], user_id="user-1")

        # Enqueued through the batch router with the file now in batch_upload_dir
        dispatch.assert_called_once()
        _job, file_data, _cfg, user_id = dispatch.call_args.args
        assert _job == job_id and user_id == "user-1"
        (disk_path, filename, size_bytes), = file_data
        assert filename == "big.xlsm" and size_bytes == 10
        assert disk_path == batch_upload_path(api.tmp / "uploads", job_id, 0, "big.xlsm")
        assert disk_path.exists() and disk_path.read_bytes() == b"x" * 10

    def test_giant_file_is_queued_to_giant(self, api):
        cfg = api.cfg.model_copy(update={
            "celery_enabled": True, "large_file_threshold_mb": 0, "giant_file_threshold_mb": 0,
        })
        # Let the real _dispatch_celery run so the queue choice is exercised end-to-end.
        task = MagicMock()
        with patch("spir_dynamic.app.routes.get_settings", return_value=cfg), \
             patch("spir_dynamic.app.routes.run_pipeline") as pipeline, \
             patch("spir_dynamic.tasks.extraction_tasks.process_file_task", task), \
             patch("spir_dynamic.monitoring.metrics.GIANT_FILES_ROUTED"), \
             patch("spir_dynamic.app.routes.get_job_store", return_value=MagicMock()), \
             patch("spir_dynamic.app.routes._persist_job_to_db", new=AsyncMock()):
            res = _post(api.client, "giant.xlsm", 10)

        assert res.status_code == 202
        assert res.json()["queue"] == "giant"
        assert task.apply_async.call_args.kwargs["queue"] == "giant"
        assert task.apply_async.call_args.kwargs["soft_time_limit"] == _GIANT_SOFT_LIMIT
        pipeline.assert_not_called()

    def test_celery_disabled_keeps_sync_for_large(self, api):
        # Native dev mode: no workers to hand off to — every size stays sync.
        cfg = api.cfg.model_copy(update={"celery_enabled": False, "large_file_threshold_mb": 0})
        with patch("spir_dynamic.app.routes.get_settings", return_value=cfg), \
             patch("spir_dynamic.app.routes.run_pipeline", return_value=dict(_FAKE_RESULT)) as pipeline, \
             patch("spir_dynamic.app.routes._dispatch_celery") as dispatch:
            res = _post(api.client, "big.xlsm", 10)

        assert res.status_code == 200
        pipeline.assert_called_once()
        dispatch.assert_not_called()

    def test_enqueue_failure_marks_job_error_and_cleans_file(self, api):
        cfg = api.cfg.model_copy(update={"celery_enabled": True, "large_file_threshold_mb": 0})
        store = MagicMock()
        with patch("spir_dynamic.app.routes.get_settings", return_value=cfg), \
             patch("spir_dynamic.app.routes.run_pipeline") as pipeline, \
             patch("spir_dynamic.app.routes._dispatch_celery", side_effect=RuntimeError("broker down")), \
             patch("spir_dynamic.app.routes.get_job_store", return_value=store):
            res = _post(api.client, "big.xlsm", 10)

        assert res.status_code == 503
        assert "broker down" in res.json()["detail"]
        pipeline.assert_not_called()
        # slot marked error so a poller never stalls on "pending"
        _jid, idx, result = store.update_result.call_args.args
        assert idx == 0 and result.status == "error"
        # moved upload removed again
        assert not list((api.tmp / "uploads").glob("*"))
