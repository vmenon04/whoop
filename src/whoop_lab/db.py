"""Async engine, session factory and the advisory-lock helper."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import URL, make_url, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

type SessionFactory = async_sessionmaker[AsyncSession]

# Arbitrary constant. Every code path that reads-then-writes the token row
# (code exchange and refresh) takes this lock first.
TOKEN_LOCK_KEY = 7_746_001


DRIVER = "postgresql+asyncpg"


def build_database_url(
    *, user: str, password: str, database: str, host: str = "db", port: int = 5432
) -> URL:
    """Assemble the URL from parts. URL.create escapes them, so any password is safe."""
    return URL.create(
        DRIVER, username=user, password=password, host=host, port=port, database=database
    )


def database_url_from_env(env: Mapping[str, str]) -> URL:
    """DATABASE_URL if set, otherwise the POSTGRES_* parts. Used by Settings and Alembic."""
    if env.get("DATABASE_URL"):
        return make_url(env["DATABASE_URL"])
    missing = [
        name for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB") if not env.get(name)
    ]
    if missing:
        raise RuntimeError(f"set DATABASE_URL or {', '.join(missing)}")
    return build_database_url(
        user=env["POSTGRES_USER"],
        password=env["POSTGRES_PASSWORD"],
        database=env["POSTGRES_DB"],
        host=env.get("POSTGRES_HOST") or "db",
        port=int(env.get("POSTGRES_PORT") or 5432),
    )


def make_engine(database_url: str | URL) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)


async def advisory_xact_lock(session: AsyncSession, key: int) -> None:
    """Block until this transaction holds the lock. Released at commit or rollback."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
