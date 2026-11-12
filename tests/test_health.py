from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import text

from tests.fakes import seed_token
from whoop_lab.api.app import create_app
from whoop_lab.config import Settings
from whoop_lab.db import SessionFactory
from whoop_lab.models import SyncRun


async def test_healthz_ok(settings: Settings, session_factory: SessionFactory) -> None:
    app = create_app(settings, session_factory)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://whoop.test"
        ) as client,
    ):
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def get_uptime(settings: Settings, session_factory: SessionFactory) -> httpx.Response:
    app = create_app(settings, session_factory)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://whoop.test"
        ) as client,
    ):
        return await client.get("/uptime")


async def add_sync(
    session_factory: SessionFactory, *, hours_ago: float, error: str | None = None
) -> None:
    finished = datetime.now(UTC) - timedelta(hours=hours_ago)
    async with session_factory() as session, session.begin():
        session.add(
            SyncRun(kind="reconcile", started_at=finished, finished_at=finished, error=error)
        )


async def test_uptime_is_degraded_before_the_first_sign_in(
    settings: Settings, session_factory: SessionFactory
) -> None:
    response = await get_uptime(settings, session_factory)
    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "checks": {"database": "ok", "token": "missing", "sync": "never"},
    }


async def test_uptime_is_ok_with_a_live_token_and_a_recent_sync(
    settings: Settings, session_factory: SessionFactory
) -> None:
    await seed_token(session_factory, access="at-SECRET")
    await add_sync(session_factory, hours_ago=1)
    response = await get_uptime(settings, session_factory)
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"database": "ok", "token": "ok", "sync": "ok"},
    }
    assert "SECRET" not in response.text


async def test_uptime_fails_when_the_last_good_sync_is_old(
    settings: Settings, session_factory: SessionFactory
) -> None:
    await seed_token(session_factory)
    await add_sync(session_factory, hours_ago=2 * settings.reconcile_interval_hours + 1)
    await add_sync(session_factory, hours_ago=0.1, error="WHOOP returned 500")  # recent but failed
    response = await get_uptime(settings, session_factory)
    assert response.status_code == 503
    assert response.json()["checks"]["sync"] == "stale"


async def test_uptime_fails_when_reauthorization_is_needed(
    settings: Settings, session_factory: SessionFactory
) -> None:
    await seed_token(session_factory)
    await add_sync(session_factory, hours_ago=1)
    async with session_factory() as session, session.begin():
        await session.execute(text("UPDATE oauth_token SET status = 'reauth_required'"))
    response = await get_uptime(settings, session_factory)
    assert response.status_code == 503
    assert response.json()["checks"]["token"] == "reauth_required"
