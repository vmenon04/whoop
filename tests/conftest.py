"""Shared fixtures. Tests run against a real Postgres 16 started by testcontainers."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

# The container is stopped by the fixture's context manager, so the Ryuk reaper
# (which cannot mount the Docker socket on Docker Desktop for macOS) is not needed.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

import httpx
import pytest
import respx
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

from tests.fakes import FakeTokenServer
from whoop_lab.config import Settings
from whoop_lab.db import SessionFactory, make_session_factory
from whoop_lab.models import Base


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:16", driver="asyncpg") as container:
        url: str = container.get_connection_url()
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
        yield url


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> SessionFactory:
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return make_session_factory(engine)


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        database_url=database_url,
        whoop_client_id="test-client-id",
        whoop_client_secret=SecretStr("test-client-secret"),
        public_base_url="https://whoop.test",
        event_backoff_base_seconds=1,
    )


@pytest.fixture
def whoop_mock() -> Iterator[respx.MockRouter]:
    """Every outbound httpx call is intercepted. Unmocked requests fail the test."""
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


@pytest.fixture
def token_server(whoop_mock: respx.MockRouter, settings: Settings) -> FakeTokenServer:
    server = FakeTokenServer()
    whoop_mock.post(settings.whoop_token_url).mock(side_effect=server)
    return server


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client
