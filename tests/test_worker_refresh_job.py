from __future__ import annotations

import httpx
import respx
from pydantic import SecretStr

from tests.fakes import FakeTokenServer, seed_token
from whoop_lab.config import Settings
from whoop_lab.db import SessionFactory
from whoop_lab.notify import notify
from whoop_lab.whoop.oauth import RefreshOutcome
from whoop_lab.worker.context import WorkerContext
from whoop_lab.worker.jobs import refresh_token_job
from whoop_lab.worker.main import build_scheduler

NTFY = "https://ntfy.test/whoop-topic"


async def test_rejected_refresh_alerts_exactly_once(
    session_factory: SessionFactory,
    http: httpx.AsyncClient,
    settings: Settings,
    token_server: FakeTokenServer,
    whoop_mock: respx.MockRouter,
) -> None:
    settings = settings.model_copy(update={"ntfy_url": NTFY})
    ntfy = whoop_mock.post(NTFY).mock(return_value=httpx.Response(200))
    await seed_token(session_factory, refresh="dead", expires_in=60)
    ctx = WorkerContext.create(settings, session_factory, http)

    assert await refresh_token_job(ctx) is RefreshOutcome.REAUTH_REQUIRED
    assert await refresh_token_job(ctx) is RefreshOutcome.AWAITING_REAUTH

    assert ntfy.call_count == 1
    sent = ntfy.calls[0].request
    assert "/oauth/start" in sent.content.decode()
    assert "dead" not in sent.content.decode()


async def test_notify_is_a_noop_without_url(settings: Settings) -> None:
    assert await notify(settings, "t", "m") is False


async def test_notify_swallows_delivery_errors(
    settings: Settings, http: httpx.AsyncClient, whoop_mock: respx.MockRouter
) -> None:
    settings = settings.model_copy(update={"ntfy_url": NTFY, "ntfy_token": SecretStr("tk")})
    route = whoop_mock.post(NTFY).mock(return_value=httpx.Response(500))
    assert await notify(settings, "t", "m", http=http) is False
    assert route.calls[0].request.headers["authorization"] == "Bearer tk"


async def test_scheduler_declares_jobs(
    session_factory: SessionFactory, http: httpx.AsyncClient, settings: Settings
) -> None:
    scheduler = build_scheduler(WorkerContext.create(settings, session_factory, http))
    assert {
        "refresh_token",
        "process_events",
        "reconcile",
        "morning_recovery",
        "assign_tomorrow",
        "daily_analysis",
    } == {job.id for job in scheduler.get_jobs()}
