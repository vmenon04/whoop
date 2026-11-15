"""Nightly metrics derived from the band's per-second records.

Each algorithm below states where it comes from and takes its numbers from
`Params`. Every one is a published or reference-project method, cited where it is
used; none is tuned to reproduce WHOOP's numbers. `METRICS_VERSION` names the whole
set: change any algorithm or default and it must change too, so results computed
under different definitions are never mixed. Nothing here is a WHOOP score, and
none of it is validated beyond what the `whoop-ble` skill records.

Pure functions: no database, no device.

Versions:
- ble-nightly-1: R-R cleaned with the 20% successive-difference rule, which breaks a
  run at every rejected beat; respiratory rate from 60 s FFT bins (1/min resolution).
- ble-nightly-2: R-R corrected with the Kubios-style threshold filter (Strong, 200 ms),
  the setting that agreed best with ECG for WHOOP during sleep; respiratory rate is
  the reference project's own function, unchanged.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from scipy.interpolate import CubicSpline

from whoop_lab.ble.decode import Biometric
from whoop_lab.ble.vendor.whoop5 import experimental

METRICS_VERSION = "ble-nightly-2"


@dataclass(frozen=True)
class Params:
    """Every tunable number, with the reason for its default."""

    # Sleep window. The band labels each second WAKE, STILL, SLEEP or UP. Same rule
    # as LeonardSEO/wearable5.0 `sleep_periods`: smooth, bridge short gaps, drop naps.
    sleep_smoothing_s: int = 300
    sleep_merge_gap_s: int = 600
    sleep_min_duration_s: int = 1200
    # A main sleep ends before this local hour, as in the daily_outcomes view.
    wake_cutoff_hour: int = 14

    # R-R range, as in LeonardSEO/wearable5.0 `rmssd_ms`. Outside it a run ends.
    rr_min_ms: int = 300
    rr_max_ms: int = 2000
    # Artifact correction in the style of Kubios threshold-based correction: each
    # interval is compared with the median of the intervals around it, and one that
    # differs by more than the threshold is replaced by cubic interpolation (as in the
    # `hrv` package's threshold filter, which follows Kubios). 200 ms is Kubios
    # "Strong" in the version Bellenger et al. tested: with WHOOP's own filter it gave
    # the best agreement with ECG during sleep (Sensors 2021;21:3571).
    rr_local_median_beats: int = 5
    rr_correction_threshold_ms: int = 200
    # A night with more than this share of corrected intervals is marked poor quality;
    # 5% is the limit the HRV literature uses for interpolated beats.
    rr_corrected_share_limit: float = 0.05
    # A beat was missed if the silence since the last one exceeds this many intervals,
    # plus an allowance because records carry whole-second timestamps.
    rr_gap_factor: float = 1.6
    rr_gap_allowance_ms: int = 1000
    # RMSSD needs enough valid beat-to-beat differences to mean anything.
    rmssd_min_differences: int = 100
    # The fixed window does not depend on sleep detection: 02:00 to 06:00 local.
    fixed_window_start: time = time(2, 0)
    fixed_window_end: time = time(6, 0)
    # Short windows for the median-of-windows figure (the usual 5-minute segment).
    segment_s: int = 300
    segment_min_differences: int = 100

    # Respiratory rate: wearable5.0 `experimental.respiratory_rate` with its own
    # defaults (60 s windows, 0.1-0.5 Hz, 0.005 Hz steps); see `respiratory_rate`.

    # SpO2 arrives in windows of about 30 s every 20 minutes. Valid seconds closer
    # together than this belong to one window.
    spo2_min_valid_seconds: int = 5
    spo2_window_gap_s: int = 60


DEFAULT_PARAMS = Params()


@dataclass(frozen=True)
class SleepPeriod:
    start_unix: int
    end_unix: int  # the last second asleep

    @property
    def duration_s(self) -> int:
        return self.end_unix - self.start_unix + 1


def sleep_periods(
    records: Sequence[Biometric], params: Params = DEFAULT_PARAMS
) -> list[SleepPeriod]:
    """Stretches the band itself calls SLEEP, smoothed and merged. `records` oldest first."""
    if not records:
        return []
    first = records[0].unix_s
    asleep = np.zeros(records[-1].unix_s - first + 1)
    for record in records:
        if record.band_state == "SLEEP":
            asleep[record.unix_s - first] = 1.0
    width = min(params.sleep_smoothing_s, len(asleep))
    smoothed = np.convolve(asleep, np.ones(width) / width, mode="same") > 0.5

    periods: list[SleepPeriod] = []
    edges = np.flatnonzero(np.diff(np.concatenate(([0], smoothed.astype(np.int8), [0]))))
    for start, stop in zip(edges[::2], edges[1::2], strict=True):
        period = SleepPeriod(first + int(start), first + int(stop) - 1)
        if periods and period.start_unix - periods[-1].end_unix - 1 <= params.sleep_merge_gap_s:
            periods[-1] = SleepPeriod(periods[-1].start_unix, period.end_unix)
        else:
            periods.append(period)
    return [p for p in periods if p.duration_s >= params.sleep_min_duration_s]


def main_sleep_by_wake_date(
    periods: Sequence[SleepPeriod], tz: ZoneInfo, params: Params = DEFAULT_PARAMS
) -> dict[date, SleepPeriod]:
    """The longest period ending on each local date before the wake cutoff: the same
    rule the daily_outcomes view applies to WHOOP's sleeps."""
    chosen: dict[date, SleepPeriod] = {}
    for period in periods:
        woke = datetime.fromtimestamp(period.end_unix, UTC).astimezone(tz)
        if woke.hour >= params.wake_cutoff_hour:
            continue
        best = chosen.get(woke.date())
        if best is None or period.duration_s > best.duration_s:
            chosen[woke.date()] = period
    return chosen


def rr_runs(
    records: Sequence[Biometric], params: Params = DEFAULT_PARAMS
) -> list[tuple[int, list[int]]]:
    """Runs of consecutive R-R intervals as (start second, intervals), uncorrected.

    A run ends wherever beat-to-beat adjacency can no longer be assumed: more time
    has passed since the last reported beat than one interval can explain (a beat
    went unreported), or an interval is outside the physiological range.
    Differences are only ever taken inside a run, never across a break.

    A second with no interval is not by itself a gap: below 60 beats a minute some
    seconds simply contain no beat.
    """
    runs: list[tuple[int, list[int]]] = []
    current: list[int] = []
    last_beat_second: int | None = None

    for record in records:
        if not record.rr_ms:
            continue
        if current and last_beat_second is not None:
            silent_ms = (record.unix_s - last_beat_second) * 1000
            # Records are stamped to the second, hence the allowance.
            if silent_ms > params.rr_gap_factor * current[-1] + params.rr_gap_allowance_ms:
                current = []
        last_beat_second = record.unix_s
        for interval in record.rr_ms:
            if not params.rr_min_ms <= interval <= params.rr_max_ms:
                current = []
                continue
            if not current:
                runs.append((record.unix_s, current))
            current.append(interval)
    return [(start, run) for start, run in runs if len(run) >= 2]


def correct_run(run: Sequence[int], params: Params = DEFAULT_PARAMS) -> tuple[list[float], int]:
    """Threshold-based artifact correction of one run: (corrected intervals, beats corrected).

    Each interval is compared with the median of the `rr_local_median_beats` intervals
    centred on it (fewer at the ends of the run). One that differs by more than
    `rr_correction_threshold_ms` is replaced by a cubic spline through the accepted
    intervals, by beat number. A flagged interval before the first or after the last
    accepted one cannot be interpolated and is dropped; it still counts as corrected.
    """
    values = np.asarray(run, dtype=float)
    half = params.rr_local_median_beats // 2
    medians = np.array(
        [np.median(values[max(0, i - half) : i + half + 1]) for i in range(len(values))]
    )
    flagged = np.abs(values - medians) > params.rr_correction_threshold_ms
    corrected = int(flagged.sum())
    if not corrected:
        return values.tolist(), 0
    accepted = np.flatnonzero(~flagged)
    if len(accepted) < 2:
        return [], len(values)
    inner = np.arange(accepted[0], accepted[-1] + 1)
    if len(accepted) >= 3:
        spline = CubicSpline(accepted, values[accepted])
        filled = np.where(flagged[inner], spline(inner), values[inner])
    else:
        filled = np.where(
            flagged[inner], np.interp(inner, accepted, values[accepted]), values[inner]
        )
    return [float(v) for v in filled], corrected


@dataclass(frozen=True)
class CleanRR:
    runs: list[tuple[int, list[float]]]
    intervals: int  # intervals in range, before correction
    corrected: int  # intervals replaced or dropped by the correction

    @property
    def corrected_share(self) -> float | None:
        return self.corrected / self.intervals if self.intervals else None


def clean_rr(records: Sequence[Biometric], params: Params = DEFAULT_PARAMS) -> CleanRR:
    """Runs of R-R intervals with artifacts corrected (see `rr_runs` and `correct_run`)."""
    runs: list[tuple[int, list[float]]] = []
    intervals = corrected = 0
    for start, run in rr_runs(records, params):
        values, count = correct_run(run, params)
        intervals += len(run)
        corrected += count
        if len(values) >= 2:
            runs.append((start, values))
    return CleanRR(runs, intervals, corrected)


def rmssd(runs: Sequence[tuple[int, Sequence[float]]], min_differences: int) -> float | None:
    """Root mean square of successive differences, in ms, over differences inside runs."""
    squares = [(b - a) ** 2 for _, run in runs for a, b in pairwise(run)]
    if len(squares) < min_differences:
        return None
    return math.sqrt(sum(squares) / len(squares))


def _between(records: Sequence[Biometric], start_unix: int, end_unix: int) -> list[Biometric]:
    return [r for r in records if start_unix <= r.unix_s <= end_unix]


def segment_rmssd_median(
    records: Sequence[Biometric], params: Params = DEFAULT_PARAMS
) -> tuple[float | None, int]:
    """Median RMSSD over fixed-length segments, and how many segments qualified."""
    segments: dict[int, list[Biometric]] = {}
    for record in records:
        segments.setdefault(record.unix_s // params.segment_s, []).append(record)
    values = [
        value
        for group in segments.values()
        if (value := rmssd(clean_rr(group, params).runs, params.segment_min_differences))
        is not None
    ]
    return (statistics.median(values) if values else None), len(values)


def respiratory_rate(records: Sequence[Biometric]) -> tuple[float | None, int]:
    """Breaths per minute (median over windows) and the number of windows with a value.

    The reference project's `respiratory_rate`, called unchanged on the reported R-R
    series in order: it keeps 300-2000 ms intervals and drops steps of 200 ms or more,
    cuts 60 s windows of beat time, and takes the peak of the f-squared-weighted
    spectrum every 0.005 Hz between 0.1 and 0.5 Hz (respiratory sinus arrhythmia).
    Its documentation notes a slight upward bias from missed beats.
    """
    series = [interval for record in records for interval in record.rr_ms]
    windows: list[dict[str, Any]] = experimental.respiratory_rate(series)
    rates = [float(w["breaths_per_min"]) for w in windows if w["breaths_per_min"] is not None]
    return (statistics.median(rates) if rates else None), len(rates)


def spo2_windows(records: Sequence[Biometric], params: Params = DEFAULT_PARAMS) -> list[float]:
    """One value per measurement window: the median of its valid seconds.

    The band reports blood oxygen in bursts (about 30 s every 20 minutes of sleep).
    Whether byte 82 is truly SpO2 is disputed between the reference projects.
    """
    windows: list[float] = []
    current: list[int] = []
    last_second: int | None = None

    def close() -> None:
        nonlocal current
        if len(current) >= params.spo2_min_valid_seconds:
            windows.append(float(statistics.median(current)))
        current = []

    for record in records:
        if record.spo2_percent is None:
            continue
        if last_second is not None and record.unix_s - last_second > params.spo2_window_gap_s:
            close()
        current.append(record.spo2_percent)
        last_second = record.unix_s
    close()
    return windows


@dataclass(frozen=True)
class NightMetrics:
    """Everything computed for the night that ends on `wake_date`."""

    wake_date: date
    metrics_version: str
    sleep_start_unix: int
    sleep_end_unix: int
    seconds_recorded: int
    rr_coverage: float | None  # reported intervals / beats implied by the heart rate
    rr_corrected_share: float | None  # intervals corrected / intervals in range
    rr_quality_ok: bool | None  # corrected share within `rr_corrected_share_limit`
    rmssd_sleep_ms: float | None  # over the detected sleep window
    rmssd_fixed_ms: float | None  # over the fixed local window
    rmssd_segment_median_ms: float | None
    rmssd_segments: int
    resting_hr_median: float | None  # median heart rate over the sleep window
    resting_hr_lowest_segment: float | None  # lowest mean over one segment
    respiratory_rate: float | None
    respiratory_windows: int
    spo2_mean: float | None
    spo2_windows: int
    skin_temp_mean_c: float | None


def _fixed_window(wake_date: date, tz: ZoneInfo, params: Params) -> tuple[int, int]:
    start = datetime.combine(wake_date, params.fixed_window_start, tz)
    end = datetime.combine(wake_date, params.fixed_window_end, tz)
    if end <= start:  # a window that starts the evening before
        start -= timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp()) - 1


def night_metrics(
    records: Sequence[Biometric],
    wake_date: date,
    period: SleepPeriod,
    tz: ZoneInfo,
    params: Params = DEFAULT_PARAMS,
) -> NightMetrics:
    asleep = _between(records, period.start_unix, period.end_unix)
    rates = [r.heart_rate for r in asleep if r.heart_rate > 0]
    expected_beats = sum(rates) / 60.0
    reported = sum(len(r.rr_ms) for r in asleep)

    by_segment: dict[int, list[int]] = {}
    for record in asleep:
        if record.heart_rate > 0:
            by_segment.setdefault(record.unix_s // params.segment_s, []).append(record.heart_rate)
    full = [statistics.fmean(v) for v in by_segment.values() if len(v) >= params.segment_s // 2]

    segment_median, segments = segment_rmssd_median(asleep, params)
    breaths, breath_windows = respiratory_rate(asleep)
    cleaned = clean_rr(asleep, params)
    share = cleaned.corrected_share
    oxygen = spo2_windows(asleep, params)
    skin = [r.skin_temp_c for r in asleep if r.skin_temp_c is not None]
    fixed = _between(records, *_fixed_window(wake_date, tz, params))
    return NightMetrics(
        wake_date=wake_date,
        metrics_version=METRICS_VERSION,
        sleep_start_unix=period.start_unix,
        sleep_end_unix=period.end_unix,
        seconds_recorded=len(asleep),
        rr_coverage=reported / expected_beats if expected_beats else None,
        rr_corrected_share=share,
        rr_quality_ok=None if share is None else share <= params.rr_corrected_share_limit,
        rmssd_sleep_ms=rmssd(cleaned.runs, params.rmssd_min_differences),
        rmssd_fixed_ms=rmssd(clean_rr(fixed, params).runs, params.rmssd_min_differences),
        rmssd_segment_median_ms=segment_median,
        rmssd_segments=segments,
        resting_hr_median=float(statistics.median(rates)) if rates else None,
        resting_hr_lowest_segment=min(full) if full else None,
        respiratory_rate=breaths,
        respiratory_windows=breath_windows,
        spo2_mean=statistics.fmean(oxygen) if oxygen else None,
        spo2_windows=len(oxygen),
        skin_temp_mean_c=statistics.fmean(skin) if skin else None,
    )


def nightly(
    records: Sequence[Biometric], tz: ZoneInfo, params: Params = DEFAULT_PARAMS
) -> list[NightMetrics]:
    """Metrics for every night with a main sleep in `records` (oldest first)."""
    chosen = main_sleep_by_wake_date(sleep_periods(records, params), tz, params)
    return [
        night_metrics(records, wake_date, period, tz, params)
        for wake_date, period in sorted(chosen.items())
    ]
