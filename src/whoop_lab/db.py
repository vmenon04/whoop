"""Async engine, session factory and the advisory-lock helper."""

from __future__ import annotations

from sqlalchemy import text
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


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)


async def advisory_xact_lock(session: AsyncSession, key: int) -> None:
    """Block until this transaction holds the lock. Released at commit or rollback."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
