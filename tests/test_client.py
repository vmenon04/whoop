from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import text

from tests.fakes import seed_token
from whoop_lab.config import Settings
from whoop_lab.db import SessionFactory
from whoop_lab.whoop.client import (
    WhoopAPIError,
    WhoopAuthError,
    WhoopClient,
    WhoopNotFoundError,
    WhoopRateLimitedError,
)

BASE = "https://api.prod.whoop.com/developer"


class Sleeper:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


@pytest.fixture
def sleeper() -> Sleeper:
    return Sleeper()


@pytest.fixture
async def client(
    http: httpx.AsyncClient, session_factory: SessionFactory, settings: Settings, sleeper: Sleeper
) -> WhoopClient:
    await seed_token(session_factory, access="at-0")
    return WhoopClient(http, session_factory, settings, sleep=sleeper)


async def test_sends_bearer_token(client: WhoopClient, whoop_mock: respx.MockRouter) -> None:
    route = whoop_mock.get(f"{BASE}/v2/cycle/93845").mock(
        return_value=httpx.Response(200, json={"id": 93845})
    )
    assert await client.get_cycle(93845) == {"id": 93845}
    assert route.calls[0].request.headers["authorization"] == "Bearer at-0"


async def test_pagination_follows_next_token_to_the_end(
    client: WhoopClient, whoop_mock: respx.MockRouter
) -> None:
    pages = {
        None: {"records": [{"id": 1}, {"id": 2}], "next_token": "t1"},
        "t1": {"records": [{"id": 3}], "next_token": "t2"},
        "t2": {"records": [{"id": 4}]},
    }
    seen: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params)
        return httpx.Response(200, json=pages[request.url.params.get("nextToken")])

    whoop_mock.get(f"{BASE}/v2/activity/sleep").mock(side_effect=handler)

    start = datetime(2026, 11, 1, tzinfo=UTC)
    records = [r async for r in client.iter_collection("sleep", start=start)]

    assert [r["id"] for r in records] == [1, 2, 3, 4]
    assert len(seen) == 3
    assert seen[0]["limit"] == "25"
    assert seen[0]["start"] == "2026-11-01T00:00:00.000Z"
    assert "nextToken" not in seen[0]
    assert seen[2]["nextToken"] == "t2"


async def test_429_waits_for_reset_then_retries(
    client: WhoopClient, whoop_mock: respx.MockRouter, sleeper: Sleeper
) -> None:
    whoop_mock.get(f"{BASE}/v2/cycle/1").mock(
        side_effect=[
            httpx.Response(429, headers={"X-RateLimit-Reset": "17", "X-RateLimit-Remaining": "0"}),
            httpx.Response(200, json={"id": 1}),
        ]
    )
    assert await client.get_cycle(1) == {"id": 1}
    assert len(sleeper.waits) == 1
    assert 17 <= sleeper.waits[0] <= 18.1


async def test_429_with_a_distant_reset_gives_up(
    client: WhoopClient, whoop_mock: respx.MockRouter, sleeper: Sleeper
) -> None:
    whoop_mock.get(f"{BASE}/v2/cycle/1").mock(
        return_value=httpx.Response(429, headers={"X-RateLimit-Reset": "40000"})
    )
    with pytest.raises(WhoopRateLimitedError):
        await client.get_cycle(1)
    assert sleeper.waits == []


async def test_pauses_before_exhausting_the_window(
    client: WhoopClient, whoop_mock: respx.MockRouter, sleeper: Sleeper
) -> None:
    headers = {
        "X-RateLimit-Limit": "100, 100;window=60, 10000;window=86400",
        "X-RateLimit-Remaining": "1",
        "X-RateLimit-Reset": "30",
    }
    whoop_mock.get(f"{BASE}/v2/cycle/1").mock(
        return_value=httpx.Response(200, json={"id": 1}, headers=headers)
    )
    await client.get_cycle(1)
    assert sleeper.waits == []
    await client.get_cycle(1)
    assert len(sleeper.waits) == 1
    assert 29 <= sleeper.waits[0] <= 31.1


async def test_401_rereads_a_rotated_token_once(
    client: WhoopClient,
    whoop_mock: respx.MockRouter,
    session_factory: SessionFactory,
) -> None:
    used: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        used.append(request.headers["authorization"])
        if len(used) == 1:
            # The refresh job rotates the token while this request is in flight.
            async with session_factory() as session, session.begin():
                await session.execute(text("UPDATE oauth_token SET access_token = 'at-1'"))
            return httpx.Response(401)
        return httpx.Response(200, json={"id": 1})

    whoop_mock.get(f"{BASE}/v2/cycle/1").mock(side_effect=handler)
    assert await client.get_cycle(1) == {"id": 1}
    assert used == ["Bearer at-0", "Bearer at-1"]


async def test_persistent_401_raises_auth_error(
    client: WhoopClient, whoop_mock: respx.MockRouter
) -> None:
    route = whoop_mock.get(f"{BASE}/v2/cycle/1").mock(return_value=httpx.Response(401))
    with pytest.raises(WhoopAuthError):
        await client.get_cycle(1)
    assert route.call_count == 2


async def test_5xx_and_transport_errors_are_retried(
    client: WhoopClient, whoop_mock: respx.MockRouter, sleeper: Sleeper
) -> None:
    whoop_mock.get(f"{BASE}/v2/cycle/1").mock(
        side_effect=[
            httpx.Response(503),
            httpx.ReadTimeout("slow"),
            httpx.Response(200, json={"id": 1}),
        ]
    )
    assert await client.get_cycle(1) == {"id": 1}
    assert len(sleeper.waits) == 2


async def test_5xx_gives_up_after_max_retries(
    client: WhoopClient, whoop_mock: respx.MockRouter
) -> None:
    whoop_mock.get(f"{BASE}/v2/cycle/1").mock(return_value=httpx.Response(500))
    with pytest.raises(WhoopAPIError) as info:
        await client.get_cycle(1)
    assert info.value.status == 500


async def test_404_raises_not_found(client: WhoopClient, whoop_mock: respx.MockRouter) -> None:
    whoop_mock.get(f"{BASE}/v2/activity/workout/abc").mock(return_value=httpx.Response(404))
    with pytest.raises(WhoopNotFoundError):
        await client.get_workout("abc")


async def test_no_token_makes_no_request(
    http: httpx.AsyncClient,
    session_factory: SessionFactory,
    settings: Settings,
    whoop_mock: respx.MockRouter,
) -> None:
    client = WhoopClient(http, session_factory, settings)
    with pytest.raises(WhoopAuthError):
        await client.get_profile()
    assert whoop_mock.calls.call_count == 0


async def test_reauth_required_makes_no_request(
    http: httpx.AsyncClient,
    session_factory: SessionFactory,
    settings: Settings,
    whoop_mock: respx.MockRouter,
) -> None:
    await seed_token(session_factory, status="reauth_required")
    client = WhoopClient(http, session_factory, settings)
    with pytest.raises(WhoopAuthError):
        await client.get_profile()
    assert whoop_mock.calls.call_count == 0
