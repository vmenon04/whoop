from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import func, select, text
from typer.testing import CliRunner

from tests import payloads
from tests.fakes import read_token, seed_token
from whoop_lab.cli import app as cli_app
from whoop_lab.config import Settings
from whoop_lab.db import SessionFactory
from whoop_lab.ingest import upsert
from whoop_lab.ingest.sync import backfill, reconcile
from whoop_lab.models import Base, Cycle, Recovery, Sleep, SyncRun, Workout
from whoop_lab.whoop.client import WhoopClient

BASE = "https://api.prod.whoop.com/developer"
PATHS = {
    "cycle": f"{BASE}/v2/cycle",
    "sleep": f"{BASE}/v2/activity/sleep",
    "recovery": f"{BASE}/v2/recovery",
    "workout": f"{BASE}/v2/activity/workout",
}


def iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def uid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def client(
    http: httpx.AsyncClient,
    session_factory: SessionFactory,
    settings: Settings,
    whoop_mock: respx.MockRouter,
) -> WhoopClient:
    await seed_token(session_factory)
    whoop_mock.get(f"{BASE}/v2/user/profile/basic").mock(
        return_value=httpx.Response(
            200,
            json={
                "user_id": payloads.USER_ID,
                "email": "x@y.z",
                "first_name": "A",
                "last_name": "B",
            },
        )
    )
    return WhoopClient(http, session_factory, settings, sleep=no_sleep)


def serve(
    whoop_mock: respx.MockRouter, records: dict[str, list[dict[str, Any]]], page_size: int = 25
) -> dict[str, list[httpx.QueryParams]]:
    """Serve each collection in pages, like the real API. Returns the queries seen."""
    seen: dict[str, list[httpx.QueryParams]] = {name: [] for name in PATHS}

    def make(name: str) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            seen[name].append(request.url.params)
            offset = int(request.url.params.get("nextToken", "0"))
            items = records.get(name, [])
            body: dict[str, Any] = {"records": items[offset : offset + page_size]}
            if offset + page_size < len(items):
                body["next_token"] = str(offset + page_size)
            return httpx.Response(200, json=body)

        return handler

    for name, url in PATHS.items():
        whoop_mock.get(url).mock(side_effect=make(name))
    return seen


async def count(session_factory: SessionFactory, table: type[Base]) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(table))).scalar_one()


async def runs(session_factory: SessionFactory) -> list[SyncRun]:
    async with session_factory() as session:
        return list((await session.execute(select(SyncRun).order_by(SyncRun.id))).scalars())


async def test_backfill_pages_through_every_collection(
    session_factory: SessionFactory, client: WhoopClient, whoop_mock: respx.MockRouter
) -> None:
    records = {
        "cycle": [payloads.cycle(n) for n in range(1, 61)],
        "sleep": [payloads.sleep(uid(n)) for n in range(30)],
        "recovery": [payloads.recovery(uid(n)) for n in range(30)],
        "workout": [payloads.workout(uid(n)) for n in range(7)],
    }
    seen = serve(whoop_mock, records)

    summary = await backfill(client, session_factory)

    assert summary.ok
    assert (summary.records_seen, summary.rows_upserted, summary.rows_deleted) == (127, 127, 0)
    assert await count(session_factory, Cycle) == 60
    assert await count(session_factory, Sleep) == 30
    assert await count(session_factory, Recovery) == 30
    assert await count(session_factory, Workout) == 7
    assert len(seen["cycle"]) == 3  # 25 + 25 + 10
    assert all(q["limit"] == "25" for q in seen["cycle"])
    assert "start" not in seen["cycle"][0]

    (run,) = await runs(session_factory)
    assert (run.kind, run.rows_upserted, run.error) == ("backfill", 127, None)
    assert run.finished_at is not None
    token = await read_token(session_factory)
    assert token is not None
    assert token.whoop_user_id == payloads.USER_ID


async def test_running_a_sync_twice_changes_nothing(
    session_factory: SessionFactory, client: WhoopClient, whoop_mock: respx.MockRouter
) -> None:
    serve(whoop_mock, {"cycle": [payloads.cycle()], "sleep": [payloads.sleep()]})
    first = await backfill(client, session_factory)
    second = await backfill(client, session_factory)
    assert first.rows_upserted == 2
    assert second.rows_upserted == 0
    assert await count(session_factory, Cycle) == 1


async def test_reconcile_asks_for_the_trailing_window(
    session_factory: SessionFactory, client: WhoopClient, whoop_mock: respx.MockRouter
) -> None:
    seen = serve(whoop_mock, {})
    await reconcile(client, session_factory, window_days=7)
    for name in PATHS:
        start = datetime.fromisoformat(seen[name][0]["start"].replace("Z", "+00:00"))
        age = datetime.now(UTC) - start
        assert timedelta(days=7) <= age < timedelta(days=7, minutes=1)


async def test_reconcile_picks_up_rescored_and_new_records(
    session_factory: SessionFactory, client: WhoopClient, whoop_mock: respx.MockRouter
) -> None:
    pending = payloads.recovery(score_state="PENDING_SCORE", updated_at="2026-11-10T11:30:00.000Z")
    del pending["score"]
    async with session_factory() as session, session.begin():
        await upsert.upsert_recovery(session, pending)

    serve(whoop_mock, {"recovery": [payloads.recovery()], "cycle": [payloads.cycle()]})
    summary = await reconcile(client, session_factory, window_days=7)

    assert summary.rows_upserted == 2
    async with session_factory() as session:
        row = await session.get_one(Recovery, UUID(payloads.SLEEP_ID))
    assert row.score_state == "SCORED"


async def test_reconcile_soft_deletes_rows_whoop_no_longer_returns(
    session_factory: SessionFactory, client: WhoopClient, whoop_mock: respx.MockRouter
) -> None:
    now = datetime.now(UTC)
    kept, gone, too_recent, at_edge = uid(1), uid(2), uid(3), uid(4)

    def sleep_at(sleep_id: str, hours_ago: float) -> dict[str, Any]:
        start = now - timedelta(hours=hours_ago)
        return payloads.sleep(sleep_id, start=iso(start), end=iso(start + timedelta(hours=7)))

    local = [
        sleep_at(kept, 72),
        sleep_at(gone, 48),
        sleep_at(too_recent, 0.5),  # may simply not be listed yet
        sleep_at(at_edge, 7 * 24 - 2),  # too close to the window edge to judge
    ]
    async with session_factory() as session, session.begin():
        for raw in local:
            await upsert.upsert_sleep(session, raw)
            await upsert.upsert_recovery(session, payloads.recovery(raw["id"]))
        await session.execute(text("UPDATE sleeps SET fetched_at = now() - interval '1 day'"))
        await session.execute(text("UPDATE recoveries SET fetched_at = now() - interval '1 day'"))

    serve(
        whoop_mock,
        {"sleep": [local[0]], "recovery": [payloads.recovery(kept)], "cycle": [payloads.cycle()]},
    )
    summary = await reconcile(client, session_factory, window_days=7)

    async with session_factory() as session:
        state = {
            str(row.id): row.deleted_at is not None
            for row in (await session.execute(select(Sleep))).scalars()
        }
        recovery_gone = await session.get_one(Recovery, UUID(gone))
        recovery_kept = await session.get_one(Recovery, UUID(kept))
    assert state == {kept: False, gone: True, too_recent: False, at_edge: False}
    assert recovery_gone.deleted_at is not None
    assert recovery_kept.deleted_at is None
    assert summary.rows_deleted == 1
    assert (await runs(session_factory))[0].rows_deleted == 1


async def test_empty_answer_does_not_prune(
    session_factory: SessionFactory, client: WhoopClient, whoop_mock: respx.MockRouter
) -> None:
    start = datetime.now(UTC) - timedelta(hours=48)
    async with session_factory() as session, session.begin():
        await upsert.upsert_workout(
            session,
            payloads.workout(start=iso(start), end=iso(start + timedelta(hours=1))),
        )
        await session.execute(text("UPDATE workouts SET fetched_at = now() - interval '1 day'"))
    serve(whoop_mock, {})
    summary = await reconcile(client, session_factory, window_days=7)
    assert summary.rows_deleted == 0
    async with session_factory() as session:
        assert (await session.get_one(Workout, UUID(payloads.WORKOUT_ID))).deleted_at is None


async def test_backfill_never_prunes(
    session_factory: SessionFactory, client: WhoopClient, whoop_mock: respx.MockRouter
) -> None:
    start = datetime.now(UTC) - timedelta(hours=48)
    async with session_factory() as session, session.begin():
        await upsert.upsert_workout(
            session, payloads.workout(start=iso(start), end=iso(start + timedelta(hours=1)))
        )
        await session.execute(text("UPDATE workouts SET fetched_at = now() - interval '1 day'"))
    serve(whoop_mock, {"workout": [payloads.workout(uid(9))]})
    summary = await backfill(client, session_factory, since=datetime.now(UTC) - timedelta(days=30))
    assert summary.rows_deleted == 0


async def test_one_failing_collection_does_not_stop_the_others(
    session_factory: SessionFactory, client: WhoopClient, whoop_mock: respx.MockRouter
) -> None:
    serve(whoop_mock, {"cycle": [payloads.cycle()], "workout": [payloads.workout()]})
    whoop_mock.get(PATHS["sleep"]).mock(return_value=httpx.Response(500))

    summary = await reconcile(client, session_factory, window_days=7)

    assert not summary.ok
    assert any(error.startswith("sleep:") for error in summary.errors)
    assert await count(session_factory, Cycle) == 1
    assert await count(session_factory, Workout) == 1
    (run,) = await runs(session_factory)
    assert run.error is not None
    assert run.finished_at is not None


async def test_one_malformed_record_does_not_block_the_page(
    session_factory: SessionFactory, client: WhoopClient, whoop_mock: respx.MockRouter
) -> None:
    bad = payloads.sleep(uid(1))
    del bad["nap"]
    serve(whoop_mock, {"sleep": [bad, payloads.sleep(uid(2))]})
    summary = await backfill(client, session_factory)
    assert await count(session_factory, Sleep) == 1
    assert any("validation" in error for error in summary.errors)


async def test_sync_without_a_token_records_the_failure(
    session_factory: SessionFactory,
    http: httpx.AsyncClient,
    settings: Settings,
    whoop_mock: respx.MockRouter,
) -> None:
    client = WhoopClient(http, session_factory, settings, sleep=no_sleep)
    summary = await reconcile(client, session_factory, window_days=7)
    assert not summary.ok
    assert whoop_mock.calls.call_count == 0
    (run,) = await runs(session_factory)
    assert run.error is not None
    assert "WhoopAuthError" in run.error


def test_cli_lists_the_ingestion_commands() -> None:
    result = CliRunner().invoke(cli_app, ["--help"])
    assert result.exit_code == 0
    assert "backfill" in result.output
    assert "sync" in result.output
