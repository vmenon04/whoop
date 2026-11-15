"""Nightly metrics on synthetic records with known answers. No real data."""

from __future__ import annotations

import math
from datetime import date, datetime
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from whoop_lab.ble.decode import Biometric
from whoop_lab.ble.metrics import (
    DEFAULT_PARAMS,
    METRICS_VERSION,
    SleepPeriod,
    clean_rr,
    correct_run,
    main_sleep_by_wake_date,
    nightly,
    respiratory_rate,
    rmssd,
    rr_runs,
    sleep_periods,
    spo2_windows,
)

NY = ZoneInfo("America/New_York")


def at(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=NY).timestamp())


def second(
    unix_s: int,
    rr: tuple[int, ...] = (),
    *,
    hr: int = 60,
    state: str = "SLEEP",
    spo2: int | None = None,
    skin: float | None = 33.5,
) -> Biometric:
    return Biometric(unix_s, unix_s, hr, rr, state, skin, spo2)


def beats(start: int, intervals: list[int], **fields: object) -> list[Biometric]:
    """Per-second records carrying the given beat intervals at the times they would occur."""
    by_second: dict[int, list[int]] = {}
    elapsed = 0.0
    for interval in intervals:
        elapsed += interval / 1000.0
        by_second.setdefault(start + int(elapsed), []).append(interval)
    last = max(by_second)
    return [
        second(t, tuple(by_second.get(t, ())), **fields)  # type: ignore[arg-type]
        for t in range(start, last + 1)
    ]


# --- R-R cleaning and RMSSD ---------------------------------------------------------------


def test_rmssd_of_an_alternating_series_is_the_step() -> None:
    records = beats(1_790_000_000, [1000, 1040] * 200)
    cleaned = clean_rr(records)
    assert len(cleaned.runs) == 1
    assert cleaned.corrected == 0
    assert rmssd(cleaned.runs, 100) == pytest.approx(40.0)


def test_slow_heart_rates_do_not_break_a_run() -> None:
    # 1,500 ms intervals: every third second has no beat at all. That is not missing data.
    records = beats(1_790_000_000, [1500, 1520] * 100, hr=40)
    assert any(not r.rr_ms for r in records)
    runs = rr_runs(records)
    assert len(runs) == 1
    assert len(runs[0][1]) == 200


def test_an_ectopic_beat_is_replaced_by_interpolation() -> None:
    clean = [1000, 1010] * 100
    with_ectopic = [*clean[:100], 600, 1400, *clean[100:]]  # premature beat, then a pause
    cleaned = clean_rr(beats(1_790_000_000, with_ectopic))
    assert len(cleaned.runs) == 1  # corrected in place: the run is not broken
    assert cleaned.corrected == 2
    assert cleaned.corrected_share == pytest.approx(2 / 202)
    value = rmssd(cleaned.runs, 100)
    assert value == pytest.approx(10.0, abs=1.0)
    unfiltered = math.sqrt(
        sum((b - a) ** 2 for a, b in pairwise(with_ectopic)) / (len(with_ectopic) - 1)
    )
    assert unfiltered > 60  # what the two bad intervals would have done


def test_out_of_range_intervals_are_rejected() -> None:
    runs = rr_runs(beats(1_790_000_000, [1000, 1000, 250, 1000, 1000, 2500, 1000, 1000]))
    assert [run for _, run in runs] == [[1000, 1000], [1000, 1000], [1000, 1000]]


def test_a_stretch_of_unreported_beats_ends_the_run() -> None:
    first = beats(1_790_000_000, [1000, 1040] * 60)
    later = beats(first[-1].unix_s + 30, [1000, 1040] * 60)  # 30 s with nothing reported
    silence = [second(t) for t in range(first[-1].unix_s + 1, later[0].unix_s)]
    runs = clean_rr([*first, *silence, *later]).runs
    assert len(runs) == 2
    # The difference across the gap is never taken.
    assert rmssd(runs, 100) == pytest.approx(40.0)


def test_too_few_differences_gives_no_value() -> None:
    assert rmssd(clean_rr(beats(1_790_000_000, [1000, 1040] * 10)).runs, 100) is None


def test_large_real_variability_is_kept() -> None:
    # Smooth swings of 180 ms over a five-beat breath: no beat is more than 200 ms from
    # its neighbours' median, so nothing is corrected, although some successive steps
    # exceed 20% of the interval and the ble-nightly-1 rule would have dropped them.
    intervals = [round(900 + 180 * math.sin(2 * math.pi * i / 5)) for i in range(400)]
    assert any(abs(b - a) > 0.2 * a for a, b in pairwise(intervals))
    values, corrected = correct_run(intervals)
    assert corrected == 0
    assert values == intervals


def test_a_flagged_beat_at_the_edge_is_dropped() -> None:
    values, corrected = correct_run([1600, 1000, 1010, 1000, 1010, 1000])
    assert corrected == 1
    assert values == [1000, 1010, 1000, 1010, 1000]


# --- sleep windows ------------------------------------------------------------------------


def night(start: str, end: str, state: str = "SLEEP") -> list[Biometric]:
    return [second(t, state=state) for t in range(at(start), at(end))]


def test_sleep_period_follows_the_band_state_and_bridges_a_short_waking() -> None:
    records = [
        *night("2026-06-09 22:00", "2026-06-09 23:30", "WAKE"),
        *night("2026-06-09 23:30", "2026-06-10 03:00"),
        *night("2026-06-10 03:00", "2026-06-10 03:06", "WAKE"),  # six minutes awake
        *night("2026-06-10 03:06", "2026-06-10 07:00"),
        *night("2026-06-10 07:00", "2026-06-10 08:00", "UP"),
    ]
    (period,) = sleep_periods(records)
    assert period.start_unix == pytest.approx(at("2026-06-09 23:30"), abs=160)
    assert period.end_unix == pytest.approx(at("2026-06-10 07:00"), abs=160)


def test_a_short_doze_is_not_a_sleep_period() -> None:
    records = [
        *night("2026-06-10 13:00", "2026-06-10 14:00", "WAKE"),
        *night("2026-06-10 14:00", "2026-06-10 14:12"),
        *night("2026-06-10 14:12", "2026-06-10 15:00", "WAKE"),
    ]
    assert sleep_periods(records) == []


def test_main_sleep_is_the_longest_ending_before_the_cutoff_on_its_wake_date() -> None:
    long_night = SleepPeriod(at("2026-06-09 23:30"), at("2026-06-10 07:00"))
    early_fragment = SleepPeriod(at("2026-06-09 21:00"), at("2026-06-10 00:20"))
    afternoon = SleepPeriod(at("2026-06-10 15:00"), at("2026-06-10 19:00"))
    next_night = SleepPeriod(at("2026-06-11 00:10"), at("2026-06-11 06:00"))
    chosen = main_sleep_by_wake_date([early_fragment, long_night, afternoon, next_night], NY)
    assert chosen == {date(2026, 6, 10): long_night, date(2026, 6, 11): next_night}


# --- respiration, blood oxygen ------------------------------------------------------------


@pytest.mark.parametrize("breaths_per_minute", [10, 15, 19])
def test_respiratory_rate_is_recovered_from_sinus_arrhythmia(breaths_per_minute: int) -> None:
    intervals: list[int] = []
    elapsed = 0.0
    while elapsed < 900:
        interval = 1000 + 60 * math.sin(2 * math.pi * breaths_per_minute / 60 * elapsed)
        intervals.append(round(interval))
        elapsed += interval / 1000.0
    rate, windows = respiratory_rate(beats(1_790_000_000, intervals))
    assert windows >= 10
    assert rate == pytest.approx(breaths_per_minute, abs=1.0)


def test_respiratory_rate_needs_data() -> None:
    assert respiratory_rate([]) == (None, 0)


def test_spo2_is_one_median_per_measurement_window() -> None:
    start = 1_790_000_000
    records = [second(start + t) for t in range(0, 4000)]
    for offset, values in ((100, [96, 97, 97, 98, 97, 96, 97]), (1300, [94, 95, 95, 95, 99])):
        for i, value in enumerate(values):
            records[offset + 3 * i] = second(start + offset + 3 * i, spo2=value)
    records[2500] = second(start + 2500, spo2=90)  # three stray seconds: too few to count
    records[2501] = second(start + 2501, spo2=90)
    records[2502] = second(start + 2502, spo2=90)
    assert spo2_windows(records) == [97.0, 95.0]


# --- the whole night ----------------------------------------------------------------------


def test_nightly_metrics_for_a_synthetic_night() -> None:
    evening = night("2026-06-09 22:00", "2026-06-09 23:00", "WAKE")
    start = at("2026-06-09 23:00")
    asleep = beats(start, [1200, 1250] * 10_500, hr=49, state="SLEEP", skin=33.5)  # 7.1 hours
    morning = [second(t, state="UP") for t in range(asleep[-1].unix_s + 1, asleep[-1].unix_s + 900)]
    (result,) = nightly([*evening, *asleep, *morning], NY)

    assert result.wake_date == date(2026, 6, 10)
    assert result.metrics_version == METRICS_VERSION
    assert result.sleep_start_unix == pytest.approx(start, abs=160)
    assert result.sleep_end_unix == pytest.approx(asleep[-1].unix_s, abs=160)
    assert result.rmssd_sleep_ms == pytest.approx(50.0, abs=0.1)
    assert result.rmssd_fixed_ms == pytest.approx(50.0, abs=0.1)  # 02:00 to 06:00 is inside
    assert result.rmssd_segment_median_ms == pytest.approx(50.0, abs=0.1)
    assert result.rmssd_segments >= 80
    assert result.resting_hr_median == 49
    assert result.resting_hr_lowest_segment == pytest.approx(49.0)
    assert result.rr_coverage == pytest.approx(1.0, abs=0.02)
    assert result.rr_corrected_share == 0
    assert result.rr_quality_ok is True
    assert result.skin_temp_mean_c == pytest.approx(33.5)
    assert result.spo2_mean is None
    assert result.spo2_windows == 0


def test_no_sleep_means_no_nights() -> None:
    assert nightly(night("2026-06-10 09:00", "2026-06-10 12:00", "WAKE"), NY) == []
    assert nightly([], NY) == []


def test_defaults_are_the_documented_ones() -> None:
    """Changing a default changes what a metric means: bump METRICS_VERSION with it."""
    assert METRICS_VERSION == "ble-nightly-2"
    assert (DEFAULT_PARAMS.rr_min_ms, DEFAULT_PARAMS.rr_max_ms) == (300, 2000)
    assert DEFAULT_PARAMS.rr_local_median_beats == 5
    assert DEFAULT_PARAMS.rr_correction_threshold_ms == 200
    assert DEFAULT_PARAMS.rr_corrected_share_limit == 0.05
    assert (DEFAULT_PARAMS.fixed_window_start.hour, DEFAULT_PARAMS.fixed_window_end.hour) == (2, 6)
    assert DEFAULT_PARAMS.sleep_min_duration_s == 1200
    assert DEFAULT_PARAMS.wake_cutoff_hour == 14
