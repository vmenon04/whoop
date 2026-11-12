from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine

from whoop_lab.config import Settings
from whoop_lab.db import database_url_from_env, make_engine


def make(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "whoop_client_id": "cid",
        "whoop_client_secret": "secret",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_redirect_uri_is_built_from_public_base_url() -> None:
    settings = make(public_base_url="https://example.test/")
    assert settings.redirect_uri == "https://example.test/oauth/callback"


def test_secret_is_not_in_repr() -> None:
    assert "secret" not in repr(make().whoop_client_secret)


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make(local_timezone="Mars/Olympus")


@pytest.mark.parametrize("hour", [0, 6, 11, 24])
def test_assignment_hour_must_be_in_the_second_half_of_the_day(hour: int) -> None:
    with pytest.raises(ValidationError):
        make(assign_hour=hour)
    assert make(assign_hour=12).assign_hour == 12


NASTY = "p@ss:w/rd#1?x%y&z=$ 'q\""


def test_database_url_is_built_from_parts_with_any_password() -> None:
    settings = Settings(
        _env_file=None,
        whoop_client_id="cid",
        whoop_client_secret=SecretStr("secret"),
        postgres_user="whoop",
        postgres_password=SecretStr(NASTY),
        postgres_db="whoop_lab",
    )
    url = settings.db_url
    assert (url.username, url.password, url.host, url.port, url.database) == (
        "whoop",
        NASTY,
        "db",
        5432,
        "whoop_lab",
    )
    assert url.drivername == "postgresql+asyncpg"
    assert NASTY not in repr(settings)


def test_full_database_url_wins_over_parts() -> None:
    settings = make(postgres_user="other", postgres_password="x", postgres_db="other")
    assert settings.db_url.database == "db"


def test_missing_database_settings_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, whoop_client_id="cid", whoop_client_secret=SecretStr("s"))


def test_env_helper_matches_settings() -> None:
    env = {"POSTGRES_USER": "whoop", "POSTGRES_PASSWORD": NASTY, "POSTGRES_DB": "whoop_lab"}
    url = database_url_from_env(env)
    assert url.password == NASTY
    assert url.host == "db"
    assert database_url_from_env({"DATABASE_URL": "postgresql+asyncpg://a:b@h/d"}).host == "h"
    with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
        database_url_from_env({"POSTGRES_USER": "whoop", "POSTGRES_DB": "x"})


async def test_connects_with_a_password_full_of_special_characters(
    engine: AsyncEngine, database_url: str
) -> None:
    base = make_url(database_url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP ROLE IF EXISTS nasty_role"))
        quoted = NASTY.replace("'", "''")
        await conn.execute(text(f"CREATE ROLE nasty_role LOGIN PASSWORD '{quoted}'"))
    assert base.host is not None
    assert base.database is not None
    settings = Settings(
        _env_file=None,
        whoop_client_id="cid",
        whoop_client_secret=SecretStr("secret"),
        postgres_host=base.host,
        postgres_port=base.port or 5432,
        postgres_user="nasty_role",
        postgres_password=SecretStr(NASTY),
        postgres_db=base.database,
    )
    nasty_engine = make_engine(settings.db_url)
    try:
        async with nasty_engine.connect() as conn:
            assert (await conn.execute(text("SELECT current_user"))).scalar_one() == "nasty_role"
    finally:
        await nasty_engine.dispose()
