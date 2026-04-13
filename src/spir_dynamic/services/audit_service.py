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
from datetime import datetime, timezone
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
) -> None:
    """Store extraction result summary in extraction_history + activity log."""
    from spir_dynamic.db.database import is_db_enabled, get_session_factory
    from spir_dynamic.db.models import ExtractionHistory

    if not is_db_enabled():
        return

    details = {
        "filename": result.get("filename"),
        "spir_no": result.get("spir_no"),
        "total_rows": result.get("total_rows"),
        "total_tags": result.get("total_tags"),
    }

    try:
        factory = get_session_factory()
        async with factory() as db:
            history = ExtractionHistory(
                user_id=user_id,
                session_id=session_id,
                original_filename=result.get("filename", ""),
                spir_no=result.get("spir_no"),
                format=result.get("format"),
                total_rows=result.get("total_rows", 0),
                total_tags=result.get("total_tags", 0),
                spare_items=result.get("spare_items", 0),
                annexure_count=result.get("annexure_count", 0),
                dup_count=result.get("dup1_count", 0),
                sap_count=result.get("sap_count", 0),
                equipment=result.get("equipment"),
                manufacturer=result.get("manufacturer"),
                supplier=result.get("supplier"),
                file_id=result.get("file_id"),
                output_filename=result.get("filename"),
            )
            db.add(history)

            activity = __import__(
                "spir_dynamic.db.models", fromlist=["UserActivityLog"]
            ).UserActivityLog(
                user_id=user_id,
                session_id=session_id,
                action="extract",
                details=details,
                ip_address=ip_address,
            )
            db.add(activity)
            await db.commit()
    except Exception as exc:
        log.warning("Extraction history write failed: %s", exc)


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
