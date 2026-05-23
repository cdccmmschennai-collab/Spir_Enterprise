"""
Database initialization:
  1. Create all tables (idempotent — does nothing if they exist).
  2. Seed the initial admin user from APP_USER / APP_PASS env vars if no admin exists.

Called once from the FastAPI lifespan event.
"""
from __future__ import annotations

import structlog
from sqlalchemy import select, text

from spir_dynamic.db.database import Base, get_engine, get_session_factory
from spir_dynamic.db.models import User, Job, PasswordResetRequest, Branch  # noqa: F401 — all models must be imported for create_all

log = structlog.stdlib.get_logger(__name__)


async def create_tables() -> None:
    """Create all tables if they don't exist (non-destructive)."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("Database tables verified/created")


async def ensure_schema() -> None:
    """Idempotently add any columns missing from extraction_history."""
    engine = get_engine()
    ddl = [
        # Columns (idempotent)
        "ALTER TABLE extraction_history ADD COLUMN IF NOT EXISTS filename    TEXT         NOT NULL DEFAULT ''",
        "ALTER TABLE extraction_history ADD COLUMN IF NOT EXISTS original_filename TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE extraction_history ADD COLUMN IF NOT EXISTS spir_no     TEXT",
        "ALTER TABLE extraction_history ADD COLUMN IF NOT EXISTS total_rows  INTEGER      NOT NULL DEFAULT 0",
        "ALTER TABLE extraction_history ADD COLUMN IF NOT EXISTS tag_count   INTEGER      NOT NULL DEFAULT 0",
        "ALTER TABLE extraction_history ADD COLUMN IF NOT EXISTS spare_count INTEGER      NOT NULL DEFAULT 0",
        "ALTER TABLE extraction_history ADD COLUMN IF NOT EXISTS created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()",
        # format can exceed typical VARCHAR limits for multi-sheet workbooks; store as TEXT.
        "ALTER TABLE extraction_history ALTER COLUMN format TYPE TEXT",
        # json_path stores the disk path of the extracted rows JSON file (for combine feature).
        "ALTER TABLE extraction_history ADD COLUMN IF NOT EXISTS json_path VARCHAR(512)",
        "CREATE INDEX IF NOT EXISTS ix_extraction_history_user_id    ON extraction_history(user_id)",
        "CREATE INDEX IF NOT EXISTS ix_extraction_history_created_at ON extraction_history(created_at)",
    ]
    async with engine.begin() as conn:
        for stmt in ddl:
            try:
                await conn.execute(text(stmt))
            except Exception as exc:
                log.warning("db.schema_sync_skipped", stmt=stmt[:60], exc_message=str(exc))
    log.info("extraction_history schema verified/synced")


async def ensure_user_schema() -> None:
    """Idempotently add any columns missing from users table."""
    engine = get_engine()
    ddl = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url VARCHAR(500)",
    ]
    async with engine.begin() as conn:
        for stmt in ddl:
            try:
                await conn.execute(text(stmt))
            except Exception as exc:
                log.warning("db.user_schema_sync_skipped", stmt=stmt[:60], exc_message=str(exc))
    log.info("users schema verified/synced")


async def ensure_branch_schema() -> None:
    """
    Idempotently add branch_id to users, widen role column, and migrate
    legacy 'admin' role → 'super_admin'. Safe to run on every startup.
    """
    engine = get_engine()
    ddl = [
        # Widen role column to accommodate new role names
        "ALTER TABLE users ALTER COLUMN role TYPE VARCHAR(50)",
        # Add branch_id FK (NULL = no branch, valid for super_admin and migrated users)
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS branch_id VARCHAR(36) REFERENCES branches(id) ON DELETE SET NULL",
        "CREATE INDEX IF NOT EXISTS ix_users_branch_id ON users(branch_id)",
        # Add created_by for audit trail
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_by VARCHAR(100)",
        # One-time role migration: admin → super_admin (idempotent — no-op after first run)
        "UPDATE users SET role = 'super_admin' WHERE role = 'admin'",
    ]
    async with engine.begin() as conn:
        for stmt in ddl:
            try:
                await conn.execute(text(stmt))
            except Exception as exc:
                log.warning("db.branch_schema_sync_skipped", stmt=stmt[:60], exc_message=str(exc))
    log.info("branch schema verified/synced")


_DEFAULT_BRANCHES = [
    {"name": "Chennai", "country": "India"},
    {"name": "Hyderabad", "country": "India"},
    {"name": "Qatar", "country": "Qatar"},
]


async def ensure_reset_schema() -> None:
    """Idempotently add user_id and branch_id to password_reset_requests."""
    engine = get_engine()
    ddl = [
        "ALTER TABLE password_reset_requests ADD COLUMN IF NOT EXISTS user_id VARCHAR(36)",
        "CREATE INDEX IF NOT EXISTS ix_prr_user_id ON password_reset_requests(user_id)",
        "ALTER TABLE password_reset_requests ADD COLUMN IF NOT EXISTS branch_id VARCHAR(36)",
        "CREATE INDEX IF NOT EXISTS ix_prr_branch_id ON password_reset_requests(branch_id)",
    ]
    async with engine.begin() as conn:
        for stmt in ddl:
            try:
                await conn.execute(text(stmt))
            except Exception as exc:
                log.warning("db.reset_schema_sync_skipped", stmt=stmt[:60], exc_message=str(exc))
    log.info("password_reset_requests schema verified/synced")


async def seed_branches() -> None:
    """Idempotently insert default branches. Never modifies existing rows."""
    factory = get_session_factory()
    async with factory() as session:
        for branch_data in _DEFAULT_BRANCHES:
            existing = await session.scalar(
                select(Branch).where(Branch.name == branch_data["name"])
            )
            if existing is None:
                session.add(Branch(
                    name=branch_data["name"],
                    country=branch_data["country"],
                    is_active=True,
                ))
        await session.commit()
    log.info("db.branches_seeded", branches=[b["name"] for b in _DEFAULT_BRANCHES])


async def seed_admin(username: str, plain_password: str) -> None:
    """
    Bootstrap super_admin ONLY if no super_admin account exists yet.

    Once a super_admin is present in the database (regardless of username),
    APP_USER / APP_PASS are completely ignored — authentication is DB-backed only.
    This prevents env-var credentials from being a permanent backdoor.
    """
    import bcrypt as _bcrypt

    def _hash(plain: str) -> str:
        return _bcrypt.hashpw(plain.encode("utf-8"), _bcrypt.gensalt(rounds=12)).decode("utf-8")

    factory = get_session_factory()

    async with factory() as session:
        # Check whether ANY super_admin exists (covers migrated accounts too)
        any_super_admin: User | None = await session.scalar(
            select(User).where(User.role == "super_admin", User.is_active == True)
        )
        if any_super_admin is not None:
            log.info(
                "db.bootstrap_skipped",
                reason="super_admin already exists",
                existing_username=any_super_admin.username,
            )
            return

        # No super_admin yet — create bootstrap account from env vars
        admin = User(
            username=username,
            password_hash=_hash(plain_password),
            role="super_admin",
            is_active=True,
            created_by="bootstrap",
        )
        session.add(admin)
        await session.commit()
        log.info(
            "db.bootstrap_super_admin_created",
            username=username,
            note="Set a strong password via admin UI and rotate APP_PASS",
        )


async def initialize(database_url: str, app_user: str, app_pass: str) -> bool:
    """
    Full initialization sequence. Returns True on success, False on failure.
    Failure is non-fatal — system falls back to static-credential mode.
    """
    from spir_dynamic.db.database import setup_engine

    try:
        setup_engine(database_url)
        await create_tables()
        await ensure_schema()
        await ensure_user_schema()
        await ensure_branch_schema()
        await ensure_reset_schema()
        await seed_branches()
        await seed_admin(app_user, app_pass)
        return True
    except Exception as exc:
        log.error("db.init_failed", exc_message=str(exc))
        return False
