"""
Integration tests for the storage lifecycle cleanup task.

All tests use real filesystem operations on tmp_path directories.
DB-dependent tests mock _fetch_known_file_ids so no live database is needed.

Coverage:
  Phase 1 — JSON expiry
    1. Old JSON file is deleted
    2. Recent JSON file is preserved
    3. File modified within 1h safety guard is always preserved
    4. Non-JSON files are untouched
    5. Missing directory is handled gracefully

  Phase 2 — Orphan JSON cleanup
    6. Orphan file (no DB record) is deleted
    7. Known file (has DB record) is preserved
    8. Skipped when database_url is empty
    9. DB query failure produces a skip result (no crash)

  Phase 3 — Stale uploads
   10. Old upload file is deleted
   11. Recent upload file is preserved
   12. Hidden files are skipped

  Phase 4 — Disk metrics
   13. Metrics reflect actual file counts and sizes

  Integration
   14. Full lifecycle_cleanup_task dry-run logs but deletes nothing
   15. Full lifecycle_cleanup_task live run deletes expired JSON
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_json(directory: Path, name: str, age_days: float) -> Path:
    """Create a JSON file whose mtime is age_days old."""
    p = directory / f"{name}.json"
    p.write_text('{"rows": []}', encoding="utf-8")
    mtime = time.time() - age_days * 86400
    os.utime(p, (mtime, mtime))
    return p


def _make_upload(directory: Path, name: str, age_hours: float) -> Path:
    """Create an upload file whose mtime is age_hours old."""
    p = directory / name
    p.write_bytes(b"fake-xlsx-content")
    mtime = time.time() - age_hours * 3600
    os.utime(p, (mtime, mtime))
    return p


@pytest.fixture()
def rows_dir(tmp_path: Path) -> Path:
    d = tmp_path / "extracted_rows"
    d.mkdir()
    return d


@pytest.fixture()
def upload_dir(tmp_path: Path) -> Path:
    d = tmp_path / "batch_uploads"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Phase 1 — JSON expiry
# ---------------------------------------------------------------------------

class TestJsonExpiry:

    def test_old_file_deleted(self, rows_dir):
        from spir_dynamic.tasks.cleanup_tasks import _purge_old_json_files

        old = _make_json(rows_dir, "old-file", age_days=20)
        result = _purge_old_json_files(rows_dir, retention_days=14, dry_run=False)

        assert not old.exists(), "Old JSON should have been deleted"
        assert result["deleted"] == 1
        assert result["mb_freed"] >= 0

    def test_recent_file_preserved(self, rows_dir):
        from spir_dynamic.tasks.cleanup_tasks import _purge_old_json_files

        recent = _make_json(rows_dir, "recent-file", age_days=3)
        result = _purge_old_json_files(rows_dir, retention_days=14, dry_run=False)

        assert recent.exists(), "Recent JSON must not be deleted"
        assert result["deleted"] == 0

    def test_safety_guard_preserves_fresh_file(self, rows_dir):
        from spir_dynamic.tasks.cleanup_tasks import _purge_old_json_files

        # File is technically old (20 days) but mtime was just touched (within 1h)
        p = rows_dir / "fresh.json"
        p.write_text('{"rows": []}')
        # mtime is NOW — safety guard kicks in
        result = _purge_old_json_files(rows_dir, retention_days=14, dry_run=False)

        assert p.exists(), "File touched within 1h must never be deleted"
        assert result["skipped_recent"] >= 1

    def test_non_json_files_untouched(self, rows_dir):
        from spir_dynamic.tasks.cleanup_tasks import _purge_old_json_files

        txt = rows_dir / "notes.txt"
        txt.write_text("not a json file")
        mtime = time.time() - 30 * 86400
        os.utime(txt, (mtime, mtime))

        result = _purge_old_json_files(rows_dir, retention_days=14, dry_run=False)

        assert txt.exists(), "Non-JSON files must never be touched"
        assert result["deleted"] == 0

    def test_dry_run_deletes_nothing(self, rows_dir):
        from spir_dynamic.tasks.cleanup_tasks import _purge_old_json_files

        old = _make_json(rows_dir, "old-dry", age_days=30)
        result = _purge_old_json_files(rows_dir, retention_days=14, dry_run=True)

        assert old.exists(), "Dry-run must not delete files"
        assert result["deleted"] == 0

    def test_missing_dir_handled_gracefully(self, tmp_path):
        from spir_dynamic.tasks.cleanup_tasks import _purge_old_json_files

        nonexistent = tmp_path / "does_not_exist"
        result = _purge_old_json_files(nonexistent, retention_days=14, dry_run=False)

        assert result.get("skipped") is True


# ---------------------------------------------------------------------------
# Phase 2 — Orphan JSON cleanup
# ---------------------------------------------------------------------------

class TestOrphanCleanup:

    def test_orphan_file_deleted(self, rows_dir):
        from spir_dynamic.tasks.cleanup_tasks import _purge_orphan_json_files

        orphan_id = "aaaaaaaa-0000-0000-0000-000000000001"
        orphan = _make_json(rows_dir, orphan_id, age_days=2)

        # DB knows about a different file_id — the one on disk is not in DB
        with patch(
            "spir_dynamic.tasks.cleanup_tasks._fetch_known_file_ids",
            return_value={"bbbbbbbb-0000-0000-0000-000000000002"},
        ):
            result = _purge_orphan_json_files(rows_dir, database_url="postgresql+asyncpg://x", dry_run=False)

        assert not orphan.exists(), "Orphan should have been deleted"
        assert result["deleted"] == 1

    def test_known_file_preserved(self, rows_dir):
        from spir_dynamic.tasks.cleanup_tasks import _purge_orphan_json_files

        known_id = "cccccccc-0000-0000-0000-000000000003"
        known = _make_json(rows_dir, known_id, age_days=2)

        with patch(
            "spir_dynamic.tasks.cleanup_tasks._fetch_known_file_ids",
            return_value={known_id},
        ):
            result = _purge_orphan_json_files(rows_dir, database_url="postgresql+asyncpg://x", dry_run=False)

        assert known.exists(), "File with DB record must be preserved"
        assert result["deleted"] == 0

    def test_skipped_without_database_url(self, rows_dir):
        from spir_dynamic.tasks.cleanup_tasks import _purge_orphan_json_files

        result = _purge_orphan_json_files(rows_dir, database_url="", dry_run=False)

        assert result.get("skipped") is True
        assert result.get("reason") == "no_database_url"

    def test_db_failure_produces_skip(self, rows_dir):
        from spir_dynamic.tasks.cleanup_tasks import _purge_orphan_json_files

        with patch(
            "spir_dynamic.tasks.cleanup_tasks._fetch_known_file_ids",
            return_value=None,   # simulates DB query failure
        ):
            result = _purge_orphan_json_files(rows_dir, database_url="postgresql+asyncpg://x", dry_run=False)

        assert result.get("skipped") is True

    def test_dry_run_preserves_orphan(self, rows_dir):
        from spir_dynamic.tasks.cleanup_tasks import _purge_orphan_json_files

        orphan_id = "dddddddd-0000-0000-0000-000000000004"
        orphan = _make_json(rows_dir, orphan_id, age_days=2)

        with patch(
            "spir_dynamic.tasks.cleanup_tasks._fetch_known_file_ids",
            return_value=set(),
        ):
            result = _purge_orphan_json_files(rows_dir, database_url="postgresql+asyncpg://x", dry_run=True)

        assert orphan.exists(), "Dry-run must not delete orphans"
        assert result["deleted"] == 0


# ---------------------------------------------------------------------------
# Phase 3 — Stale uploads
# ---------------------------------------------------------------------------

class TestStaleUploads:

    def test_old_upload_deleted(self, upload_dir):
        from spir_dynamic.tasks.cleanup_tasks import _purge_stale_uploads

        old = _make_upload(upload_dir, "job123_file.xlsx", age_hours=30)
        result = _purge_stale_uploads(upload_dir, stale_hours=24, dry_run=False)

        assert not old.exists(), "Stale upload should have been deleted"
        assert result["deleted"] == 1

    def test_recent_upload_preserved(self, upload_dir):
        from spir_dynamic.tasks.cleanup_tasks import _purge_stale_uploads

        recent = _make_upload(upload_dir, "in_progress.xlsx", age_hours=2)
        result = _purge_stale_uploads(upload_dir, stale_hours=24, dry_run=False)

        assert recent.exists(), "Recent upload must not be deleted"
        assert result["deleted"] == 0

    def test_hidden_files_skipped(self, upload_dir):
        from spir_dynamic.tasks.cleanup_tasks import _purge_stale_uploads

        hidden = _make_upload(upload_dir, ".gitkeep", age_hours=999)
        result = _purge_stale_uploads(upload_dir, stale_hours=24, dry_run=False)

        assert hidden.exists(), "Hidden files must never be deleted"

    def test_dry_run_preserves_stale(self, upload_dir):
        from spir_dynamic.tasks.cleanup_tasks import _purge_stale_uploads

        old = _make_upload(upload_dir, "stale_dry.xlsx", age_hours=48)
        result = _purge_stale_uploads(upload_dir, stale_hours=24, dry_run=True)

        assert old.exists(), "Dry-run must not delete stale uploads"
        assert result["deleted"] == 0


# ---------------------------------------------------------------------------
# Phase 4 — Disk metrics
# ---------------------------------------------------------------------------

class TestDiskMetrics:

    def test_metrics_reflect_actual_counts(self, rows_dir, upload_dir):
        from spir_dynamic.tasks.cleanup_tasks import _update_disk_metrics

        (rows_dir / "a.json").write_text('{"rows":[]}')
        (rows_dir / "b.json").write_text('{"rows":[]}')
        (upload_dir / "upload1.xlsx").write_bytes(b"x" * 1024)

        result = _update_disk_metrics(rows_dir, upload_dir)

        assert result["json_count"] == 2
        assert result["json_size_mb"] >= 0
        assert result["upload_size_mb"] >= 0

    def test_metrics_with_empty_dirs(self, rows_dir, upload_dir):
        from spir_dynamic.tasks.cleanup_tasks import _update_disk_metrics

        result = _update_disk_metrics(rows_dir, upload_dir)

        assert result["json_count"] == 0
        assert result["json_size_mb"] == 0.0
        assert result["upload_size_mb"] == 0.0

    def test_metrics_with_missing_dirs(self, tmp_path):
        from spir_dynamic.tasks.cleanup_tasks import _update_disk_metrics

        result = _update_disk_metrics(
            tmp_path / "nonexistent_rows",
            tmp_path / "nonexistent_uploads",
        )
        assert result["json_count"] == 0


# ---------------------------------------------------------------------------
# Integration — full task
# ---------------------------------------------------------------------------

class TestLifecycleCleanupTask:

    def test_dry_run_deletes_nothing(self, tmp_path, monkeypatch):
        from spir_dynamic.tasks.cleanup_tasks import lifecycle_cleanup_task

        rows_dir   = tmp_path / "extracted_rows"
        upload_dir = tmp_path / "batch_uploads"
        rows_dir.mkdir()
        upload_dir.mkdir()

        old_json   = _make_json(rows_dir, "old-integ", age_days=20)
        old_upload = _make_upload(upload_dir, "stale.xlsx", age_hours=48)

        monkeypatch.setenv("SANITIZER_ENABLED", "false")  # avoid noise
        from spir_dynamic.app.config import get_settings
        get_settings.cache_clear()

        with patch("spir_dynamic.app.config.get_settings") as mock_cfg:
            cfg = get_settings()
            # Override storage paths to point at tmp_path
            cfg_obj = type("Cfg", (), {
                "rows_storage_path": str(rows_dir),
                "batch_upload_dir":  str(upload_dir),
                "cleanup_json_retention_days": 14,
                "cleanup_upload_stale_hours":  24,
                "cleanup_dry_run": True,   # force dry-run at config level
                "database_url": "",
            })()
            mock_cfg.return_value = cfg_obj

            summary = lifecycle_cleanup_task.run(dry_run=True)

        assert old_json.exists(),   "Dry-run must not delete JSON"
        assert old_upload.exists(), "Dry-run must not delete uploads"
        assert summary["dry_run"] is True

        get_settings.cache_clear()

    def test_live_run_deletes_expired_json(self, tmp_path):
        from spir_dynamic.tasks.cleanup_tasks import lifecycle_cleanup_task

        rows_dir   = tmp_path / "extracted_rows"
        upload_dir = tmp_path / "batch_uploads"
        rows_dir.mkdir()
        upload_dir.mkdir()

        old    = _make_json(rows_dir, "expired-integ", age_days=20)
        recent = _make_json(rows_dir, "current-integ", age_days=3)

        with patch("spir_dynamic.app.config.get_settings") as mock_cfg:
            cfg_obj = type("Cfg", (), {
                "rows_storage_path": str(rows_dir),
                "batch_upload_dir":  str(upload_dir),
                "cleanup_json_retention_days": 14,
                "cleanup_upload_stale_hours":  24,
                "cleanup_dry_run": False,
                "database_url": "",
            })()
            mock_cfg.return_value = cfg_obj

            summary = lifecycle_cleanup_task.run(dry_run=False)

        assert not old.exists(),  "Expired JSON must be deleted"
        assert recent.exists(),   "Recent JSON must be preserved"
        phase = summary["phases"]["json_expiry"]
        assert phase["deleted"] == 1
