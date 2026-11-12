"""Alembic environment. Async engine, URL from DATABASE_URL or the POSTGRES_* parts."""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy import URL, make_url, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from whoop_lab.db import database_url_from_env
from whoop_lab.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> URL:
    # Tests pass a URL through config attributes; deployments use the environment.
    override = config.attributes.get("database_url")
    return make_url(override) if override else database_url_from_env(os.environ)


def run_migrations_offline() -> None:
    context.configure(
        url=_url().render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_async_engine(_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(_run_online())
