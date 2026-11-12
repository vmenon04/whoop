from __future__ import annotations

import httpx

from tests import payloads
from tests.fakes import seed_token
from whoop_lab.db import SessionFactory
from whoop_lab.ingest import upsert
from whoop_lab.models import SyncRun


async def test_status_page_before_connecting(api: httpx.AsyncClient) -> None:
    response = await api.get("/")
    assert response.status_code == 200
    assert "Connect WHOOP" in response.text
    assert "No sync has run yet" in response.text


async def test_status_page_shows_counts_and_never_tokens(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    await seed_token(session_factory, access="at-SECRET", refresh="rt-SECRET")
    async with session_factory() as session, session.begin():
        await upsert.upsert_sleep(session, payloads.sleep())
        session.add(SyncRun(kind="reconcile", rows_upserted=3, error="sleep: HTTP 500"))

    response = await api.get("/")

    assert response.status_code == 200
    assert "Connected." in response.text
    assert "reconcile" in response.text
    assert "sleep: HTTP 500" in response.text
    assert "SECRET" not in response.text


async def test_status_page_prompts_for_reauth(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    await seed_token(session_factory, status="reauth_required")
    response = await api.get("/")
    assert "Reconnect WHOOP" in response.text


async def test_privacy_page_is_static(api: httpx.AsyncClient) -> None:
    response = await api.get("/privacy")
    assert response.status_code == 200
    assert "Privacy policy" in response.text
    assert "set-cookie" not in response.headers
