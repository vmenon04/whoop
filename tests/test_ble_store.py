"""Storing nightly Bluetooth metrics, and choosing captures to import. Synthetic data only."""

from __future__ import annotations

import dataclasses
import os
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from whoop_lab.ble.capture import NOTIFICATIONS_FILE
from whoop_lab.ble.metrics import METRICS_VERSION, NightMetrics
from whoop_lab.ble.store import capture_time, recent_captures, store_nights
from whoop_lab.db import SessionFactory
from whoop_lab.models import BleNight

NY = ZoneInfo("America/New_York")


def night(seconds: int = 25_000, rmssd: float | None = 60.0) -> NightMetrics:
    start = int(datetime(2026, 6, 10, 4, 0, tzinfo=UTC).timestamp())
    return NightMetrics(
        wake_date=date(2026, 6, 10),
        metrics_version=METRICS_VERSION,
        sleep_start_unix=start,
        sleep_end_unix=start + seconds - 1,
        seconds_recorded=seconds,
        rr_coverage=0.95,
        rr_corrected_share=0.02,
        rr_quality_ok=True,
        rmssd_sleep_ms=rmssd,
        rmssd_fixed_ms=rmssd,
        rmssd_segment_median_ms=rmssd,
        rmssd_segments=80,
        resting_hr_median=50.0,
        resting_hr_lowest_segment=47.0,
        respiratory_rate=15.0,
        respiratory_windows=300,
        spo2_mean=None,
        spo2_windows=0,
        skin_temp_mean_c=33.5,
    )


async def stored(session_factory: SessionFactory) -> list[BleNight]:
    async with session_factory() as session:
        return list((await session.scalars(select(BleNight))).all())


async def test_a_night_is_stored_with_its_versions(session_factory: SessionFactory) -> None:
    assert await store_nights(session_factory, [night()], "decoder@1") == 1
    (row,) = await stored(session_factory)
    assert row.wake_date == date(2026, 6, 10)
    assert row.metrics_version == METRICS_VERSION
    assert row.decoder_version == "decoder@1"
    assert row.sleep_start == datetime(2026, 6, 10, 4, 0, tzinfo=UTC)
    assert row.rmssd_sleep_ms == 60.0
    assert row.rr_quality_ok is True


async def test_fuller_data_replaces_and_partial_data_does_not(
    session_factory: SessionFactory,
) -> None:
    await store_nights(session_factory, [night(25_000, 60.0)], "decoder@1")
    assert await store_nights(session_factory, [night(9_000, 40.0)], "decoder@1") == 0
    assert (await stored(session_factory))[0].rmssd_sleep_ms == 60.0
    assert await store_nights(session_factory, [night(26_000, 61.0)], "decoder@1") == 1
    (row,) = await stored(session_factory)
    assert (row.seconds_recorded, row.rmssd_sleep_ms) == (26_000, 61.0)


async def test_another_metrics_version_is_a_separate_row(session_factory: SessionFactory) -> None:
    await store_nights(session_factory, [night()], "decoder@1")
    older = dataclasses.replace(night(), metrics_version="ble-nightly-1")
    await store_nights(session_factory, [older], "decoder@1")
    assert {row.metrics_version for row in await stored(session_factory)} == {
        METRICS_VERSION,
        "ble-nightly-1",
    }


async def test_nothing_to_store(session_factory: SessionFactory) -> None:
    assert await store_nights(session_factory, [], "decoder@1") == 0


def make_capture(root: Path, name: str) -> Path:
    directory = root / name
    directory.mkdir()
    (directory / NOTIFICATIONS_FILE).write_text("")
    return directory


def test_capture_time_is_read_from_the_name_in_local_time(tmp_path: Path) -> None:
    directory = make_capture(tmp_path, "20261115-102453-drain")
    assert capture_time(directory, NY) == datetime(2026, 11, 15, 10, 24, 53, tzinfo=NY)


def test_recent_captures_are_filtered_and_ordered(tmp_path: Path) -> None:
    make_capture(tmp_path, "20261115-102841-drain")
    make_capture(tmp_path, "20261113-103859-drain")
    make_capture(tmp_path, "20261101-090000-peek")
    unnamed = make_capture(tmp_path, "manual")
    old = datetime(2026, 10, 1, tzinfo=UTC).timestamp()
    os.utime(unnamed, (old, old))
    (tmp_path / "20261114-000000-empty").mkdir()  # no notifications file: ignored

    since = datetime(2026, 11, 12, tzinfo=NY)
    assert [p.parent.name for p in recent_captures(tmp_path, since, NY)] == [
        "20261113-103859-drain",
        "20261115-102841-drain",
    ]
    assert [p.parent.name for p in recent_captures(tmp_path, None, NY)] == [
        "manual",
        "20261101-090000-peek",
        "20261113-103859-drain",
        "20261115-102841-drain",
    ]
