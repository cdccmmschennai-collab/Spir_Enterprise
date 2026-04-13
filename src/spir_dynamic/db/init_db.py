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
    If the username already exists, skip silently.
    """
    from passlib.context import CryptContext

    pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
    factory = get_session_factory()

    async with factory() as session:
        existing = await session.scalar(
            select(User).where(User.username == username)
        )
        if existing:
            log.info("Admin user '%s' already exists — skipping seed", username)
            return

        admin = User(
            username=username,
            password_hash=pwd_ctx.hash(plain_password),
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
