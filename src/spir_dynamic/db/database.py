"""
Async SQLAlchemy engine and session factory.

Set DATABASE_URL in .env to enable full DB mode:
  DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname

If DATABASE_URL is not set, the DB layer is disabled and the system
falls back to static env-var credentials with no audit logging.
"""
from __future__ import annotations

import logging
from typing import AsyncGenerator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

log = logging.getLogger(__name__)

# ── Global singletons (set in init_db.setup_engine) ──────────────────────────

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


class Base(DeclarativeBase):
    pass


def is_db_enabled() -> bool:
    return _engine is not None


def setup_engine(database_url: str) -> None:
    """Create the async engine and session factory from a DATABASE_URL string."""
    global _engine, _session_factory

    # Convert sync postgres:// → postgresql+asyncpg://
    url = database_url
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    _engine = create_async_engine(
        url,
        echo=False,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
    )
    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    log.info("Database engine created")


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database engine not initialized")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Session factory not initialized")
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an async session and commits on success."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
