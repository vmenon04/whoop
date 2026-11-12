from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import httpx
import pytest
import respx

from tests.fakes import FakeTokenServer, read_token, seed_token
from whoop_lab.config import Settings
from whoop_lab.db import SessionFactory
from whoop_lab.whoop.oauth import RefreshOutcome, refresh_if_needed


async def test_no_token_row_makes_no_call(
    session_factory: SessionFactory,
    http: httpx.AsyncClient,
    settings: Settings,
    token_server: FakeTokenServer,
) -> None:
    result = await refresh_if_needed(session_factory, http, settings)
    assert result.outcome is RefreshOutcome.NO_TOKEN
    assert token_server.calls == 0


async def test_fresh_token_is_not_refreshed(
    session_factory: SessionFactory,
    http: httpx.AsyncClient,
    settings: Settings,
    token_server: FakeTokenServer,
) -> None:
    await seed_token(session_factory, expires_in=3000)
    result = await refresh_if_needed(session_factory, http, settings)
    assert result.outcome is RefreshOutcome.NOT_DUE
    assert token_server.calls == 0


async def test_rotation_is_persisted(
    session_factory: SessionFactory,
    http: httpx.AsyncClient,
    settings: Settings,
    token_server: FakeTokenServer,
) -> None:
    await seed_token(session_factory, expires_in=60)
    result = await refresh_if_needed(session_factory, http, settings)
    assert result.outcome is RefreshOutcome.REFRESHED

    row = await read_token(session_factory)
    assert row is not None
    assert (row.access_token, row.refresh_token) == ("at-1", "rt-1")
    assert row.status == "active"
    assert 3500 < (row.expires_at - datetime.now(UTC)).total_seconds() <= 3600
    form = token_server.forms[0]
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "rt-0"
    assert form["scope"] == "offline"
    assert form["client_id"] == "test-client-id"

    # The rotated token is the one used next time.
    await seed_expiry_soon(session_factory)
    again = await refresh_if_needed(session_factory, http, settings)
    assert again.outcome is RefreshOutcome.REFRESHED
    assert token_server.forms[1]["refresh_token"] == "rt-1"


async def seed_expiry_soon(session_factory: SessionFactory) -> None:
    from sqlalchemy import text

    async with session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE oauth_token SET expires_at = now() + interval '30 seconds'")
        )


async def test_concurrent_refreshes_do_not_lose_the_token(
    session_factory: SessionFactory,
    http: httpx.AsyncClient,
    settings: Settings,
    token_server: FakeTokenServer,
) -> None:
    await seed_token(session_factory, expires_in=60)
    token_server.delay = 0.3  # hold the lock long enough for the others to queue up

    results = await asyncio.gather(
        *(refresh_if_needed(session_factory, http, settings) for _ in range(5))
    )

    outcomes = sorted(r.outcome.value for r in results)
    assert outcomes == ["not_due"] * 4 + ["refreshed"]
    assert token_server.calls == 1
    row = await read_token(session_factory)
    assert row is not None
    assert row.refresh_token == token_server.current_refresh
    assert row.status == "active"


async def test_rejected_refresh_requires_reauth_and_stops_calling(
    session_factory: SessionFactory,
    http: httpx.AsyncClient,
    settings: Settings,
    token_server: FakeTokenServer,
) -> None:
    await seed_token(session_factory, refresh="stale-token", expires_in=60)

    first = await refresh_if_needed(session_factory, http, settings)
    assert first.outcome is RefreshOutcome.REAUTH_REQUIRED
    assert "invalid_grant" in first.detail

    second = await refresh_if_needed(session_factory, http, settings)
    assert second.outcome is RefreshOutcome.AWAITING_REAUTH
    assert token_server.calls == 1  # never retried with the dead token

    row = await read_token(session_factory)
    assert row is not None
    assert row.status == "reauth_required"


@pytest.mark.parametrize("status", [500, 503, 429])
async def test_server_error_keeps_the_token(
    session_factory: SessionFactory,
    http: httpx.AsyncClient,
    settings: Settings,
    token_server: FakeTokenServer,
    status: int,
) -> None:
    await seed_token(session_factory, expires_in=60)
    token_server.fail_with = status
    result = await refresh_if_needed(session_factory, http, settings)
    assert result.outcome is RefreshOutcome.TRANSIENT_FAILURE
    row = await read_token(session_factory)
    assert row is not None
    assert (row.refresh_token, row.status) == ("rt-0", "active")


async def test_network_error_keeps_the_token(
    session_factory: SessionFactory,
    http: httpx.AsyncClient,
    settings: Settings,
    whoop_mock: respx.MockRouter,
) -> None:
    await seed_token(session_factory, expires_in=60)
    whoop_mock.post(settings.whoop_token_url).mock(side_effect=httpx.ConnectError("down"))
    result = await refresh_if_needed(session_factory, http, settings)
    assert result.outcome is RefreshOutcome.TRANSIENT_FAILURE
    row = await read_token(session_factory)
    assert row is not None
    assert (row.refresh_token, row.status) == ("rt-0", "active")


async def test_refresh_never_logs_token_material(
    session_factory: SessionFactory,
    http: httpx.AsyncClient,
    settings: Settings,
    token_server: FakeTokenServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    await seed_token(session_factory, expires_in=60)
    await refresh_if_needed(session_factory, http, settings)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret in ("rt-0", "rt-1", "at-1", "test-client-secret"):
        assert secret not in logged
