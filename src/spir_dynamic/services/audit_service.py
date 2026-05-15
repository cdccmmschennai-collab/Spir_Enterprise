"""
Fire-and-forget audit logging service.

All functions are safe to call even when the DB is not configured —
they silently skip when is_db_enabled() is False.

Usage from route handlers (via BackgroundTasks):
    background_tasks.add_task(
        log_extraction, user_id, session_id, result, ip_address
    )
"""
from __future__ import annotations

import asyncio
import logging
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any, Optional

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _write_activity(
    user_id: str,
    session_id: Optional[str],
    action: str,
    details: Optional[dict],
    ip_address: Optional[str],
) -> None:
    """Internal: write one UserActivityLog row. Never raises."""
    from spir_dynamic.db.database import is_db_enabled, get_session_factory
    from spir_dynamic.db.models import UserActivityLog, Session
    from sqlalchemy import select

    if not is_db_enabled():
        return
    try:
        factory = get_session_factory()
        async with factory() as db:
            # session_id from JWT is the jti; FK points to sessions.id (PK).
            # Resolve jti → sessions.id; fall back to None to avoid FK violation.
            session_pk: Optional[str] = None
            if session_id:
                session_pk = await db.scalar(
                    select(Session.id).where(Session.jti == session_id)
                )
            entry = UserActivityLog(
                user_id=user_id,
                session_id=session_pk,
                action=action,
                details=details,
                ip_address=ip_address,
            )
            db.add(entry)
            await db.commit()
    except Exception as exc:
        log.warning("Audit log write failed [%s]: %s", action, exc)


def schedule(coro) -> None:
    """Schedule an async coroutine as a fire-and-forget task."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(coro)
        else:
            loop.run_until_complete(coro)
    except Exception as exc:
        log.warning("Audit schedule error: %s", exc)


# ── Public helpers ─────────────────────────────────────────────────────────────

async def log_login(
    user_id: str,
    session_id: str,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    await _write_activity(
        user_id=user_id,
        session_id=session_id,
        action="login",
        details={"user_agent": user_agent},
        ip_address=ip_address,
    )


async def log_logout(
    user_id: str,
    session_id: str,
    ip_address: Optional[str] = None,
) -> None:
    await _write_activity(
        user_id=user_id,
        session_id=session_id,
        action="logout",
        details=None,
        ip_address=ip_address,
    )


async def log_extraction(
    user_id: str,
    session_id: Optional[str],
    result: dict,
    ip_address: Optional[str] = None,
    original_filename: Optional[str] = None,
    json_path: Optional[str] = None,
) -> None:
    """Store minimal extraction summary in extraction_history + activity log. Never raises."""
    from spir_dynamic.db.database import is_db_enabled, get_session_factory
    from spir_dynamic.db.models import ExtractionHistory, Session, UserActivityLog
    from sqlalchemy import select

    if not is_db_enabled():
        return

    if not user_id:
        log.error(
            "log_extraction: user_id is missing — extraction_history NOT written. "
            "file=%s This indicates user_id was not passed at enqueue time.",
            result.get("filename", "unknown"),
        )
        return

    output_filename = result.get("filename")
    spir_no = result.get("spir_no") or None
    fmt = result.get("format")
    equipment = result.get("equipment")
    manufacturer = result.get("manufacturer")
    supplier = result.get("supplier")
    file_id = result.get("file_id")

    # Do not silently invent defaults here; if pipeline didn't provide counts,
    # we fail the history write (extraction still succeeds).
    total_rows = int(result["total_rows"])
    total_tags = int(result["total_tags"])
    spare_items = int(result["spare_items"])
    annexure_count = int(result.get("annexure_count", 0) or 0)
    dup_count = int(result.get("dup1_count", 0) or 0)
    sap_count = int(result.get("sap_count", 0) or 0)

    if not output_filename:
        raise ValueError("log_extraction: result['filename'] missing/empty")
    if not original_filename:
        raise ValueError("log_extraction: original_filename missing/empty")

    print("Saving history:", output_filename, total_tags, spare_items)

    try:
        factory = get_session_factory()
        async with factory() as db:
            # Fix session FK issue: routes pass JWT jti, but FK points to sessions.id.
            # Map jti -> sessions.id when possible; otherwise write NULL.
            session_pk: Optional[str] = None
            if session_id:
                session_pk = await db.scalar(select(Session.id).where(Session.jti == session_id))

            history = ExtractionHistory(
                id=str(uuid4()),
                user_id=user_id,
                session_id=session_pk,
                filename=output_filename,
                output_filename=output_filename,
                original_filename=original_filename,
                spir_no=spir_no,
                format=fmt,
                total_rows=total_rows,
                total_tags=total_tags,
                spare_items=spare_items,
                annexure_count=annexure_count,
                dup_count=dup_count,
                sap_count=sap_count,
                equipment=equipment,
                manufacturer=manufacturer,
                supplier=supplier,
                file_id=file_id,
                json_path=json_path,
                # Keep the existing user-facing history API working
                tag_count=total_tags,
                spare_count=spare_items,
                created_at=datetime.now(ZoneInfo("Asia/Kolkata")),
            )
            db.add(history)
            db.add(
                UserActivityLog(
                    user_id=user_id,
                    session_id=session_pk,
                    action="extract",
                    details={
                        "filename": output_filename,
                        "original_filename": original_filename,
                        "spir_no": spir_no,
                        "tag_count": total_tags,
                        "spare_count": spare_items,
                    },
                    ip_address=ip_address,
                )
            )

            try:
                await db.commit()
            except Exception as e:
                await db.rollback()
                print("History save failed:", e)
    except Exception as exc:
        # Keep extraction pipeline unaffected.
        log.error("Extraction history write failed: %s", exc, exc_info=True)


def log_extraction_sync(
    user_id: str,
    session_id: Optional[str],
    result: dict,
    ip_address: Optional[str] = None,
    original_filename: Optional[str] = None,
    json_path: Optional[str] = None,
) -> None:
    """
    Synchronous wrapper around log_extraction — safe to call from Celery tasks.

    Creates a fresh, isolated event loop for each call so it cannot conflict
    with any existing event loop in the calling thread (gevent, eventlet, etc.).
    The loop is always closed in the finally block to prevent file-descriptor leaks.
    """
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            log_extraction(
                user_id=user_id,
                session_id=session_id,
                result=result,
                ip_address=ip_address,
                original_filename=original_filename,
                json_path=json_path,
            )
        )
    finally:
        loop.close()


def log_extraction_worker(
    user_id: str,
    result: dict,
    original_filename: str,
    json_path: Optional[str] = None,
) -> None:
    """
    Synchronous history writer for Celery workers.

    Uses a psycopg2-backed SQLAlchemy sync engine with NullPool — no asyncio,
    no event loops, no shared connection state between worker processes.
    Each call: connect → insert → commit → close.

    The async log_extraction() used by FastAPI routes is NOT touched.
    """
    from spir_dynamic.app.config import get_settings
    from spir_dynamic.db.models import ExtractionHistory, UserActivityLog
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from uuid import uuid4 as _uuid4

    cfg = get_settings()
    if not cfg.database_url:
        log.debug("log_extraction_worker: DATABASE_URL not set — skipping history write")
        return

    if not user_id:
        log.error(
            "log_extraction_worker: user_id missing — extraction_history NOT written. file=%s",
            result.get("filename", "unknown"),
        )
        return

    if not original_filename:
        log.error("log_extraction_worker: original_filename missing — history NOT written")
        return

    output_filename = result.get("filename") or ""
    if not output_filename:
        log.error("log_extraction_worker: result['filename'] missing — history NOT written")
        return

    # Convert asyncpg URL → psycopg2 URL for synchronous access.
    # database.py stores the URL as postgresql+asyncpg://... — strip that prefix.
    sync_url = cfg.database_url
    sync_url = sync_url.replace("+asyncpg", "+psycopg2")
    if sync_url.startswith("postgres://"):
        sync_url = "postgresql+psycopg2://" + sync_url[len("postgres://"):]

    # NullPool: no connection pooling — open, write, close.
    # Safe for multiple worker processes that each call this independently.
    engine = create_engine(sync_url, poolclass=NullPool, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        total_rows = int(result.get("total_rows", 0) or 0)
        total_tags = int(result.get("total_tags", 0) or 0)
        spare_items = int(result.get("spare_items", 0) or 0)
        now = datetime.now(ZoneInfo("Asia/Kolkata"))

        history = ExtractionHistory(
            id=str(_uuid4()),
            user_id=user_id,
            session_id=None,
            filename=output_filename,
            output_filename=output_filename,
            original_filename=original_filename,
            spir_no=result.get("spir_no") or None,
            format=result.get("format"),
            total_rows=total_rows,
            total_tags=total_tags,
            spare_items=spare_items,
            annexure_count=int(result.get("annexure_count", 0) or 0),
            dup_count=int(result.get("dup1_count", 0) or 0),
            sap_count=int(result.get("sap_count", 0) or 0),
            equipment=result.get("equipment"),
            manufacturer=result.get("manufacturer"),
            supplier=result.get("supplier"),
            file_id=result.get("file_id"),
            json_path=json_path,
            tag_count=total_tags,
            spare_count=spare_items,
            created_at=now,
        )
        session.add(history)

        session.add(UserActivityLog(
            user_id=user_id,
            session_id=None,
            action="extract",
            details={
                "filename": output_filename,
                "original_filename": original_filename,
                "spir_no": result.get("spir_no"),
                "tag_count": total_tags,
                "spare_count": spare_items,
            },
            ip_address=None,
        ))

        session.commit()
        log.info(
            "Extraction history written (worker) | user=%s file=%s rows=%d tags=%d",
            user_id, original_filename, total_rows, total_tags,
        )

    except Exception as exc:
        session.rollback()
        log.error(
            "Extraction history write failed (worker) | user=%s file=%s: %s",
            user_id, original_filename, exc, exc_info=True,
        )
        raise

    finally:
        session.close()
        engine.dispose()


async def log_download(
    user_id: str,
    session_id: Optional[str],
    file_id: str,
    ip_address: Optional[str] = None,
) -> None:
    await _write_activity(
        user_id=user_id,
        session_id=session_id,
        action="download",
        details={"file_id": file_id},
        ip_address=ip_address,
    )


async def update_session_activity(session_id: str) -> None:
    """Bump last_activity_at for a session. Called on every authenticated request."""
    from spir_dynamic.db.database import is_db_enabled, get_session_factory
    from spir_dynamic.db.models import Session
    from sqlalchemy import select, update

    if not is_db_enabled():
        return
    try:
        factory = get_session_factory()
        async with factory() as db:
            await db.execute(
                update(Session)
                .where(Session.jti == session_id)
                .values(last_activity_at=_now())
            )
            await db.commit()
    except Exception as exc:
        log.debug("Session activity update failed: %s", exc)


async def end_session(jti: str) -> None:
    """Mark a session as ended (logout)."""
    from spir_dynamic.db.database import is_db_enabled, get_session_factory
    from spir_dynamic.db.models import Session
    from sqlalchemy import update

    if not is_db_enabled():
        return
    try:
        factory = get_session_factory()
        async with factory() as db:
            await db.execute(
                update(Session)
                .where(Session.jti == jti)
                .values(is_active=False, ended_at=_now())
            )
            await db.commit()
    except Exception as exc:
        log.warning("Session end failed: %s", exc)
