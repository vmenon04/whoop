from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import respx

from tests import payloads
from tests.fakes import seed_token
from whoop_lab.config import Settings
from whoop_lab.db import SessionFactory
from whoop_lab.health import check_morning_recovery
from whoop_lab.ingest import upsert

NTFY = "https://ntfy.test/whoop-topic"
NOON = datetime(2026, 6, 10, 12, 0, tzinfo=ZoneInfo("America/New_York"))
SLEEP = {"start": "2026-06-10T03:00:00.000Z", "end": "2026-06-10T11:00:00.000Z"}


async def test_alerts_when_this_mornings_recovery_is_missing(
    session_factory: SessionFactory,
    settings: Settings,
    http: httpx.AsyncClient,
    whoop_mock: respx.MockRouter,
) -> None:
    settings = settings.model_copy(update={"ntfy_url": NTFY})
    route = whoop_mock.post(NTFY).mock(return_value=httpx.Response(200))
    await seed_token(session_factory)

    assert await check_morning_recovery(session_factory, settings, http=http, now=NOON) is False
    assert route.call_count == 1
    assert "Jun 10" in route.calls[0].request.content.decode()


async def test_alerts_when_the_recovery_is_still_pending(
    session_factory: SessionFactory,
    settings: Settings,
    http: httpx.AsyncClient,
    whoop_mock: respx.MockRouter,
) -> None:
    settings = settings.model_copy(update={"ntfy_url": NTFY})
    route = whoop_mock.post(NTFY).mock(return_value=httpx.Response(200))
    await seed_token(session_factory)
    pending = payloads.recovery(score_state="PENDING_SCORE")
    del pending["score"]
    async with session_factory() as session, session.begin():
        await upsert.upsert_sleep(session, payloads.sleep(**SLEEP))
        await upsert.upsert_recovery(session, pending)

    assert await check_morning_recovery(session_factory, settings, http=http, now=NOON) is False
    assert route.call_count == 1


async def test_quiet_when_this_mornings_recovery_is_scored(
    session_factory: SessionFactory,
    settings: Settings,
    http: httpx.AsyncClient,
    whoop_mock: respx.MockRouter,
) -> None:
    settings = settings.model_copy(update={"ntfy_url": NTFY})
    route = whoop_mock.post(NTFY).mock(return_value=httpx.Response(200))
    await seed_token(session_factory)
    async with session_factory() as session, session.begin():
        await upsert.upsert_sleep(session, payloads.sleep(**SLEEP))
        await upsert.upsert_recovery(session, payloads.recovery())

    assert await check_morning_recovery(session_factory, settings, http=http, now=NOON) is True
    assert route.call_count == 0


async def test_quiet_before_whoop_is_connected(
    session_factory: SessionFactory,
    settings: Settings,
    http: httpx.AsyncClient,
    whoop_mock: respx.MockRouter,
) -> None:
    settings = settings.model_copy(update={"ntfy_url": NTFY})
    route = whoop_mock.post(NTFY).mock(return_value=httpx.Response(200))
    assert await check_morning_recovery(session_factory, settings, http=http, now=NOON) is True
    assert route.call_count == 0
