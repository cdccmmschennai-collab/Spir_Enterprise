"""
Tests for the broker delivery-cap guard in process_file_task.

The guard stops infinite OOM re-delivery loops that bypass max_retries=3:
  - reject_on_worker_lost causes broker-level re-delivery on OOM kill
  - broker re-deliveries do NOT increment self.request.retries
  - a Redis counter keyed to the upload filename caps total deliveries at 5

All imports inside process_file_task are local (deferred), so patches target
the source modules directly rather than the extraction_tasks namespace.

For bind=True Celery tasks, the task instance IS self. Tests use
push_request()/pop_request() to inject a fake request context without a
real broker, then call process_file_task.run(...) directly.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── Patch targets (all imports inside the task body are deferred) ─────────────
_P_JOB_STORE   = "spir_dynamic.services.job_store.get_job_store"
_P_FILE_RESULT = "spir_dynamic.services.job_store.FileResult"
_P_SETTINGS    = "spir_dynamic.app.config.get_settings"
_P_PIPELINE    = "spir_dynamic.app.pipeline.run_pipeline"
_P_SANITIZER   = "spir_dynamic.extraction.sanitizer.sanitize_workbook"
_P_SAFE_DELETE = "spir_dynamic.services.cleanup.safe_delete"
_P_AUDIT       = "spir_dynamic.services.audit_service.log_extraction_worker"
_P_STORAGE     = "spir_dynamic.services.storage.get_storage"
_P_REDIS       = "redis.from_url"
_P_CAP_HITS    = "spir_dynamic.monitoring.metrics.DELIVERY_CAP_HITS"
_P_SAN_RUNS    = "spir_dynamic.monitoring.metrics.SANITIZER_RUNS"
_P_SAN_SAVINGS = "spir_dynamic.monitoring.metrics.SANITIZER_SAVINGS_MB"
_P_SAN_PCT     = "spir_dynamic.monitoring.metrics.SANITIZER_REDUCTION_PCT"
_P_SAN_DUR     = "spir_dynamic.monitoring.metrics.SANITIZER_DURATION"


# ── Shared fixtures ───────────────────────────────────────────────────────────

_GOOD_PIPELINE_RESULT = {
    "total_rows": 5, "total_tags": 2, "spir_no": "S-001",
    "file_id": "file-uuid-1", "preview_cols": [], "preview_rows": [],
    "filename": "out.xlsx", "format": "A", "equipment": "", "manufacturer": "",
    "supplier": "", "spir_type": None, "eqpt_qty": 0, "spare_items": 0,
    "annexure_count": 0, "dup1_count": 0, "sap_count": 0,
}

_NOOP_SANITIZER = SimpleNamespace(
    sanitized_path=None, used_fallback=False, skip_reason="skipped",
    original_size_mb=0.1, sanitized_size_mb=0.1, reduction_pct=0, duration_s=0.01,
)


def _fake_redis(delivery_count: int) -> MagicMock:
    r = MagicMock()
    r.incr.return_value = delivery_count
    r.expire.return_value = True
    return r


@contextlib.contextmanager
def _task_context(
    upload_path: str,
    delivery_count: int,
    redis_side_effect=None,
    pipeline_result: dict | None = None,
    extra_patches: list | None = None,
):
    """
    Context manager that:
    1. Sets up all mock patches required to run process_file_task in isolation.
    2. Pushes a fake Celery request (id, retries=0) so self.request is usable.
    3. Yields the task instance; caller calls task.run(...) inside the block.
    4. Pops the request on exit.
    """
    from spir_dynamic.tasks.extraction_tasks import process_file_task

    store    = MagicMock()
    settings = SimpleNamespace(
        redis_url="redis://localhost:6379/0",
        rows_storage_path=str(Path(upload_path).parent / "rows"),
    )

    redis_stub = _fake_redis(delivery_count)
    redis_factory = redis_side_effect or (lambda url, **kw: redis_stub)

    patches = [
        patch(_P_JOB_STORE,   return_value=store),
        patch(_P_FILE_RESULT, side_effect=lambda **kw: SimpleNamespace(**kw)),
        patch(_P_SETTINGS,    return_value=settings),
        patch(_P_REDIS,       side_effect=redis_factory),
        patch(_P_SAFE_DELETE),
        patch(_P_PIPELINE,    return_value=pipeline_result or _GOOD_PIPELINE_RESULT),
        patch(_P_SANITIZER,   return_value=_NOOP_SANITIZER),
        patch(_P_AUDIT),
        patch(_P_STORAGE),
        patch(_P_CAP_HITS),
        patch(_P_SAN_RUNS),
        patch(_P_SAN_SAVINGS),
        patch(_P_SAN_PCT),
        patch(_P_SAN_DUR),
        *(extra_patches or []),
    ]

    with contextlib.ExitStack() as stack:
        mocks = [stack.enter_context(p) for p in patches]
        # Inject a fake Celery request so self.request.id / .retries work
        process_file_task.push_request(id="test-task-id", retries=0)
        try:
            yield process_file_task, store, redis_stub, mocks
        finally:
            process_file_task.pop_request()


def _run(tmp_path: Path, delivery_count: int, **ctx_kw):
    """One-liner: create upload file, run task, return result dict."""
    from spir_dynamic.tasks.extraction_tasks import process_file_task

    upload_file = tmp_path / "test_upload.xlsx"
    upload_file.write_bytes(b"PK\x03\x04" + b"\x00" * 100)

    with _task_context(str(upload_file), delivery_count, **ctx_kw) as (task, *_):
        return task.run(
            job_id="job-1", file_idx=0,
            upload_path=str(upload_file),
            filename="test_upload.xlsx",
            user_id="user-1",
        )


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestDeliveryCapGuard:

    def test_first_delivery_passes_cap(self, tmp_path):
        """delivery_count=1 — well under cap, task proceeds normally."""
        result = _run(tmp_path, delivery_count=1)
        assert result["status"] == "ok"

    def test_fifth_delivery_passes_cap(self, tmp_path):
        """delivery_count=5 — exactly at cap limit, still allowed through."""
        result = _run(tmp_path, delivery_count=5)
        assert result["status"] == "ok"

    def test_sixth_delivery_blocked(self, tmp_path):
        """delivery_count=6 — one over cap, must be rejected."""
        result = _run(tmp_path, delivery_count=6)
        assert result["status"] == "error"
        assert result.get("error") == "delivery_cap_exceeded"

    def test_high_delivery_count_blocked(self, tmp_path):
        """delivery_count=20 — pathological re-delivery loop, must be blocked."""
        result = _run(tmp_path, delivery_count=20)
        assert result["status"] == "error"
        assert result.get("error") == "delivery_cap_exceeded"

    def test_cap_error_result_has_job_and_file_idx(self, tmp_path):
        """Blocked result must carry job_id and file_idx for batch tracking."""
        result = _run(tmp_path, delivery_count=10)
        assert result["job_id"] == "job-1"
        assert result["file_idx"] == 0

    def test_cap_hit_updates_store_with_error_status(self, tmp_path):
        """store.update_result must be called with status='error' on cap breach."""
        upload_file = tmp_path / "test_upload.xlsx"
        upload_file.write_bytes(b"PK\x03\x04" + b"\x00" * 100)

        with _task_context(str(upload_file), delivery_count=7) as (task, store, *_):
            task.run(
                job_id="job-1", file_idx=0,
                upload_path=str(upload_file), filename="test_upload.xlsx",
            )

        store.update_result.assert_called_once()
        result_arg = store.update_result.call_args[0][2]
        assert result_arg.status == "error"
        assert "attempt" in result_arg.error.lower() or "delivery" in result_arg.error.lower()

    def test_cap_increments_prometheus_counter(self, tmp_path):
        """DELIVERY_CAP_HITS must be incremented exactly once on cap breach."""
        upload_file = tmp_path / "test_upload.xlsx"
        upload_file.write_bytes(b"PK\x03\x04" + b"\x00" * 100)

        mock_counter = MagicMock()
        with _task_context(
            str(upload_file), delivery_count=8,
            extra_patches=[patch(_P_CAP_HITS, mock_counter)],
        ) as (task, *_):
            task.run(
                job_id="job-1", file_idx=0,
                upload_path=str(upload_file), filename="test_upload.xlsx",
            )

        mock_counter.inc.assert_called_once()

    def test_redis_key_scoped_to_upload_filename(self, tmp_path):
        """Redis key must embed the upload filename so different files don't share counts."""
        upload_file = tmp_path / "my_special_upload.xlsx"
        upload_file.write_bytes(b"PK\x03\x04" + b"\x00" * 100)

        with _task_context(str(upload_file), delivery_count=1) as (task, _, redis_stub, _m):
            task.run(
                job_id="job-1", file_idx=0,
                upload_path=str(upload_file), filename="my_special_upload.xlsx",
            )

        incr_key = redis_stub.incr.call_args[0][0]
        assert "my_special_upload.xlsx" in incr_key
        assert incr_key.startswith("spir:dlv:")

    def test_redis_key_expires_in_24h(self, tmp_path):
        """Redis counter key must get a 86400-second (24 h) TTL."""
        upload_file = tmp_path / "expire_test.xlsx"
        upload_file.write_bytes(b"PK\x03\x04" + b"\x00" * 100)

        with _task_context(str(upload_file), delivery_count=1) as (task, _, redis_stub, _m):
            task.run(
                job_id="job-1", file_idx=0,
                upload_path=str(upload_file), filename="expire_test.xlsx",
            )

        _, expire_ttl = redis_stub.expire.call_args[0]
        assert expire_ttl == 86400

    def test_redis_unavailable_does_not_block_task(self, tmp_path):
        """If Redis is unreachable, the cap guard is bypassed and extraction proceeds."""
        upload_file = tmp_path / "redis_down.xlsx"
        upload_file.write_bytes(b"PK\x03\x04" + b"\x00" * 100)

        def _raise(url, **kw):
            raise ConnectionError("Redis is down")

        with _task_context(
            str(upload_file), delivery_count=0, redis_side_effect=_raise
        ) as (task, *_):
            result = task.run(
                job_id="job-1", file_idx=0,
                upload_path=str(upload_file), filename="redis_down.xlsx",
            )

        assert result["status"] == "ok"

    def test_upload_file_deleted_on_cap_breach(self, tmp_path):
        """The upload file must be deleted when the cap fires to prevent disk leaks."""
        upload_file = tmp_path / "cap_delete.xlsx"
        upload_file.write_bytes(b"PK\x03\x04" + b"\x00" * 100)

        mock_delete = MagicMock()
        with _task_context(
            str(upload_file), delivery_count=9,
            extra_patches=[patch(_P_SAFE_DELETE, mock_delete)],
        ) as (task, *_):
            task.run(
                job_id="job-1", file_idx=0,
                upload_path=str(upload_file), filename="cap_delete.xlsx",
            )

        mock_delete.assert_called_once()
        deleted_path = mock_delete.call_args[0][0]
        assert deleted_path == upload_file

    def test_cap_boundary_at_exactly_5_and_6(self, tmp_path):
        """Boundary: delivery_count=5 passes, delivery_count=6 is the first to block."""
        result_5 = _run(tmp_path, delivery_count=5)
        assert result_5["status"] == "ok", "delivery_count=5 must not be blocked"

        sub = tmp_path / "second"
        sub.mkdir()
        result_6 = _run(sub, delivery_count=6)
        assert result_6["status"] == "error", "delivery_count=6 must be blocked"
        assert result_6["error"] == "delivery_cap_exceeded"
