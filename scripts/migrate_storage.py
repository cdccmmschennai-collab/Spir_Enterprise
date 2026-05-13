"""
One-time storage migration: standardise extracted_rows location.

WHAT IT DOES
------------
1. Copies *.json files from src/storage/extracted_rows/ → storage/extracted_rows/
   (the correct project-root location going forward).
2. Updates ExtractionHistory.json_path records in PostgreSQL that contain
   relative or stale paths, replacing each with the new absolute path.

WHAT IT DOES NOT DO
-------------------
- Does NOT delete the originals in src/storage/extracted_rows/
  (kept for rollback safety — remove manually after verification).
- Does NOT touch any extraction logic, pipeline, or Celery code.

IDEMPOTENT: safe to re-run. Files already in destination are skipped.
DB records that already have a correct absolute path are skipped.

USAGE
-----
    cd /opt/spir_dynamic          # or wherever the project root is
    python scripts/migrate_storage.py

Reads DATABASE_URL from .env automatically.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

# scripts/migrate_storage.py → project root is one level up
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

OLD_DIR = PROJECT_ROOT / "src" / "storage" / "extracted_rows"
NEW_DIR = PROJECT_ROOT / "storage" / "extracted_rows"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _banner(msg: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {msg}")
    print('=' * 60)


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _skip(msg: str) -> None:
    print(f"  [SKIP] {msg}")


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}", file=sys.stderr)


def _err(msg: str) -> None:
    print(f"  [ERR]  {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Step 1: File migration
# ---------------------------------------------------------------------------

def migrate_files() -> dict[str, Path]:
    """
    Copy *.json from OLD_DIR to NEW_DIR.

    Returns a mapping {uuid_stem: new_absolute_path} for every file that
    now exists in NEW_DIR (whether just copied or already present).
    """
    _banner("STEP 1 — Copy JSON row files")
    print(f"  Source : {OLD_DIR}")
    print(f"  Target : {NEW_DIR}")

    NEW_DIR.mkdir(parents=True, exist_ok=True)

    result: dict[str, Path] = {}
    copied = skipped = errors = 0

    if not OLD_DIR.exists():
        _warn(f"Source directory not found: {OLD_DIR}")
        _warn("Nothing to copy — checking existing NEW_DIR files instead.")
    else:
        for src_file in sorted(OLD_DIR.glob("*.json")):
            dest_file = NEW_DIR / src_file.name
            stem = src_file.stem  # UUID without .json

            if dest_file.exists() and dest_file.stat().st_size == src_file.stat().st_size:
                _skip(f"{src_file.name} (already in destination, same size)")
                result[stem] = dest_file
                skipped += 1
                continue

            try:
                shutil.copy2(src_file, dest_file)
                # Verify readability after copy
                json.loads(dest_file.read_text(encoding="utf-8"))
                _ok(f"Copied {src_file.name} ({src_file.stat().st_size:,} bytes)")
                result[stem] = dest_file
                copied += 1
            except Exception as exc:
                _err(f"Failed to copy {src_file.name}: {exc}")
                errors += 1

    # Also collect any *.json already in NEW_DIR that weren't in OLD_DIR
    for f in NEW_DIR.glob("*.json"):
        if f.stem not in result:
            result[f.stem] = f

    print(f"\n  Copied: {copied}  |  Skipped (already present): {skipped}  |  Errors: {errors}")
    print(f"  Files now in destination: {len(result)}")

    if errors:
        _warn("Some files failed to copy — check errors above before proceeding.")

    return result


# ---------------------------------------------------------------------------
# Step 2: Database migration
# ---------------------------------------------------------------------------

async def migrate_db(uuid_to_new_path: dict[str, Path]) -> None:
    """
    For each ExtractionHistory row whose json_path is relative or points to
    OLD_DIR, update it to the new absolute path.

    Skips rows where:
      - json_path IS NULL
      - json_path is already absolute AND the file exists
    """
    _banner("STEP 2 — Update ExtractionHistory.json_path in PostgreSQL")

    # Load .env so DATABASE_URL is available
    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:
        pass  # python-dotenv not installed; rely on env vars being set externally

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        _warn("DATABASE_URL not set — skipping DB update.")
        _warn("Re-run after setting DATABASE_URL if you need to fix combine for old records.")
        return

    try:
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy import text
    except ImportError:
        _warn("SQLAlchemy not installed in this Python env — skipping DB update.")
        return

    engine = create_async_engine(db_url, echo=False)

    updated = skipped = errors = not_found = 0

    try:
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    "SELECT id, json_path, filename "
                    "FROM extraction_history "
                    "WHERE json_path IS NOT NULL"
                )
            )
            rows = result.fetchall()

        print(f"  Found {len(rows)} ExtractionHistory records with json_path set.")

        async with engine.begin() as conn:
            for row_id, json_path, filename in rows:
                old_p = Path(json_path)
                stem = old_p.stem  # UUID

                # Case 1: already absolute and the file exists → nothing to do
                if old_p.is_absolute() and old_p.exists():
                    _skip(f"id={row_id} ({filename}) — already correct: {json_path}")
                    skipped += 1
                    continue

                # Case 2: we have the file in the new location
                if stem in uuid_to_new_path:
                    new_abs = str(uuid_to_new_path[stem])
                    await conn.execute(
                        text(
                            "UPDATE extraction_history "
                            "SET json_path = :new_path "
                            "WHERE id = :id"
                        ),
                        {"new_path": new_abs, "id": row_id},
                    )
                    _ok(
                        f"id={row_id} ({filename})\n"
                        f"         old: {json_path}\n"
                        f"         new: {new_abs}"
                    )
                    updated += 1

                else:
                    # The UUID doesn't match any file we have — record will still
                    # fail combine, but we can't fix what we don't have.
                    _warn(
                        f"id={row_id} ({filename}) — no JSON file found for UUID={stem} "
                        f"(path was: {json_path})"
                    )
                    not_found += 1

    except Exception as exc:
        _err(f"Database operation failed: {exc}")
        errors += 1
    finally:
        await engine.dispose()

    print(
        f"\n  Updated: {updated}  |  Skipped (already correct): {skipped}  "
        f"|  No file found: {not_found}  |  Errors: {errors}"
    )


# ---------------------------------------------------------------------------
# Step 3: Verification
# ---------------------------------------------------------------------------

def verify(uuid_to_new_path: dict[str, Path]) -> bool:
    """Quick readability check on every file in NEW_DIR."""
    _banner("STEP 3 — Verify migrated files are readable")

    passed = failed = 0
    for stem, path in sorted(uuid_to_new_path.items()):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            rows = data.get("rows", [])
            _ok(f"{path.name} — {len(rows)} rows")
            passed += 1
        except Exception as exc:
            _err(f"{path.name} — unreadable: {exc}")
            failed += 1

    print(f"\n  Passed: {passed}  |  Failed: {failed}")

    if not_removable := OLD_DIR if OLD_DIR.exists() else None:
        print(
            f"\n  NOTE: Original files in {not_removable} were NOT deleted."
        )
        print("        After verifying combine works, you may remove them manually:")
        print(f"        rm -rf {not_removable}  (Linux/macOS)")
        print(f"        Remove-Item -Recurse '{not_removable}'  (PowerShell)")

    return failed == 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _main() -> None:
    print(f"\nSPIR Dynamic — Storage Migration")
    print(f"Project root : {PROJECT_ROOT}")
    print(f"Old location : {OLD_DIR}")
    print(f"New location : {NEW_DIR}")

    uuid_map = migrate_files()
    await migrate_db(uuid_map)
    ok = verify(uuid_map)

    _banner("DONE")
    if ok:
        print("  All files verified. Storage migration complete.")
        print("  Next steps:")
        print("    1. Restart the FastAPI app")
        print("    2. Check logs for: 'Row storage ready: <absolute-path>'")
        print("    3. Test /api/combine with existing history IDs")
        print("    4. Extract a new file and confirm its json_path is absolute")
        print("    5. Restart again and confirm combine still works")
        print(f"    6. Manually delete {OLD_DIR} once satisfied")
    else:
        print("  WARNING: Some files failed verification — check errors above.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())
