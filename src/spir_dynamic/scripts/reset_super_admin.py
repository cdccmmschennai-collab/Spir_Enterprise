"""
Emergency super_admin password reset script.

Run this directly with DB access when the super_admin cannot log in:

    DATABASE_URL=postgresql+asyncpg://... python -m spir_dynamic.scripts.reset_super_admin

Interactive — prompts for username and new password. Does NOT require the old password.
Only works when run with direct DB access (not over HTTP). No attack surface exposed.
"""
from __future__ import annotations

import asyncio
import getpass
import re
import sys


_PASSWORD_POLICY = re.compile(r"^(?=.*[A-Z])(?=.*[a-z])(?=.*\d).{8,72}$")


async def _reset(database_url: str) -> None:
    import bcrypt
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select

    sys.path.insert(0, "src")

    engine = create_async_engine(database_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    from spir_dynamic.db.models import User

    async with factory() as session:
        result = await session.execute(
            select(User).where(User.role == "super_admin", User.is_active == True)
        )
        admins = result.scalars().all()

    if not admins:
        print("ERROR: No active super_admin accounts found in the database.")
        sys.exit(1)

    print("\nActive super_admin accounts:")
    for i, a in enumerate(admins):
        print(f"  [{i}] {a.username} (id={a.id})")

    idx_str = input("\nEnter number to select account: ").strip()
    try:
        idx = int(idx_str)
        target: User = admins[idx]
    except (ValueError, IndexError):
        print("Invalid selection.")
        sys.exit(1)

    print(f"\nResetting password for: {target.username}")
    print("Password requirements: 8–72 chars, uppercase, lowercase, digit")

    while True:
        pw = getpass.getpass("New password: ")
        pw2 = getpass.getpass("Confirm password: ")
        if pw != pw2:
            print("Passwords do not match. Try again.")
            continue
        if not _PASSWORD_POLICY.match(pw):
            print("Password does not meet policy requirements. Try again.")
            continue
        break

    hashed = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")

    async with factory() as session:
        user: User | None = await session.get(User, target.id)
        if user is None:
            print("ERROR: User not found (deleted between listing and update?).")
            sys.exit(1)
        user.password_hash = hashed
        await session.commit()

    print(f"\nPassword successfully reset for '{target.username}'.")
    print("All active sessions will be invalidated on next request (JTI check).")
    await engine.dispose()


def main() -> None:
    import os
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable not set.")
        sys.exit(1)
    asyncio.run(_reset(database_url))


if __name__ == "__main__":
    main()
