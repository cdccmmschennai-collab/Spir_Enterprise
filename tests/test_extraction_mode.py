"""
Explicit Standard | Saipem extraction mode (services/extraction_mode).

    standard → generic path, even for a workbook that looks like a Saipem form
    saipem   → Saipem path, or SaipemFormError — never a Standard fallback
    omitted  → structural auto-detection (callers that predate the selector)

The mode must survive every entry point: sync /api/extract, the large-file
Celery hand-off, batch multipart / register / per-file upload, direct
(presigned) uploads, the Celery task (and its retries) and the in-process
batch fallback. Combine refuses non-Standard rows.

Endpoint coroutines are called directly: starlette's TestClient needs httpx,
which this environment lacks (the TestClient suites skip here).
"""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import openpyxl
import pytest
from fastapi import HTTPException

from spir_dynamic.app.auth import TokenData
from spir_dynamic.extraction.output_schema import OUTPUT_COLS
from spir_dynamic.extraction.saipem.errors import (
    NO_SAIPEM_ROWS_MESSAGE,
    NOT_SAIPEM_MESSAGE,
    SaipemFormError,
)
from spir_dynamic.extraction.file_validator import ValidationError
from spir_dynamic.services.extraction_mode import (
    InvalidExtractionMode,
    resolve_extraction_mode,
)
from spir_dynamic.services.planning_plant import PlanningPlant
from tests.saipem_fixtures import Line, build_instruction, saipem_workbook
from tests.test_source_objects import MemoryObjectStorage, _PLANT_FIELDS, _slot_updates, worker_ctx

NGL = PlanningPlant("2400", "NGL Mesaieed")
TD = TokenData(username="u", user_id="u1", jti="j", role="user")
REPO_ROOT = Path(__file__).resolve().parents[1]
GENERIC_WORKBOOK = REPO_ROOT / "templates" / "inputs" / "VEN-4460-DGTYP-5-43-1202-A.xlsm"
needs_generic = pytest.mark.skipif(not GENERIC_WORKBOOK.exists(), reason="generic template not present")


# ── Mode values ──────────────────────────────────────────────────────────────

class TestResolveMode:
    @pytest.mark.parametrize("raw, mode", [
        ("standard", "standard"), ("saipem", "saipem"), (" SAIPEM ", "saipem"), ("Standard", "standard"),
        (None, None), ("", None), ("   ", None),
    ])
    def test_valid(self, raw, mode):
        assert resolve_extraction_mode(raw) == mode

    @pytest.mark.parametrize("raw", ["auto", "generic", "saipem2", "1"])
    def test_invalid_rejected(self, raw):
        with pytest.raises(InvalidExtractionMode):
            resolve_extraction_mode(raw)


# ── run_pipeline routing ─────────────────────────────────────────────────────

def _save(wb, tmp_path: Path, name: str = "form.xlsx") -> Path:
    path = tmp_path / name
    wb.save(path)
    return path


def _pipeline(path: Path, mode, *, forbid_saipem=False, forbid_standard=False):
    """run_pipeline with storage captured and currency off; optionally forbid one path."""
    from spir_dynamic.app.pipeline import run_pipeline
    storage = MagicMock()
    boom = AssertionError("this path must not run")
    patches = [
        patch("spir_dynamic.app.pipeline.get_storage", return_value=storage),
        patch("spir_dynamic.extraction.saipem.service.get_storage", return_value=storage),
        patch("spir_dynamic.app.pipeline._apply_currency_conversion", return_value=None),
    ]
    if forbid_saipem:
        patches.append(patch("spir_dynamic.app.pipeline.run_saipem_pipeline", side_effect=boom))
    if forbid_standard:
        patches.append(patch("spir_dynamic.app.pipeline.extract_workbook", side_effect=boom))
    with contextlib_exit(patches):
        kwargs = {} if mode is None else {"extraction_mode": mode}
        result = run_pipeline(path, path.name, NGL, **kwargs)
    return result, storage


class contextlib_exit:
    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        for p in self.patches:
            p.start()

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


class TestPipelineRouting:
    def test_saipem_selected_uses_saipem_path(self, tmp_path):
        result, storage = _pipeline(_save(saipem_workbook(), tmp_path), "saipem", forbid_standard=True)
        assert result["format"].startswith("SAIPEM") and result["extraction_mode"] == "saipem"
        assert result["total_tags"] == 4
        assert storage.put.call_count == 1

    def test_standard_selected_never_switches_to_saipem(self, tmp_path):
        # A workbook the Saipem detector would claim — Standard must still run the generic path.
        result, _ = _pipeline(_save(saipem_workbook(), tmp_path), "standard", forbid_saipem=True)
        assert result["preview_cols"] == OUTPUT_COLS
        assert result["extraction_mode"] == "standard"
        assert not result["format"].startswith("SAIPEM")

    @needs_generic
    def test_standard_selected_generic_workbook(self, tmp_path):
        result, _ = _pipeline(GENERIC_WORKBOOK, "standard", forbid_saipem=True)
        assert result["preview_cols"] == OUTPUT_COLS and result["total_rows"] > 0

    def test_mode_omitted_keeps_auto_detection(self, tmp_path):
        result, _ = _pipeline(_save(saipem_workbook(), tmp_path), None, forbid_standard=True)
        assert result["format"].startswith("SAIPEM")

    @needs_generic
    def test_mode_omitted_generic_workbook_stays_standard(self):
        result, _ = _pipeline(GENERIC_WORKBOOK, None, forbid_saipem=True)
        assert result["preview_cols"] == OUTPUT_COLS

    def test_invalid_mode_rejected_before_reading(self, tmp_path):
        from spir_dynamic.app.pipeline import run_pipeline
        with pytest.raises(InvalidExtractionMode):
            run_pipeline(tmp_path / "never-opened.xlsx", "x.xlsx", NGL, extraction_mode="auto")


class TestSaipemValidation:
    def _plain_table(self):
        wb = openpyxl.Workbook()
        wb.active.append(["TAG NO", "DESCRIPTION OF PARTS", "QTY"])
        wb.active.append(["P-101", "SEAL", 1])
        return wb

    def test_not_a_saipem_form_fails_without_fallback(self, tmp_path):
        with pytest.raises(SaipemFormError) as err:
            _pipeline(_save(self._plain_table(), tmp_path), "saipem", forbid_standard=True)
        # The user sees one short, actionable line — no per-sheet detail.
        assert str(err.value) == NOT_SAIPEM_MESSAGE
        assert isinstance(err.value, ValidationError)       # → HTTP 422 on the sync route

    def test_detailed_reason_is_logged_not_shown(self, tmp_path):
        from spir_dynamic.app import pipeline
        with patch.object(pipeline.log, "info") as info, pytest.raises(SaipemFormError):
            _pipeline(_save(self._plain_table(), tmp_path, "plain.xlsx"), "saipem", forbid_standard=True)
        logged = [c for c in info.call_args_list if c.args and c.args[0] == "saipem.not_detected"]
        assert logged and "ITEM TAG No. (MAIN ITEM)" in logged[0].kwargs["detail"]
        assert logged[0].kwargs["filename"] == "plain.xlsx"

    def test_diagnosis_names_missing_labels(self):
        from spir_dynamic.extraction.saipem.detector import explain_not_saipem
        assert "'Sheet' is missing ITEM TAG No. (MAIN ITEM)" in explain_not_saipem(self._plain_table())

    def test_example_only_workbook_rejected(self, tmp_path):
        from spir_dynamic.extraction.saipem.detector import explain_not_saipem
        wb = saipem_workbook(with_instruction=False)
        wb["SPIR Form"].title = "SPIR Form Sample"
        with pytest.raises(SaipemFormError) as err:
            _pipeline(_save(wb, tmp_path), "saipem", forbid_standard=True)
        assert str(err.value) == NOT_SAIPEM_MESSAGE
        assert "example / sample / instruction" in explain_not_saipem(wb)

    def test_instruction_sheet_only_rejected(self, tmp_path):
        from spir_dynamic.extraction.saipem.detector import explain_not_saipem
        wb = openpyxl.Workbook()
        build_instruction(wb.active)
        wb.active.title = "Notes"
        with pytest.raises(SaipemFormError) as err:
            _pipeline(_save(wb, tmp_path), "saipem", forbid_standard=True)
        assert str(err.value) == NOT_SAIPEM_MESSAGE
        assert "Saipem form layout" in explain_not_saipem(wb)

    def test_recognised_form_without_rows_is_not_exported(self, tmp_path):
        wb = saipem_workbook(lines=[Line({"pos": "0001", "desc": "SEAL"}, per_unit={})])
        from spir_dynamic.app.pipeline import run_pipeline
        storage = MagicMock()
        with patch("spir_dynamic.extraction.saipem.service.get_storage", return_value=storage), \
             patch("spir_dynamic.app.pipeline.extract_workbook", side_effect=AssertionError("no fallback")):
            with pytest.raises(SaipemFormError) as err:
                run_pipeline(_save(wb, tmp_path), "form.xlsx", NGL, extraction_mode="saipem")
        assert str(err.value) == NO_SAIPEM_ROWS_MESSAGE
        storage.put.assert_not_called()


# ── Celery task ──────────────────────────────────────────────────────────────

class TestWorkerMode:
    KEY = "job-1_000_form.xlsx"

    def _run(self, tmp_path, pipeline=None, **kw):
        st = MemoryObjectStorage()
        st.put_bytes(self.KEY, b"wb")
        with worker_ctx(tmp_path, st, pipeline=pipeline) as (task, store, pipe):
            with patch.object(task, "retry", side_effect=RuntimeError("retry")) as retry:
                try:
                    out = task.run(job_id="job-1", file_idx=0, source_key=self.KEY, filename="form.xlsx",
                                   **_PLANT_FIELDS, **kw)
                except RuntimeError:
                    out = "retried"
        return out, store, pipe, retry, st

    @pytest.mark.parametrize("mode", ["saipem", "standard"])
    def test_mode_reaches_pipeline(self, tmp_path, mode):
        out, _, pipe, _, _ = self._run(tmp_path, extraction_mode=mode)
        assert out["status"] == "ok"
        assert pipe.call_args.kwargs == {"extraction_mode": mode}

    def test_no_mode_calls_pipeline_as_before(self, tmp_path):
        out, _, pipe, _, _ = self._run(tmp_path)
        assert out["status"] == "ok" and pipe.call_args.kwargs == {} and len(pipe.call_args.args) == 3

    def test_invalid_mode_fails_slot_without_extraction(self, tmp_path):
        out, store, pipe, retry, st = self._run(tmp_path, extraction_mode="bogus")
        assert out["status"] == "error" and "extraction_mode" in out["error"]
        pipe.assert_not_called()
        retry.assert_not_called()
        assert self.KEY not in st.objects
        assert [u.status for u in _slot_updates(store)] == ["error"]

    def test_saipem_validation_error_fails_at_once(self, tmp_path):
        pipe = MagicMock(side_effect=SaipemFormError("This workbook is not a supported Saipem SPIR form"))
        out, store, _, retry, st = self._run(tmp_path, pipeline=pipe, extraction_mode="saipem")
        assert out["status"] == "error" and "Saipem" in out["error"]
        retry.assert_not_called()                     # deterministic — no retry, no fallback
        assert self.KEY not in st.objects
        assert _slot_updates(store)[-1].error.startswith("This workbook is not")

    def test_other_errors_still_retry(self, tmp_path):
        out, _, _, retry, st = self._run(tmp_path, pipeline=MagicMock(side_effect=OSError("disk")),
                                         extraction_mode="saipem")
        assert out == "retried" and retry.call_count == 1
        assert self.KEY in st.objects                 # kept for the retry, which re-runs with the same kwargs


# ── Dispatch and in-process batch fallback ───────────────────────────────────

class TestBatchPropagation:
    def _dispatch(self, **kw):
        from spir_dynamic.app.batch_router import _dispatch_celery
        task = MagicMock()
        files = [(f"j_{i:03d}_f{i}.xlsx", f"f{i}.xlsx", 1) for i in range(3)]
        cfg = SimpleNamespace(large_file_threshold_mb=100, giant_file_threshold_mb=500)
        with patch("spir_dynamic.tasks.extraction_tasks.process_file_task", task), \
             patch("spir_dynamic.monitoring.metrics.GIANT_FILES_ROUTED"):
            _dispatch_celery("j", files, cfg, planning_plant=NGL, **kw)
        return [c.kwargs["kwargs"] for c in task.apply_async.call_args_list]

    def test_dispatch_carries_mode_for_every_file(self):
        assert [k["extraction_mode"] for k in self._dispatch(extraction_mode="saipem")] == ["saipem"] * 3

    def test_dispatch_without_mode_is_unchanged(self):
        assert all("extraction_mode" not in k for k in self._dispatch())

    def test_fallback_batch_passes_mode(self, tmp_path):
        from spir_dynamic.app.batch_router import _process_batch_from_storage
        from spir_dynamic.services.job_store import JobStore
        storage = MemoryObjectStorage()
        names = ["a.xlsx", "b.xlsx"]
        for i, n in enumerate(names):
            storage.put_bytes(f"job_{i:03d}_{n}", b"x")
        store = JobStore()
        store.create("job", names)
        seen = []

        def fake_pipeline(path, filename, plant, **kw):
            seen.append((filename, kw))
            return {"total_rows": 1, "total_tags": 1, "spir_no": "S", "file_id": filename}

        with patch("spir_dynamic.services.source_objects.get_source_storage", return_value=storage), \
             patch("spir_dynamic.app.batch_router.get_job_store", return_value=store), \
             patch("spir_dynamic.app.batch_router.run_pipeline", side_effect=fake_pipeline), \
             patch("spir_dynamic.services.source_objects.scratch_dir", return_value=tmp_path / "scratch"):
            asyncio.run(_process_batch_from_storage(
                "job", [(f"job_{i:03d}_{n}", n, 1) for i, n in enumerate(names)],
                planning_plant=NGL, extraction_mode="saipem",
            ))
        assert seen == [(n, {"extraction_mode": "saipem"}) for n in names]


# ── API endpoints (coroutines called directly) ───────────────────────────────

def _cfg(**kw):
    base = dict(max_file_size_mb=100, absolute_max_file_size_mb=1000, upload_chunk_size=1 << 20,
                celery_enabled=True, extraction_timeout_seconds=60, batch_max_files=20,
                batch_ttl_seconds=60, large_file_threshold_mb=100, giant_file_threshold_mb=500)
    base.update(kw)
    return SimpleNamespace(**base)


class TestSyncExtractEndpoint:
    def _call(self, tmp_path, mode, *, queue="normal"):
        from spir_dynamic.app import routes
        tmp = tmp_path / "up.xlsx"
        tmp.write_bytes(b"x")
        pipeline = MagicMock(return_value={"file_id": "f", "total_rows": 1, "total_tags": 1})
        enqueue = AsyncMock(return_value=("job-9", queue))
        with patch.object(routes, "get_settings", return_value=_cfg()), \
             patch.object(routes, "_stream_to_temp", AsyncMock(return_value=(tmp, 1))), \
             patch.object(routes, "route_queue", return_value=(queue, {})), \
             patch.object(routes, "run_pipeline", pipeline), \
             patch.object(routes, "_enqueue_single_file", enqueue), \
             patch.object(routes, "_save_rows_to_disk", return_value=None), \
             patch.object(routes, "_get_semaphore", side_effect=lambda: asyncio.Semaphore(1)), \
             patch.object(routes, "_get_executor", return_value=None), \
             patch("spir_dynamic.services.audit_service.log_extraction", AsyncMock()):
            out = asyncio.run(routes.extract(
                request=MagicMock(), background_tasks=MagicMock(), file=MagicMock(filename="a.xlsx"),
                planning_plant="2400", planning_plant_description="NGL Mesaieed",
                extraction_mode=mode, td=TD,
            ))
        return out, pipeline, enqueue

    def test_sync_path_gets_mode(self, tmp_path):
        _, pipeline, _ = self._call(tmp_path, "saipem")
        assert pipeline.call_args.args[-1] == "saipem"

    def test_large_file_handoff_gets_mode(self, tmp_path):
        out, pipeline, enqueue = self._call(tmp_path, "standard", queue="heavy")
        assert out.status_code == 202
        pipeline.assert_not_called()
        assert enqueue.call_args.args[-1] == "standard"

    def test_invalid_mode_422_before_upload(self, tmp_path):
        from spir_dynamic.app import routes
        stream = AsyncMock()
        with patch.object(routes, "_stream_to_temp", stream):
            with pytest.raises(HTTPException) as err:
                asyncio.run(routes.extract(
                    request=MagicMock(), background_tasks=MagicMock(), file=MagicMock(filename="a.xlsx"),
                    planning_plant="2400", planning_plant_description="NGL Mesaieed",
                    extraction_mode="auto", td=TD,
                ))
        assert err.value.status_code == 422
        stream.assert_not_called()

    def test_enqueue_single_file_dispatches_mode(self, tmp_path):
        from spir_dynamic.app import routes
        dispatch = MagicMock(return_value=["heavy"])
        with patch.object(routes, "store_source_upload"), \
             patch.object(routes, "get_job_store"), \
             patch.object(routes, "_dispatch_celery", dispatch), \
             patch.object(routes, "_persist_job_to_db", AsyncMock()):
            asyncio.run(routes._enqueue_single_file(tmp_path / "a.xlsx", "a.xlsx", 1, _cfg(), TD, NGL, "saipem"))
        assert dispatch.call_args.kwargs["extraction_mode"] == "saipem"


class TestBatchEndpoints:
    def test_register_rejects_invalid_mode(self):
        from spir_dynamic.app.batch_router import RegisterRequest, batch_register
        body = RegisterRequest(filenames=["a.xlsx"], **_PLANT_FIELDS, extraction_mode="bogus")
        with pytest.raises(HTTPException) as err:
            asyncio.run(batch_register(body, td=TD))
        assert err.value.status_code == 422

    def test_register_accepts_mode(self):
        from spir_dynamic.app import batch_router
        body = batch_router.RegisterRequest(filenames=["a.xlsx"], **_PLANT_FIELDS, extraction_mode="saipem")
        with patch.object(batch_router, "get_settings", return_value=_cfg()), \
             patch.object(batch_router, "get_job_store"), \
             patch.object(batch_router, "_persist_job_to_db", AsyncMock()):
            out = asyncio.run(batch_router.batch_register(body, td=TD))
        assert out["status"] == "ready"

    @pytest.mark.parametrize("celery", [True, False])
    def test_per_file_upload_carries_mode(self, celery):
        from spir_dynamic.app import batch_router
        job = SimpleNamespace(user_id="u1", total=1, results=[SimpleNamespace(status="pending", filename="a.xlsx")])
        store = MagicMock()
        store.get.return_value = job
        dispatch = MagicMock(return_value=["normal"])
        fallback = MagicMock(return_value=asyncio.sleep(0))
        with patch.object(batch_router, "get_settings", return_value=_cfg(celery_enabled=celery)), \
             patch.object(batch_router, "get_job_store", return_value=store), \
             patch.object(batch_router, "_stream_batch_upload", AsyncMock(return_value=("k", 1))), \
             patch.object(batch_router, "_dispatch_celery", dispatch), \
             patch.object(batch_router, "_process_batch_from_storage", fallback):
            asyncio.run(batch_router.batch_upload_file(
                "job", file=MagicMock(), file_idx=0, **_PLANT_FIELDS, extraction_mode="saipem", td=TD,
            ))
        called = dispatch if celery else fallback
        assert called.call_args.kwargs["extraction_mode"] == "saipem"

    def test_per_file_upload_invalid_mode_fails_slot(self):
        from spir_dynamic.app import batch_router
        job = SimpleNamespace(user_id="u1", total=1, results=[SimpleNamespace(status="pending", filename="a.xlsx")])
        store = MagicMock()
        store.get.return_value = job
        with patch.object(batch_router, "get_settings", return_value=_cfg()), \
             patch.object(batch_router, "get_job_store", return_value=store):
            with pytest.raises(HTTPException) as err:
                asyncio.run(batch_router.batch_upload_file(
                    "job", file=MagicMock(), file_idx=0, **_PLANT_FIELDS, extraction_mode="nope", td=TD,
                ))
        assert err.value.status_code == 422
        assert store.update_result.call_args.args[2].status == "error"

    @pytest.mark.parametrize("celery", [True, False])
    def test_multipart_batch_carries_mode(self, celery):
        from spir_dynamic.app import batch_router
        dispatch = MagicMock(return_value=["normal"])
        fallback = MagicMock(return_value=asyncio.sleep(0))
        files = [MagicMock(filename="a.xlsx"), MagicMock(filename="b.xlsx")]
        with patch.object(batch_router, "get_settings", return_value=_cfg(celery_enabled=celery)), \
             patch.object(batch_router, "get_job_store"), \
             patch.object(batch_router, "_stream_batch_upload", AsyncMock(return_value=("k", 1))), \
             patch.object(batch_router, "_dispatch_celery", dispatch), \
             patch.object(batch_router, "_process_batch_from_storage", fallback), \
             patch.object(batch_router, "_persist_job_to_db", AsyncMock()):
            asyncio.run(batch_router.batch_extract(files, **_PLANT_FIELDS, extraction_mode="standard", td=TD))
        called = dispatch if celery else fallback
        assert called.call_args.kwargs["extraction_mode"] == "standard"


class TestDirectUpload:
    def test_session_keeps_mode_and_reads_legacy_records(self):
        from spir_dynamic.services.direct_upload import UploadSession
        fields = dict(job_id="j", file_idx=0, filename="a.xlsx", size=1, source_key="k", upload_id="u",
                      part_size=1, part_count=1, user_id="u1", queue="heavy")
        s = UploadSession(**fields, extraction_mode="saipem")
        assert UploadSession.from_dict(json.loads(json.dumps(s.to_dict()))).extraction_mode == "saipem"
        assert UploadSession.from_dict(fields).extraction_mode == ""      # recorded before the selector

    def test_plan_upload_records_mode(self):
        from spir_dynamic.services import direct_upload
        storage = MagicMock()
        storage.create_multipart_upload.return_value = "up-1"
        storage.presign_upload_part.return_value = "https://x"
        with patch.object(direct_upload, "index_open_upload"):
            session, _ = direct_upload.plan_upload(
                storage, job_id="j", file_idx=0, filename="a.xlsx", size=10, user_id="u", queue="heavy",
                part_size=10, url_ttl=60, planning_plant="2400", planning_plant_description="NGL Mesaieed",
                extraction_mode="saipem",
            )
        assert session.extraction_mode == "saipem"

    def test_initiate_rejects_invalid_mode(self):
        from spir_dynamic.app.upload_router import InitiateRequest, initiate_upload
        body = InitiateRequest(filename="a.xlsx", size=10, **_PLANT_FIELDS, extraction_mode="bogus")
        with pytest.raises(HTTPException) as err:
            asyncio.run(initiate_upload(body, td=TD))
        assert err.value.status_code == 422

    def test_initiate_hands_mode_to_session(self):
        from spir_dynamic.app import upload_router
        body = upload_router.InitiateRequest(filename="a.xlsx", size=10, **_PLANT_FIELDS, extraction_mode="saipem")
        plan = MagicMock(return_value=(MagicMock(to_dict=dict, source_key="k", part_count=1, part_size=10), []))
        with patch.object(upload_router, "get_settings", return_value=_cfg(
                direct_upload_configured=True, direct_upload_part_size_mb=1, direct_upload_url_ttl_seconds=60)), \
             patch.object(upload_router, "route_queue", return_value=("heavy", {})), \
             patch.object(upload_router, "direct_upload_storage", return_value=MagicMock()), \
             patch.object(upload_router, "plan_upload", plan), \
             patch.object(upload_router, "get_job_store"), \
             patch.object(upload_router, "_persist_job_to_db", AsyncMock()):
            out = asyncio.run(upload_router.initiate_upload(body, td=TD))
        assert out["mode"] == "direct"
        assert plan.call_args.kwargs["extraction_mode"] == "saipem"

    def test_complete_dispatches_session_mode(self):
        from spir_dynamic.app import upload_router
        from spir_dynamic.services.direct_upload import UploadSession
        session = UploadSession(job_id="j", file_idx=0, filename="a.xlsx", size=10, source_key="k",
                                upload_id="u", part_size=10, part_count=1, user_id="u1", queue="heavy",
                                **_PLANT_FIELDS, extraction_mode="saipem")
        job = SimpleNamespace(user_id="u1", total=1, results=[SimpleNamespace(status="uploading", filename="a.xlsx")])
        store = MagicMock()
        store.get.return_value = job
        store.get_upload.return_value = session.to_dict()
        store.transition_upload.return_value = True
        dispatch = MagicMock(return_value=["heavy"])
        with patch.object(upload_router, "get_settings", return_value=_cfg()), \
             patch.object(upload_router, "get_job_store", return_value=store), \
             patch.object(upload_router, "direct_upload_storage", return_value=MagicMock()), \
             patch.object(upload_router, "finalize_upload", return_value=SimpleNamespace(size=10)), \
             patch.object(upload_router, "_dispatch_celery", dispatch):
            asyncio.run(upload_router.complete_upload("j", 0, td=TD))
        assert dispatch.call_args.kwargs["extraction_mode"] == "saipem"


# ── Combine refuses non-Standard rows ────────────────────────────────────────

class TestCombineGuard:
    def test_standard_and_legacy_payloads_allowed(self):
        from spir_dynamic.app.batch_router import ensure_combinable
        ensure_combinable(OUTPUT_COLS, "a")      # standard
        ensure_combinable(None, "b")             # stored before columns were recorded
        ensure_combinable([], "c")

    def test_saipem_rows_refused(self):
        from spir_dynamic.app import batch_router
        payload = {"cols": ["SPIR No.", "ITEM TAG No. (MAIN ITEM)"], "rows": [["x", "y"]], "filename": "s.xlsx"}
        storage = MagicMock()
        storage.get.return_value = (json.dumps(payload).encode(), "rows.json")
        with patch.object(batch_router, "get_storage", return_value=storage):
            with pytest.raises(batch_router.CombineUnsupported) as err:
                batch_router._do_combine(["fid-1", "fid-2"])
        assert "s.xlsx" in str(err.value) and "Standard" in str(err.value)
        storage.put.assert_not_called()

    def test_batch_combine_endpoint_answers_422(self):
        from spir_dynamic.app import batch_router
        job = SimpleNamespace(user_id="u1", results=[SimpleNamespace(file_id="f1", status="ok"),
                                                     SimpleNamespace(file_id="f2", status="ok")])
        store = MagicMock()
        store.get.return_value = job
        with patch.object(batch_router, "get_job_store", return_value=store), \
             patch.object(batch_router, "_do_combine", side_effect=batch_router.CombineUnsupported("no")):
            with pytest.raises(HTTPException) as err:
                asyncio.run(batch_router.batch_combine("job", batch_router.CombineRequest(file_ids=["f1", "f2"]), td=TD))
        assert err.value.status_code == 422


def test_saipem_result_payload_reaches_polling_endpoint():
    """The worker stores mode + warnings; /api/batch/{id}/result returns them."""
    from spir_dynamic.app import batch_router
    job = SimpleNamespace(user_id="u1", total=1, completed=1,
                          results=[SimpleNamespace(status="ok", file_id="f", filename="a.xlsx",
                                                   spir_no="S", total_rows=3, total_tags=1)])
    store = MagicMock()
    store.get.return_value = job
    payload = {"cols": ["SPIR No."], "rows": [], "extraction_mode": "saipem", "warnings": ["note"]}
    storage = MagicMock()
    storage.get.return_value = (json.dumps(payload).encode(), "rows.json")
    with patch.object(batch_router, "get_job_store", return_value=store), \
         patch.object(batch_router, "get_storage", return_value=storage):
        out = asyncio.run(batch_router.batch_single_result("job", td=TD))
    assert out["extraction_mode"] == "saipem" and out["warnings"] == ["note"]


# ── Saipem uses no Planning Plant; Standard (and no mode) still require one ──

class TestSaipemNeedsNoPlant:
    def test_pipeline_saipem_without_plant(self, tmp_path):
        from spir_dynamic.app.pipeline import run_pipeline
        storage = MagicMock()
        with patch("spir_dynamic.extraction.saipem.service.get_storage", return_value=storage):
            out = run_pipeline(_save(saipem_workbook(), tmp_path), "form.xlsx", None, extraction_mode="saipem")
        assert out["total_tags"] == 4 and out["plant"] is None and out["plant_description"] is None
        assert "PLANNING PLANT" not in out["preview_cols"]

    @pytest.mark.parametrize("mode", ["standard", None])
    def test_pipeline_standard_or_no_mode_still_requires_plant(self, tmp_path, mode):
        from spir_dynamic.app.pipeline import run_pipeline
        from spir_dynamic.services.planning_plant import InvalidPlanningPlant
        kwargs = {} if mode is None else {"extraction_mode": mode}
        with pytest.raises(InvalidPlanningPlant):
            run_pipeline(tmp_path / "never-opened.xlsx", "x.xlsx", None, **kwargs)

    def test_request_plant_rule(self):
        from spir_dynamic.app.batch_router import request_plant_or_422
        assert request_plant_or_422(None, None, "saipem") is None
        assert request_plant_or_422("2400", "NGL Mesaieed", "saipem") is None      # ignored for Saipem
        assert request_plant_or_422("2400", "NGL Mesaieed", "standard") == NGL
        for mode in ("standard", None):
            with pytest.raises(HTTPException) as err:
                request_plant_or_422(None, None, mode)
            assert err.value.status_code == 422

    def test_dispatch_saipem_sends_no_plant(self):
        from spir_dynamic.app.batch_router import _dispatch_celery
        task = MagicMock()
        cfg = SimpleNamespace(large_file_threshold_mb=100, giant_file_threshold_mb=500)
        with patch("spir_dynamic.tasks.extraction_tasks.process_file_task", task), \
             patch("spir_dynamic.monitoring.metrics.GIANT_FILES_ROUTED"):
            _dispatch_celery("j", [("k", "a.xlsx", 1)], cfg, planning_plant=None, extraction_mode="saipem")
        assert task.apply_async.call_args.kwargs["kwargs"] == {"extraction_mode": "saipem"}

    def test_worker_saipem_without_plant(self, tmp_path):
        st = MemoryObjectStorage()
        st.put_bytes("job-1_000_form.xlsx", b"wb")
        with worker_ctx(tmp_path, st) as (task, _store, pipe):
            out = task.run(job_id="job-1", file_idx=0, source_key="job-1_000_form.xlsx",
                           filename="form.xlsx", extraction_mode="saipem")
        assert out["status"] == "ok"
        assert pipe.call_args.args[2] is None and pipe.call_args.kwargs == {"extraction_mode": "saipem"}

    def test_worker_standard_without_plant_fails(self, tmp_path):
        st = MemoryObjectStorage()
        st.put_bytes("job-1_000_form.xlsx", b"wb")
        with worker_ctx(tmp_path, st) as (task, _store, pipe):
            out = task.run(job_id="job-1", file_idx=0, source_key="job-1_000_form.xlsx",
                           filename="form.xlsx", extraction_mode="standard")
        assert out["status"] == "error" and "planning_plant" in out["error"]
        pipe.assert_not_called()

    def test_sync_extract_saipem_without_plant(self, tmp_path):
        from spir_dynamic.app import routes
        tmp = tmp_path / "up.xlsx"
        tmp.write_bytes(b"x")
        pipeline = MagicMock(return_value={"file_id": "f", "total_rows": 1, "total_tags": 1})
        with patch.object(routes, "get_settings", return_value=_cfg()), \
             patch.object(routes, "_stream_to_temp", AsyncMock(return_value=(tmp, 1))), \
             patch.object(routes, "route_queue", return_value=("normal", {})), \
             patch.object(routes, "run_pipeline", pipeline), \
             patch.object(routes, "_save_rows_to_disk", return_value=None), \
             patch.object(routes, "_get_semaphore", side_effect=lambda: asyncio.Semaphore(1)), \
             patch.object(routes, "_get_executor", return_value=None), \
             patch("spir_dynamic.services.audit_service.log_extraction", AsyncMock()):
            asyncio.run(routes.extract(
                request=MagicMock(), background_tasks=MagicMock(), file=MagicMock(filename="a.xlsx"),
                planning_plant=None, planning_plant_description=None, extraction_mode="saipem", td=TD,
            ))
        assert pipeline.call_args.args[2] is None and pipeline.call_args.args[3] == "saipem"

    def test_register_saipem_without_plant(self):
        from spir_dynamic.app import batch_router
        with patch.object(batch_router, "get_settings", return_value=_cfg()), \
             patch.object(batch_router, "get_job_store"), \
             patch.object(batch_router, "_persist_job_to_db", AsyncMock()):
            out = asyncio.run(batch_router.batch_register(
                batch_router.RegisterRequest(filenames=["a.xlsx"], extraction_mode="saipem"), td=TD))
            assert out["status"] == "ready"
            with pytest.raises(HTTPException):
                asyncio.run(batch_router.batch_register(
                    batch_router.RegisterRequest(filenames=["a.xlsx"], extraction_mode="standard"), td=TD))

    def test_direct_upload_saipem_without_plant(self):
        from spir_dynamic.app import upload_router
        from spir_dynamic.services.direct_upload import UploadSession
        body = upload_router.InitiateRequest(filename="a.xlsx", size=10, extraction_mode="saipem")
        plan = MagicMock(return_value=(MagicMock(to_dict=dict, source_key="k", part_count=1, part_size=10), []))
        with patch.object(upload_router, "get_settings", return_value=_cfg(
                direct_upload_configured=True, direct_upload_part_size_mb=1, direct_upload_url_ttl_seconds=60)), \
             patch.object(upload_router, "route_queue", return_value=("heavy", {})), \
             patch.object(upload_router, "direct_upload_storage", return_value=MagicMock()), \
             patch.object(upload_router, "plan_upload", plan), \
             patch.object(upload_router, "get_job_store"), \
             patch.object(upload_router, "_persist_job_to_db", AsyncMock()):
            asyncio.run(upload_router.initiate_upload(body, td=TD))
        assert plan.call_args.kwargs["planning_plant"] == "" and plan.call_args.kwargs["extraction_mode"] == "saipem"

        session = UploadSession(job_id="j", file_idx=0, filename="a.xlsx", size=10, source_key="k",
                                upload_id="u", part_size=10, part_count=1, user_id="u1", queue="heavy",
                                extraction_mode="saipem")
        job = SimpleNamespace(user_id="u1", total=1, results=[SimpleNamespace(status="uploading", filename="a.xlsx")])
        store = MagicMock()
        store.get.return_value = job
        store.get_upload.return_value = session.to_dict()
        store.transition_upload.return_value = True
        dispatch = MagicMock(return_value=["heavy"])
        with patch.object(upload_router, "get_settings", return_value=_cfg()), \
             patch.object(upload_router, "get_job_store", return_value=store), \
             patch.object(upload_router, "direct_upload_storage", return_value=MagicMock()), \
             patch.object(upload_router, "finalize_upload", return_value=SimpleNamespace(size=10)), \
             patch.object(upload_router, "_dispatch_celery", dispatch):
            asyncio.run(upload_router.complete_upload("j", 0, td=TD))
        assert dispatch.call_args.kwargs["planning_plant"] is None
        assert dispatch.call_args.kwargs["extraction_mode"] == "saipem"
