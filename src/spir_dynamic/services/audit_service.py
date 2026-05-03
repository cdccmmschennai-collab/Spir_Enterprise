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
    from spir_dynamic.db.models import UserActivityLog

    if not is_db_enabled():
        return
    try:
        factory = get_session_factory()
        async with factory() as db:
            entry = UserActivityLog(
                user_id=user_id,
                session_id=session_id,
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
) -> None:
    """Store minimal extraction summary in extraction_history + activity log. Never raises."""
    from spir_dynamic.db.database import is_db_enabled, get_session_factory
    from spir_dynamic.db.models import ExtractionHistory, Session, UserActivityLog
    from sqlalchemy import select

    if not is_db_enabled():
        return

    if not user_id:
        log.warning("log_extraction: user_id is None — history skipped (re-login required)")
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
