"""The day-to-outcome mapping. Outcome for local date D = recovery of the main
sleep ending on the morning of D+1, in the configured zone (America/New_York here)."""

from __future__ import annotations

import itertools
from datetime import date
from typing import Any

import pytest
from sqlalchemy import text

from tests import payloads
from whoop_lab.db import SessionFactory
from whoop_lab.ingest import upsert


def uid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


async def add_night(
    session_factory: SessionFactory,
    n: int,
    start: str,
    end: str,
    *,
    hrv: float | None = 50.0,
    nap: bool = False,
    cycle_id: int | None = None,
    recovery_state: str = "SCORED",
    sleep_overrides: dict[str, Any] | None = None,
) -> str:
    sleep_id = uid(n)
    cycle = cycle_id if cycle_id is not None else 1000 + n
    sleep = payloads.sleep(sleep_id, start=start, end=end, nap=nap, cycle_id=cycle)
    sleep.update(sleep_overrides or {})
    async with session_factory() as session, session.begin():
        await upsert.upsert_sleep(session, sleep)
        if hrv is not None:
            recovery = payloads.recovery(sleep_id, cycle_id=cycle, score_state=recovery_state)
            if recovery_state == "SCORED":
                recovery["score"]["hrv_rmssd_milli"] = hrv
            else:
                del recovery["score"]
            await upsert.upsert_recovery(session, recovery)
    return sleep_id


async def outcomes(session_factory: SessionFactory) -> dict[date, dict[str, Any]]:
    async with session_factory() as session:
        rows = (await session.execute(text("SELECT * FROM daily_outcomes"))).mappings().all()
    return {row["local_date"]: dict(row) for row in rows}


async def test_outcome_for_day_d_is_the_recovery_from_the_next_morning(
    session_factory: SessionFactory,
) -> None:
    # Asleep 23:00 on Jun 9, awake 07:00 on Jun 10 (EDT, UTC-4).
    sleep_id = await add_night(
        session_factory, 1, "2026-06-10T03:00:00.000Z", "2026-06-10T11:00:00.000Z", hrv=62.0
    )
    rows = await outcomes(session_factory)
    row = rows[date(2026, 6, 9)]
    assert str(row["outcome_sleep_id"]) == sleep_id
    assert row["outcome_scored"] is True
    assert row["hrv_rmssd_milli"] == 62.0
    assert row["ln_hrv_rmssd"] == pytest.approx(4.1271343850)
    assert row["deep_sleep_minutes"] == pytest.approx(6630370 / 60000)
    assert row["sleep_efficiency_percentage"] == pytest.approx(91.69533848)
    # The wake date itself has no outcome yet: that needs the following night.
    assert rows[date(2026, 6, 10)]["outcome_sleep_id"] is None
    assert rows[date(2026, 6, 10)]["outcome_scored"] is False


async def test_local_date_not_utc_date_decides_the_day(session_factory: SessionFactory) -> None:
    # Ends 23:30 local on Jun 9, which is already Jun 10 in UTC.
    await add_night(session_factory, 1, "2026-06-09T20:00:00.000Z", "2026-06-10T03:30:00.000Z")
    rows = await outcomes(session_factory)
    assert rows[date(2026, 6, 8)]["outcome_sleep_id"] is not None


async def test_fall_back_dst_change(session_factory: SessionFactory) -> None:
    # Clocks go back at 06:00 UTC on Nov 1, 2026 (EDT, UTC-4 -> EST, UTC-5).
    # Night A wakes 07:00 EDT on Oct 31. Night B spans the change and wakes 06:30 EST on Nov 1.
    # Night C wakes 23:30 EST on Nov 1, which a fixed UTC-4 offset would call Nov 2.
    a = await add_night(session_factory, 1, "2026-10-31T03:00:00.000Z", "2026-10-31T11:00:00.000Z")
    b = await add_night(session_factory, 2, "2026-11-01T03:00:00.000Z", "2026-11-01T11:30:00.000Z")
    c = await add_night(session_factory, 3, "2026-11-01T22:00:00.000Z", "2026-11-02T04:30:00.000Z")

    rows = await outcomes(session_factory)

    assert str(rows[date(2026, 10, 30)]["outcome_sleep_id"]) == a
    # Nov 1 has two non-nap sleeps ending on it (B at 06:30 and C at 23:30): the longer wins.
    assert str(rows[date(2026, 10, 31)]["outcome_sleep_id"]) == b
    assert c not in {str(r["outcome_sleep_id"]) for r in rows.values()}
    assert rows[date(2026, 11, 1)]["outcome_sleep_id"] is None  # nothing ended on Nov 2 local


async def test_spring_forward_dst_change(session_factory: SessionFactory) -> None:
    # Clocks go forward at 07:00 UTC on Mar 8, 2026 (EST, UTC-5 -> EDT, UTC-4).
    # Night A: asleep 23:00 EST Mar 7, awake 07:30 EDT Mar 8 (a 7.5 hour night).
    # Night B: asleep 20:00, awake 23:30 EDT on Mar 9. A fixed UTC-5 offset would say 22:30;
    # either way it is Mar 9 locally, while its UTC date is Mar 10.
    a = await add_night(session_factory, 1, "2026-03-08T04:00:00.000Z", "2026-03-08T11:30:00.000Z")
    b = await add_night(session_factory, 2, "2026-03-10T00:00:00.000Z", "2026-03-10T03:30:00.000Z")
    rows = await outcomes(session_factory)
    assert str(rows[date(2026, 3, 7)]["outcome_sleep_id"]) == a
    assert str(rows[date(2026, 3, 8)]["outcome_sleep_id"]) == b
    assert date(2026, 3, 9) not in rows or rows[date(2026, 3, 9)]["outcome_sleep_id"] is None


async def test_nap_never_becomes_the_outcome(session_factory: SessionFactory) -> None:
    night = await add_night(
        session_factory, 1, "2026-06-10T03:00:00.000Z", "2026-06-10T11:00:00.000Z", hrv=55.0
    )
    # A long afternoon nap the same day, longer than the night, with its own recovery row.
    await add_night(
        session_factory,
        2,
        "2026-06-10T16:00:00.000Z",
        "2026-06-11T01:00:00.000Z",
        hrv=99.0,
        nap=True,
    )
    rows = await outcomes(session_factory)
    assert str(rows[date(2026, 6, 9)]["outcome_sleep_id"]) == night
    assert rows[date(2026, 6, 9)]["hrv_rmssd_milli"] == 55.0
    assert all(r["hrv_rmssd_milli"] != 99.0 for r in rows.values())


async def test_night_with_only_a_nap_has_no_outcome(session_factory: SessionFactory) -> None:
    await add_night(session_factory, 1, "2026-06-09T03:00:00.000Z", "2026-06-09T11:00:00.000Z")
    await add_night(
        session_factory, 2, "2026-06-10T16:00:00.000Z", "2026-06-10T17:00:00.000Z", nap=True
    )
    await add_night(session_factory, 3, "2026-06-11T03:00:00.000Z", "2026-06-11T11:00:00.000Z")
    rows = await outcomes(session_factory)
    assert rows[date(2026, 6, 9)]["outcome_sleep_id"] is None
    assert rows[date(2026, 6, 9)]["outcome_scored"] is False
    assert rows[date(2026, 6, 10)]["outcome_scored"] is True


async def test_longest_of_two_sleeps_the_same_morning_wins(
    session_factory: SessionFactory,
) -> None:
    await add_night(
        session_factory, 1, "2026-06-10T02:00:00.000Z", "2026-06-10T04:00:00.000Z", hrv=40.0
    )
    long = await add_night(
        session_factory, 2, "2026-06-10T05:00:00.000Z", "2026-06-10T11:00:00.000Z", hrv=60.0
    )
    rows = await outcomes(session_factory)
    assert str(rows[date(2026, 6, 9)]["outcome_sleep_id"]) == long
    assert rows[date(2026, 6, 9)]["hrv_rmssd_milli"] == 60.0


@pytest.mark.parametrize("state", ["PENDING_SCORE", "UNSCORABLE"])
async def test_unscored_recovery_is_not_an_outcome(
    session_factory: SessionFactory, state: str
) -> None:
    await add_night(
        session_factory,
        1,
        "2026-06-10T03:00:00.000Z",
        "2026-06-10T11:00:00.000Z",
        recovery_state=state,
    )
    row = (await outcomes(session_factory))[date(2026, 6, 9)]
    assert row["outcome_sleep_id"] is not None
    assert row["outcome_scored"] is False
    assert row["hrv_rmssd_milli"] is None
    assert row["ln_hrv_rmssd"] is None


async def test_soft_deleted_rows_are_ignored(session_factory: SessionFactory) -> None:
    from uuid import UUID

    await add_night(session_factory, 9, "2026-06-08T03:00:00.000Z", "2026-06-08T11:00:00.000Z")
    sleep_id = await add_night(
        session_factory, 1, "2026-06-10T03:00:00.000Z", "2026-06-10T11:00:00.000Z"
    )
    await add_night(session_factory, 2, "2026-06-11T03:00:00.000Z", "2026-06-11T11:00:00.000Z")
    async with session_factory() as session, session.begin():
        await upsert.soft_delete_recovery(session, UUID(sleep_id))
    row = (await outcomes(session_factory))[date(2026, 6, 9)]
    assert row["outcome_scored"] is False

    async with session_factory() as session, session.begin():
        await upsert.soft_delete_sleep(session, UUID(sleep_id))
    assert (await outcomes(session_factory))[date(2026, 6, 9)]["outcome_sleep_id"] is None


async def test_day_covariates_come_from_day_d(session_factory: SessionFactory) -> None:
    # Morning of Jun 10 opens cycle 500, which covers Jun 10. Its strain belongs to Jun 10.
    await add_night(
        session_factory, 1, "2026-06-10T03:00:00.000Z", "2026-06-10T11:00:00.000Z", cycle_id=500
    )
    await add_night(
        session_factory, 2, "2026-06-11T03:00:00.000Z", "2026-06-11T11:00:00.000Z", cycle_id=501
    )
    async with session_factory() as session, session.begin():
        cycle = payloads.cycle(500)
        cycle["score"]["strain"] = 14.5
        await upsert.upsert_cycle(session, cycle)
        open_cycle = payloads.cycle(501, score_state="PENDING_SCORE")
        del open_cycle["score"]
        await upsert.upsert_cycle(session, open_cycle)
        # A workout at 18:00 local on Jun 10, and a tag for the same day.
        await upsert.upsert_workout(
            session,
            payloads.workout(start="2026-06-10T22:00:00.000Z", end="2026-06-10T23:00:00.000Z"),
        )
        await session.execute(
            text(
                "INSERT INTO daily_tags (local_date, key, value) "
                "VALUES ('2026-06-10', 'alcohol', 'true')"
            )
        )

    rows = await outcomes(session_factory)
    day = rows[date(2026, 6, 10)]
    assert day["day_cycle_id"] == 500
    assert day["day_strain"] == 14.5
    assert day["workout_count"] == 1
    assert day["workout_minutes"] == pytest.approx(60.0)
    assert day["tags"] == {"alcohol": "true"}
    assert rows[date(2026, 6, 9)]["day_strain"] is None
    assert rows[date(2026, 6, 9)]["workout_count"] == 0
    assert rows[date(2026, 6, 11)]["day_strain"] is None  # cycle 501 is not scored yet


async def test_timezone_comes_from_app_config(session_factory: SessionFactory) -> None:
    # Ends 03:30 UTC Jun 10: Jun 9 in New York, Jun 10 in London.
    await add_night(session_factory, 1, "2026-06-09T20:00:00.000Z", "2026-06-10T03:30:00.000Z")
    assert date(2026, 6, 8) in await outcomes(session_factory)
    async with session_factory() as session, session.begin():
        await session.execute(text("UPDATE app_config SET local_timezone = 'Europe/London'"))
    rows = await outcomes(session_factory)
    assert rows[date(2026, 6, 9)]["outcome_sleep_id"] is not None
    assert date(2026, 6, 8) not in rows


async def test_one_row_per_date_with_no_gaps(session_factory: SessionFactory) -> None:
    await add_night(session_factory, 1, "2026-06-10T03:00:00.000Z", "2026-06-10T11:00:00.000Z")
    await add_night(session_factory, 2, "2026-06-14T03:00:00.000Z", "2026-06-14T11:00:00.000Z")
    rows = await outcomes(session_factory)
    dates = sorted(rows)
    assert dates[0] == date(2026, 6, 9)
    assert all((b - a).days == 1 for a, b in itertools.pairwise(dates))
    assert rows[date(2026, 6, 11)]["outcome_sleep_id"] is None
