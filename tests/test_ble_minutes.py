"""Per-minute summaries on synthetic records. No real data."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from whoop_lab.ble.decode import Biometric
from whoop_lab.ble.minutes import MINUTES_VERSION, counter_increases, minutes
from whoop_lab.ble.store import store_minutes
from whoop_lab.db import SessionFactory
from whoop_lab.models import BleMinute

START = 1_790_000_040 - 1_790_000_040 % 60  # a whole minute


def rec(
    t: int,
    *,
    hr: int = 60,
    rr: tuple[int, ...] = (),
    state: str = "WAKE",
    activity: str = "still",
    steps: int | None = 0,
    hw: int | None = 0,
    accel: float | None = 0.01,
    skin: float | None = 33.0,
) -> Biometric:
    return Biometric(t, t, hr, rr, state, skin, None, activity, accel, steps, hw)


def test_counter_increases_ignore_resets_and_gaps() -> None:
    records = [
        rec(START, steps=100),
        rec(START + 1, steps=102),
        rec(START + 2, steps=0),  # reset: adds nothing
        rec(START + 3, steps=3),
        rec(START + 30, steps=50),  # 27 s gap: the change is not attributed
        rec(START + 31, steps=51),
        rec(START + 32, steps=None),
        rec(START + 33, steps=60),  # unknown before: adds nothing
    ]
    assert counter_increases(records, "step_counter") == [0, 2, 0, 3, 0, 1, 0, 0]


def test_a_minute_summarises_its_seconds() -> None:
    records = [
        *(rec(START + s, hr=60 + s % 3, steps=s, activity="walk") for s in range(30)),
        *(rec(START + s, hr=0, rr=(1000,), state="SLEEP", steps=29) for s in range(30, 60)),
    ]
    (minute,) = minutes(records)
    assert minute.start_unix == START
    assert minute.minutes_version == MINUTES_VERSION
    assert minute.seconds_recorded == 60
    assert minute.seconds_with_hr == 30
    assert minute.hr_min == 60
    assert minute.hr_max == 62
    assert minute.hr_mean == 61.0
    assert minute.rr_intervals == 30
    assert minute.seconds_asleep == 30
    assert (minute.seconds_still, minute.seconds_walk, minute.seconds_run) == (30, 30, 0)
    assert minute.steps == 29


def test_a_step_across_the_minute_boundary_counts_in_the_new_minute() -> None:
    records = [rec(START + 59, steps=10), rec(START + 60, steps=12)]
    first, second = minutes(records)
    assert (first.steps, second.steps) == (0, 2)
    assert second.start_unix == START + 60


def test_a_minute_without_readings_has_no_values() -> None:
    (minute,) = minutes([rec(START, hr=0, accel=None, skin=None, state="")])
    assert minute.hr_mean is None
    assert minute.band_state is None
    assert minute.accel_mean_g is None
    assert minute.skin_temp_c is None


def test_no_records_no_minutes() -> None:
    assert minutes([]) == []


async def test_minutes_are_stored_and_partial_data_does_not_overwrite(
    session_factory: SessionFactory,
) -> None:
    full = minutes([rec(START + s, hr=70) for s in range(60)])
    partial = minutes([rec(START + s, hr=90) for s in range(10)])
    assert await store_minutes(session_factory, full, "decoder@1") == 1
    assert await store_minutes(session_factory, partial, "decoder@1") == 0
    async with session_factory() as session:
        (row,) = (await session.scalars(select(BleMinute))).all()
    assert row.minute == datetime.fromtimestamp(START, UTC)
    assert (row.hr_mean, row.seconds_recorded, row.decoder_version) == (70.0, 60, "decoder@1")


async def test_many_minutes_are_stored_in_chunks(session_factory: SessionFactory) -> None:
    day = minutes([rec(START + 60 * m) for m in range(2500)])
    assert await store_minutes(session_factory, day, "decoder@1") == 2500
