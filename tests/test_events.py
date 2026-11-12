from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from tests import payloads
from tests.fakes import seed_token
from whoop_lab.config import Settings
from whoop_lab.db import SessionFactory
from whoop_lab.ingest import upsert
from whoop_lab.ingest.events import drain_events, process_next_event
from whoop_lab.models import Recovery, Sleep, WebhookEvent, Workout
from whoop_lab.whoop.client import WhoopClient

BASE = "https://api.prod.whoop.com/developer"
SLEEP_URL = f"{BASE}/v2/activity/sleep/{payloads.SLEEP_ID}"
RECOVERY_URL = f"{BASE}/v2/cycle/{payloads.CYCLE_ID}/recovery"
WORKOUT_URL = f"{BASE}/v2/activity/workout/{payloads.WORKOUT_ID}"
SLEEP = UUID(payloads.SLEEP_ID)
WORKOUT = UUID(payloads.WORKOUT_ID)


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def client(
    http: httpx.AsyncClient, session_factory: SessionFactory, settings: Settings
) -> WhoopClient:
    await seed_token(session_factory)
    return WhoopClient(http, session_factory, settings, sleep=no_sleep)


async def enqueue(
    session_factory: SessionFactory, type_: str, resource_id: str, trace_id: str = "t-1"
) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            insert(WebhookEvent).values(
                trace_id=trace_id,
                type=type_,
                resource_id=resource_id,
                user_id=payloads.USER_ID,
                payload={"id": resource_id, "type": type_, "trace_id": trace_id},
            )
        )


async def events(session_factory: SessionFactory) -> list[WebhookEvent]:
    async with session_factory() as session:
        return list(
            (await session.execute(select(WebhookEvent).order_by(WebhookEvent.id))).scalars()
        )


async def seed_rows(session_factory: SessionFactory) -> None:
    async with session_factory() as session, session.begin():
        await upsert.upsert_sleep(session, payloads.sleep())
        await upsert.upsert_recovery(session, payloads.recovery())
        await upsert.upsert_workout(session, payloads.workout())


async def test_sleep_updated_fetches_and_upserts(
    session_factory: SessionFactory,
    client: WhoopClient,
    settings: Settings,
    whoop_mock: respx.MockRouter,
) -> None:
    route = whoop_mock.get(SLEEP_URL).mock(return_value=httpx.Response(200, json=payloads.sleep()))
    await enqueue(session_factory, "sleep.updated", payloads.SLEEP_ID)

    assert await process_next_event(session_factory, client, settings) is True
    assert await process_next_event(session_factory, client, settings) is False

    assert route.call_count == 1
    async with session_factory() as session:
        assert (await session.get_one(Sleep, SLEEP)).score_state == "SCORED"
    (event,) = await events(session_factory)
    assert (event.status, event.attempts, event.error) == ("done", 1, None)
    assert event.processed_at is not None


async def test_recovery_updated_goes_through_sleep_then_cycle(
    session_factory: SessionFactory,
    client: WhoopClient,
    settings: Settings,
    whoop_mock: respx.MockRouter,
) -> None:
    sleep_route = whoop_mock.get(SLEEP_URL).mock(
        return_value=httpx.Response(200, json=payloads.sleep())
    )
    recovery_route = whoop_mock.get(RECOVERY_URL).mock(
        return_value=httpx.Response(200, json=payloads.recovery())
    )
    await enqueue(session_factory, "recovery.updated", payloads.SLEEP_ID)

    await drain_events(session_factory, client, settings)

    assert (sleep_route.call_count, recovery_route.call_count) == (1, 1)
    async with session_factory() as session:
        recovery = await session.get_one(Recovery, SLEEP)
        assert await session.get(Sleep, SLEEP) is not None
    assert recovery.hrv_rmssd_milli == pytest.approx(31.813562)
    assert (await events(session_factory))[0].status == "done"


async def test_recovery_event_when_cycle_recovery_moved_to_another_sleep(
    session_factory: SessionFactory,
    client: WhoopClient,
    settings: Settings,
    whoop_mock: respx.MockRouter,
) -> None:
    await seed_rows(session_factory)
    other = "aaaaaaaa-1111-2222-3333-444444444444"
    whoop_mock.get(SLEEP_URL).mock(return_value=httpx.Response(200, json=payloads.sleep()))
    whoop_mock.get(RECOVERY_URL).mock(
        return_value=httpx.Response(200, json=payloads.recovery(sleep_id=other))
    )
    await enqueue(session_factory, "recovery.updated", payloads.SLEEP_ID)

    await drain_events(session_factory, client, settings)

    async with session_factory() as session:
        assert (await session.get_one(Recovery, SLEEP)).deleted_at is not None
        assert (await session.get_one(Recovery, UUID(other))).deleted_at is None


async def test_workout_updated(
    session_factory: SessionFactory,
    client: WhoopClient,
    settings: Settings,
    whoop_mock: respx.MockRouter,
) -> None:
    whoop_mock.get(WORKOUT_URL).mock(return_value=httpx.Response(200, json=payloads.workout()))
    await enqueue(session_factory, "workout.updated", payloads.WORKOUT_ID)
    await drain_events(session_factory, client, settings)
    async with session_factory() as session:
        assert (await session.get_one(Workout, WORKOUT)).sport_name == "running"


@pytest.mark.parametrize(
    ("type_", "resource_id"),
    [
        ("sleep.deleted", payloads.SLEEP_ID),
        ("recovery.deleted", payloads.SLEEP_ID),
        ("workout.deleted", payloads.WORKOUT_ID),
    ],
)
async def test_deleted_events_soft_delete_without_calling_whoop(
    session_factory: SessionFactory,
    client: WhoopClient,
    settings: Settings,
    whoop_mock: respx.MockRouter,
    type_: str,
    resource_id: str,
) -> None:
    await seed_rows(session_factory)
    await enqueue(session_factory, type_, resource_id)

    await drain_events(session_factory, client, settings)

    assert whoop_mock.calls.call_count == 0
    async with session_factory() as session:
        sleep = await session.get_one(Sleep, SLEEP)
        recovery = await session.get_one(Recovery, SLEEP)
        workout = await session.get_one(Workout, WORKOUT)
    deleted = {
        "sleep": sleep.deleted_at is not None,
        "recovery": recovery.deleted_at is not None,
        "workout": workout.deleted_at is not None,
    }
    expected = {
        "sleep.deleted": {"sleep": True, "recovery": True, "workout": False},
        "recovery.deleted": {"sleep": False, "recovery": True, "workout": False},
        "workout.deleted": {"sleep": False, "recovery": False, "workout": True},
    }[type_]
    assert deleted == expected
    assert (await events(session_factory))[0].status == "done"


async def test_deleted_event_for_unknown_row_is_a_noop(
    session_factory: SessionFactory, client: WhoopClient, settings: Settings
) -> None:
    await enqueue(session_factory, "workout.deleted", payloads.WORKOUT_ID)
    await drain_events(session_factory, client, settings)
    assert (await events(session_factory))[0].status == "done"


async def test_404_on_fetch_is_treated_as_deleted(
    session_factory: SessionFactory,
    client: WhoopClient,
    settings: Settings,
    whoop_mock: respx.MockRouter,
) -> None:
    await seed_rows(session_factory)
    whoop_mock.get(SLEEP_URL).mock(return_value=httpx.Response(404))
    await enqueue(session_factory, "sleep.updated", payloads.SLEEP_ID)
    await drain_events(session_factory, client, settings)
    async with session_factory() as session:
        assert (await session.get_one(Sleep, SLEEP)).deleted_at is not None
    assert (await events(session_factory))[0].status == "done"


async def test_failure_is_recorded_and_retried_with_backoff(
    session_factory: SessionFactory,
    client: WhoopClient,
    settings: Settings,
    whoop_mock: respx.MockRouter,
) -> None:
    route = whoop_mock.get(SLEEP_URL).mock(return_value=httpx.Response(500))
    await enqueue(session_factory, "sleep.updated", payloads.SLEEP_ID)

    assert await process_next_event(session_factory, client, settings) is True

    (event,) = await events(session_factory)
    assert (event.status, event.attempts) == ("pending", 1)
    assert event.error is not None
    assert "500" in event.error
    assert event.next_attempt_at > datetime.now(UTC)
    # Not due yet, so it is not claimed again.
    assert await process_next_event(session_factory, client, settings) is False

    # Once due, a success completes it.
    route.mock(return_value=httpx.Response(200, json=payloads.sleep()))
    async with session_factory() as session, session.begin():
        await session.execute(text("UPDATE webhook_events SET next_attempt_at = now()"))
    assert await process_next_event(session_factory, client, settings) is True
    (event,) = await events(session_factory)
    assert (event.status, event.attempts, event.error) == ("done", 2, None)


async def test_backoff_grows_and_event_fails_after_max_attempts(
    session_factory: SessionFactory,
    client: WhoopClient,
    settings: Settings,
    whoop_mock: respx.MockRouter,
) -> None:
    settings = settings.model_copy(
        update={"event_max_attempts": 3, "event_backoff_base_seconds": 10}
    )
    whoop_mock.get(SLEEP_URL).mock(return_value=httpx.Response(500))
    await enqueue(session_factory, "sleep.updated", payloads.SLEEP_ID)

    delays: list[float] = []
    for _ in range(3):
        await process_next_event(session_factory, client, settings)
        (event,) = await events(session_factory)
        delays.append((event.next_attempt_at - datetime.now(UTC)).total_seconds())
        async with session_factory() as session, session.begin():
            await session.execute(
                text("UPDATE webhook_events SET next_attempt_at = now() WHERE status = 'pending'")
            )

    assert 8 < delays[0] <= 10
    assert 18 < delays[1] <= 20
    (event,) = await events(session_factory)
    assert (event.status, event.attempts) == ("failed", 3)
    assert await process_next_event(session_factory, client, settings) is False


async def test_partial_writes_roll_back_when_the_second_fetch_fails(
    session_factory: SessionFactory,
    client: WhoopClient,
    settings: Settings,
    whoop_mock: respx.MockRouter,
) -> None:
    whoop_mock.get(SLEEP_URL).mock(return_value=httpx.Response(200, json=payloads.sleep()))
    whoop_mock.get(RECOVERY_URL).mock(return_value=httpx.Response(500))
    await enqueue(session_factory, "recovery.updated", payloads.SLEEP_ID)
    await process_next_event(session_factory, client, settings)
    async with session_factory() as session:
        assert await session.get(Sleep, SLEEP) is None
    assert (await events(session_factory))[0].status == "pending"


async def test_missing_token_defers_without_spending_an_attempt(
    session_factory: SessionFactory,
    http: httpx.AsyncClient,
    settings: Settings,
    whoop_mock: respx.MockRouter,
) -> None:
    client = WhoopClient(http, session_factory, settings, sleep=no_sleep)  # no token row
    await enqueue(session_factory, "sleep.updated", payloads.SLEEP_ID)
    assert await process_next_event(session_factory, client, settings) is None
    (event,) = await events(session_factory)
    assert (event.status, event.attempts) == ("pending", 0)
    assert event.next_attempt_at > datetime.now(UTC)
    assert whoop_mock.calls.call_count == 0


async def test_non_uuid_id_fails_permanently(
    session_factory: SessionFactory, client: WhoopClient, settings: Settings
) -> None:
    await enqueue(session_factory, "sleep.updated", "10235")
    await drain_events(session_factory, client, settings)
    (event,) = await events(session_factory)
    assert (event.status, event.attempts) == ("failed", 1)


async def test_unknown_event_type_is_ignored(
    session_factory: SessionFactory, client: WhoopClient, settings: Settings
) -> None:
    await enqueue(session_factory, "cycle.updated", "1")
    await drain_events(session_factory, client, settings)
    (event,) = await events(session_factory)
    assert event.status == "done"
    assert event.error is not None
    assert "ignored" in event.error


async def test_two_workers_never_process_the_same_event(
    session_factory: SessionFactory,
    client: WhoopClient,
    settings: Settings,
    whoop_mock: respx.MockRouter,
) -> None:
    ids = [f"00000000-0000-0000-0000-{n:012d}" for n in range(12)]
    fetched: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        workout_id = request.url.path.rsplit("/", 1)[-1]
        fetched.append(workout_id)
        await asyncio.sleep(0.02)  # let the other worker run while this row is locked
        return httpx.Response(200, json=payloads.workout(workout_id))

    whoop_mock.get(url__regex=rf"{BASE}/v2/activity/workout/.+").mock(side_effect=handler)
    for n, workout_id in enumerate(ids):
        await enqueue(session_factory, "workout.updated", workout_id, trace_id=f"t-{n}")

    settings = settings.model_copy(update={"event_batch_size": 50})
    counts = await asyncio.gather(
        drain_events(session_factory, client, settings),
        drain_events(session_factory, client, settings),
        drain_events(session_factory, client, settings),
    )

    assert sorted(fetched) == sorted(ids)  # each exactly once
    assert sum(counts) == len(ids)
    assert all(e.status == "done" and e.attempts == 1 for e in await events(session_factory))


async def test_replaying_an_event_changes_no_data(
    session_factory: SessionFactory,
    client: WhoopClient,
    settings: Settings,
    whoop_mock: respx.MockRouter,
) -> None:
    whoop_mock.get(SLEEP_URL).mock(return_value=httpx.Response(200, json=payloads.sleep()))
    await enqueue(session_factory, "sleep.updated", payloads.SLEEP_ID, trace_id="a")
    await drain_events(session_factory, client, settings)

    async def snapshot() -> dict[str, Any]:
        async with session_factory() as session:
            row = (await session.execute(select(Sleep.__table__))).mappings().one()
        return dict(row)

    before = await snapshot()
    await enqueue(session_factory, "sleep.updated", payloads.SLEEP_ID, trace_id="b")
    await drain_events(session_factory, client, settings)
    assert await snapshot() == before
