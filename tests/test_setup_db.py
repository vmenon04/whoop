from __future__ import annotations

import pytest
from pydantic import SecretStr
from sqlalchemy import make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.fakes import seed_token
from whoop_lab.config import Settings
from whoop_lab.db import SessionFactory
from whoop_lab.setup_db import apply_app_config, ensure_grafana_role, setup_database

PASSWORD = "gr@f'ana:p%ss"


def grafana_engine(database_url: str, password: str = PASSWORD) -> AsyncEngine:
    url = make_url(database_url).set(username="grafana_ro", password=password)
    return create_async_engine(url, poolclass=NullPool)


async def test_app_config_follows_the_setting(
    session_factory: SessionFactory, settings: Settings
) -> None:
    await apply_app_config(
        session_factory, settings.model_copy(update={"local_timezone": "Europe/London"})
    )
    async with session_factory() as session:
        value = (await session.execute(text("SELECT local_timezone FROM app_config"))).scalar_one()
    assert value == "Europe/London"


async def test_grafana_role_reads_analysis_data_but_not_secrets(
    session_factory: SessionFactory, settings: Settings, database_url: str
) -> None:
    await seed_token(session_factory, access="at-SECRET")
    await setup_database(
        session_factory, settings.model_copy(update={"grafana_db_password": SecretStr(PASSWORD)})
    )
    engine = grafana_engine(database_url)
    try:
        async with engine.connect() as conn:
            for readable in (
                "SELECT count(*) FROM daily_outcomes",
                "SELECT count(*) FROM cycles",
                "SELECT count(*) FROM sync_runs",
                "SELECT count(*) FROM webhook_events WHERE status = 'pending'",
                "SELECT local_timezone FROM app_config",
            ):
                await conn.execute(text(readable))
        for forbidden in (
            "SELECT * FROM oauth_token",
            "SELECT access_token FROM oauth_token",
            "SELECT payload FROM webhook_events",
            "SELECT * FROM webhook_events",
            "INSERT INTO daily_tags (local_date, key, value) VALUES ('2026-01-01', 'a', 'b')",
            "DELETE FROM cycles",
            "UPDATE app_config SET local_timezone = 'UTC'",
        ):
            async with engine.connect() as conn:
                with pytest.raises(DBAPIError, match="permission denied"):
                    await conn.execute(text(forbidden))
    finally:
        await engine.dispose()


async def test_grafana_role_setup_is_repeatable_and_updates_the_password(
    session_factory: SessionFactory, database_url: str
) -> None:
    await ensure_grafana_role(session_factory, "first")
    await ensure_grafana_role(session_factory, "second")
    engine = grafana_engine(database_url, "second")
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT current_user"))).scalar_one() == "grafana_ro"
    finally:
        await engine.dispose()


async def test_no_grafana_password_skips_the_role(
    session_factory: SessionFactory, settings: Settings
) -> None:
    await setup_database(session_factory, settings)  # must not raise
