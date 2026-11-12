from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests import payloads
from whoop_lab.db import SessionFactory
from whoop_lab.ingest import upsert
from whoop_lab.models import Base, Cycle, Recovery, Sleep, Workout
from whoop_lab.whoop import generated as gen

SLEEP = UUID(payloads.SLEEP_ID)
Upsert = Callable[[AsyncSession, dict[str, Any]], Awaitable[bool]]

CASES: list[tuple[type[Base], Upsert, Callable[..., dict[str, Any]]]] = [
    (Cycle, upsert.upsert_cycle, payloads.cycle),
    (Sleep, upsert.upsert_sleep, payloads.sleep),
    (Recovery, upsert.upsert_recovery, payloads.recovery),
    (Workout, upsert.upsert_workout, payloads.workout),
]


async def write(session_factory: SessionFactory, fn: Upsert, raw: dict[str, Any]) -> bool:
    async with session_factory() as session, session.begin():
        return await fn(session, raw)


async def snapshot(session_factory: SessionFactory, table: type[Base]) -> list[dict[str, Any]]:
    async with session_factory() as session:
        rows = (await session.execute(select(table.__table__))).mappings().all()
    return [dict(row) for row in rows]


@pytest.mark.parametrize(("table", "fn", "build"), CASES)
async def test_replay_changes_nothing(
    session_factory: SessionFactory,
    table: type[Base],
    fn: Upsert,
    build: Callable[..., dict[str, Any]],
) -> None:
    assert await write(session_factory, fn, build()) is True
    before = await snapshot(session_factory, table)
    assert len(before) == 1
    assert before[0]["raw"] == build()

    assert await write(session_factory, fn, build()) is False
    assert await snapshot(session_factory, table) == before


async def test_typed_columns_are_filled_from_the_payload(session_factory: SessionFactory) -> None:
    await write(session_factory, upsert.upsert_sleep, payloads.sleep())
    await write(session_factory, upsert.upsert_recovery, payloads.recovery())
    await write(session_factory, upsert.upsert_workout, payloads.workout())
    await write(session_factory, upsert.upsert_cycle, payloads.cycle())

    async with session_factory() as session:
        sleep = await session.get_one(Sleep, SLEEP)
        recovery = await session.get_one(Recovery, SLEEP)
        workout = await session.get_one(Workout, UUID(payloads.WORKOUT_ID))
        cycle = await session.get_one(Cycle, payloads.CYCLE_ID)

    assert sleep.nap is False
    assert sleep.total_slow_wave_sleep_time_milli == 6630370
    assert sleep.sleep_needed_need_from_recent_nap_milli == -12312
    assert sleep.sleep_efficiency_percentage == pytest.approx(91.69533848)
    assert sleep.timezone_offset == "-05:00"
    assert sleep.end.utcoffset() is not None
    assert recovery.hrv_rmssd_milli == pytest.approx(31.813562)
    assert recovery.resting_heart_rate == 64.0
    assert recovery.cycle_id == payloads.CYCLE_ID
    assert workout.sport_name == "running"
    assert workout.zone_five_milli == 300000
    assert cycle.strain == pytest.approx(5.2951527)
    assert cycle.step_count == 8421


async def test_every_spec_score_field_has_a_column() -> None:
    """If WHOOP adds a field and the models are regenerated, this fails until a column exists."""
    assert set(upsert.sleep_values(payloads.sleep())) <= set(Sleep.__table__.c.keys())
    assert set(upsert.recovery_values(payloads.recovery())) <= set(Recovery.__table__.c.keys())
    assert set(upsert.workout_values(payloads.workout())) <= set(Workout.__table__.c.keys())
    assert set(upsert.cycle_values(payloads.cycle())) <= set(Cycle.__table__.c.keys())
    assert set(gen.RecoveryScore.model_fields) <= set(Recovery.__table__.c.keys())


async def test_pending_then_scored_overwrites_cleanly(session_factory: SessionFactory) -> None:
    pending = payloads.recovery(score_state="PENDING_SCORE", updated_at="2026-11-10T11:30:00.000Z")
    del pending["score"]
    await write(session_factory, upsert.upsert_recovery, pending)
    async with session_factory() as session:
        row = await session.get_one(Recovery, SLEEP)
    assert (row.score_state, row.hrv_rmssd_milli, row.user_calibrating) == (
        "PENDING_SCORE",
        None,
        None,
    )

    assert await write(session_factory, upsert.upsert_recovery, payloads.recovery()) is True
    async with session_factory() as session:
        row = await session.get_one(Recovery, SLEEP)
        count = (await session.execute(select(func.count()).select_from(Recovery))).scalar_one()
    assert count == 1
    assert row.score_state == "SCORED"
    assert row.hrv_rmssd_milli == pytest.approx(31.813562)
    assert row.raw["score"]["recovery_score"] == 44.0


async def test_rescore_with_newer_updated_at_overwrites(session_factory: SessionFactory) -> None:
    await write(session_factory, upsert.upsert_recovery, payloads.recovery())
    rescored = payloads.recovery(updated_at="2026-11-10T18:00:00.000Z")
    rescored["score"]["hrv_rmssd_milli"] = 40.5
    assert await write(session_factory, upsert.upsert_recovery, rescored) is True
    async with session_factory() as session:
        row = await session.get_one(Recovery, SLEEP)
    assert row.hrv_rmssd_milli == 40.5
    assert row.raw == rescored


async def test_stale_fetch_does_not_overwrite_newer_row(session_factory: SessionFactory) -> None:
    newer = payloads.recovery(updated_at="2026-11-10T18:00:00.000Z")
    newer["score"]["hrv_rmssd_milli"] = 40.5
    await write(session_factory, upsert.upsert_recovery, newer)
    assert await write(session_factory, upsert.upsert_recovery, payloads.recovery()) is False
    async with session_factory() as session:
        row = await session.get_one(Recovery, SLEEP)
    assert row.hrv_rmssd_milli == 40.5


async def test_same_updated_at_with_different_payload_overwrites(
    session_factory: SessionFactory,
) -> None:
    await write(session_factory, upsert.upsert_cycle, payloads.cycle())
    changed = payloads.cycle(step_count=9000)
    assert await write(session_factory, upsert.upsert_cycle, changed) is True
    async with session_factory() as session:
        row = await session.get_one(Cycle, payloads.CYCLE_ID)
    assert row.step_count == 9000


async def test_open_cycle_has_no_end(session_factory: SessionFactory) -> None:
    current = payloads.cycle(score_state="PENDING_SCORE")
    del current["end"]
    del current["score"]
    await write(session_factory, upsert.upsert_cycle, current)
    async with session_factory() as session:
        row = await session.get_one(Cycle, payloads.CYCLE_ID)
    assert row.end is None
    assert row.strain is None


async def test_deleting_a_sleep_also_deletes_its_recovery(session_factory: SessionFactory) -> None:
    await write(session_factory, upsert.upsert_sleep, payloads.sleep())
    await write(session_factory, upsert.upsert_recovery, payloads.recovery())
    async with session_factory() as session, session.begin():
        assert await upsert.soft_delete_sleep(session, SLEEP) is True
    async with session_factory() as session, session.begin():
        assert await upsert.soft_delete_sleep(session, SLEEP) is False  # idempotent
    async with session_factory() as session:
        sleep = await session.get_one(Sleep, SLEEP)
        recovery = await session.get_one(Recovery, SLEEP)
    assert sleep.deleted_at is not None
    assert recovery.deleted_at is not None
    assert recovery.raw == payloads.recovery()  # history kept


async def test_refetch_revives_a_soft_deleted_row(session_factory: SessionFactory) -> None:
    await write(session_factory, upsert.upsert_workout, payloads.workout())
    async with session_factory() as session, session.begin():
        await upsert.soft_delete_workout(session, UUID(payloads.WORKOUT_ID))
    assert await write(session_factory, upsert.upsert_workout, payloads.workout()) is True
    async with session_factory() as session:
        row = await session.get_one(Workout, UUID(payloads.WORKOUT_ID))
    assert row.deleted_at is None


async def test_malformed_record_is_rejected(session_factory: SessionFactory) -> None:
    bad = payloads.sleep()
    del bad["nap"]
    with pytest.raises(ValidationError):
        await write(session_factory, upsert.upsert_sleep, bad)
