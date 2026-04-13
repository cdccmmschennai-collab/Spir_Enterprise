"""
Database initialization:
  1. Create all tables (idempotent — does nothing if they exist).
  2. Seed the initial admin user from APP_USER / APP_PASS env vars if no admin exists.

Called once from the FastAPI lifespan event.
"""
from __future__ import annotations

import logging

from sqlalchemy import select, text

from spir_dynamic.db.database import Base, get_engine, get_session_factory
from spir_dynamic.db.models import User

log = logging.getLogger(__name__)


async def create_tables() -> None:
    """Create all tables if they don't exist (non-destructive)."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("Database tables verified/created")


async def seed_admin(username: str, plain_password: str) -> None:
    """
    Ensure at least one admin user exists.
    If the username already exists but the password changed, update the hash.
    """
    import bcrypt as _bcrypt

    def _hash(plain: str) -> str:
        return _bcrypt.hashpw(plain.encode("utf-8"), _bcrypt.gensalt(rounds=12)).decode("utf-8")

    def _verify(plain: str, hashed: str) -> bool:
        try:
            return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
        except Exception:
            return False

    factory = get_session_factory()

    async with factory() as session:
        existing: User | None = await session.scalar(
            select(User).where(User.username == username)
        )
        if existing:
            existing_hash: str = existing.password_hash
            if not _verify(plain_password, existing_hash):
                # APP_PASS changed — update hash so login works after redeploy
                existing.password_hash = _hash(plain_password)
                await session.commit()
                log.info("Admin user '%s' password hash updated", username)
            else:
                log.info("Admin user '%s' already exists — no change", username)
            return

        admin = User(
            username=username,
            password_hash=_hash(plain_password),
            role="admin",
            is_active=True,
        )
        session.add(admin)
        await session.commit()
        log.info("Admin user '%s' seeded into database", username)


async def initialize(database_url: str, app_user: str, app_pass: str) -> bool:
    """
    Full initialization sequence. Returns True on success, False on failure.
    Failure is non-fatal — system falls back to static-credential mode.
    """
    from spir_dynamic.db.database import setup_engine

    try:
        setup_engine(database_url)
        await create_tables()
        await seed_admin(app_user, app_pass)
        return True
    except Exception as exc:
        log.error(
            "Database initialization failed — running in no-DB mode: %s", exc
        )
        return False
