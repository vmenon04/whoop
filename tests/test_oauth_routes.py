from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx

from tests.fakes import FakeTokenServer, read_token
from whoop_lab.config import WHOOP_SCOPES, Settings
from whoop_lab.db import SessionFactory


async def test_start_redirects_to_whoop_with_eight_char_state(
    api: httpx.AsyncClient, settings: Settings
) -> None:
    response = await api.get("/oauth/start")
    assert response.status_code == 302
    target = urlsplit(response.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == settings.whoop_auth_url
    query = parse_qs(target.query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["test-client-id"]
    assert query["redirect_uri"] == ["https://whoop.test/oauth/callback"]
    assert query["scope"] == [" ".join(WHOOP_SCOPES)]
    assert "offline" in query["scope"][0]
    assert len(query["state"][0]) == 8
    assert api.cookies.get("whoop_oauth_state") == query["state"][0]
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "secure" in cookie


async def test_callback_exchanges_code_and_stores_token(
    api: httpx.AsyncClient, session_factory: SessionFactory, token_server: FakeTokenServer
) -> None:
    start = await api.get("/oauth/start")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]

    response = await api.get("/oauth/callback", params={"code": "the-code", "state": state})

    assert response.status_code == 200
    assert "Connected" in response.text
    form = token_server.forms[0]
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "the-code"
    assert form["redirect_uri"] == "https://whoop.test/oauth/callback"
    row = await read_token(session_factory)
    assert row is not None
    assert (row.access_token, row.refresh_token, row.status) == ("at-1", "rt-1", "active")
    assert row.lifetime_seconds == 3600


async def test_callback_rejects_wrong_state(
    api: httpx.AsyncClient, session_factory: SessionFactory, token_server: FakeTokenServer
) -> None:
    await api.get("/oauth/start")
    response = await api.get("/oauth/callback", params={"code": "c", "state": "WRONG123"})
    assert response.status_code == 400
    assert token_server.calls == 0
    assert await read_token(session_factory) is None


async def test_callback_without_cookie_is_rejected(
    api: httpx.AsyncClient, token_server: FakeTokenServer
) -> None:
    response = await api.get("/oauth/callback", params={"code": "c", "state": "abcd1234"})
    assert response.status_code == 400
    assert token_server.calls == 0


async def test_callback_reports_whoop_error(
    api: httpx.AsyncClient, token_server: FakeTokenServer
) -> None:
    start = await api.get("/oauth/start")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    response = await api.get("/oauth/callback", params={"error": "access_denied", "state": state})
    assert response.status_code == 400
    assert "access_denied" in response.text
    assert token_server.calls == 0


async def test_reauth_clears_reauth_required(
    api: httpx.AsyncClient, session_factory: SessionFactory, token_server: FakeTokenServer
) -> None:
    from tests.fakes import seed_token

    await seed_token(session_factory, refresh="dead", status="reauth_required")
    start = await api.get("/oauth/start")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    await api.get("/oauth/callback", params={"code": "c", "state": state})
    row = await read_token(session_factory)
    assert row is not None
    assert row.status == "active"
