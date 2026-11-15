"""Per-minute summaries of the band's per-second records, for the whole day.

Only plain aggregation: means, extremes, counts and counter increases. Nothing here
estimates HRV, breathing or blood oxygen; those stay nightly (see `metrics`), because
the band reports few beat-to-beat intervals while awake and measures SpO2 only asleep.
`MINUTES_VERSION` names the aggregation; change any rule and it must change too.

Pure functions: no database, no device.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from whoop_lab.ble.decode import Biometric

MINUTES_VERSION = "ble-minutes-1"

# A counter increase is counted only between records at most this far apart, so steps
# are never attributed across a recording gap.
MAX_COUNTER_GAP_S = 5
ACTIVITIES = ("still", "walk", "run")


@dataclass(frozen=True)
class Minute:
    """One minute of records; `start_unix` is a multiple of 60."""

    start_unix: int
    minutes_version: str
    seconds_recorded: int
    seconds_with_hr: int
    hr_mean: float | None
    hr_min: int | None
    hr_max: int | None
    rr_intervals: int
    band_state: str | None  # the most frequent state in the minute
    seconds_asleep: int
    seconds_still: int
    seconds_walk: int
    seconds_run: int
    steps: int  # increases of `step_counter`
    steps_hw: int  # increases of `hw_step_counter`
    accel_mean_g: float | None
    skin_temp_c: float | None


def counter_increases(
    records: Sequence[Biometric], field: str, max_gap_s: int = MAX_COUNTER_GAP_S
) -> list[int]:
    """Per record, how much the counter rose since the record before it.

    A drop is a reset (the band restarts its counters, for example when a client
    connects), so it adds nothing. So does any change across a gap in the records.
    """
    increases = [0] * len(records)
    for index in range(1, len(records)):
        before, now = records[index - 1], records[index]
        old, new = getattr(before, field), getattr(now, field)
        if old is None or new is None or now.unix_s - before.unix_s > max_gap_s:
            continue
        increases[index] = max(0, new - old)
    return increases


def minutes(records: Sequence[Biometric]) -> list[Minute]:
    """Every minute that holds at least one record. `records` oldest first, each once."""
    steps = counter_increases(records, "step_counter")
    steps_hw = counter_increases(records, "hw_step_counter")
    groups: dict[int, list[int]] = {}
    for index, record in enumerate(records):
        groups.setdefault(record.unix_s - record.unix_s % 60, []).append(index)

    result: list[Minute] = []
    for start, indexes in sorted(groups.items()):
        rows = [records[i] for i in indexes]
        rates = [r.heart_rate for r in rows if r.heart_rate > 0]
        states = Counter(r.band_state for r in rows if r.band_state)
        activities = Counter(r.activity_class for r in rows)
        accel = [r.dynamic_accel_g for r in rows if r.dynamic_accel_g is not None]
        skin = [r.skin_temp_c for r in rows if r.skin_temp_c is not None]
        result.append(
            Minute(
                start_unix=start,
                minutes_version=MINUTES_VERSION,
                seconds_recorded=len(rows),
                seconds_with_hr=len(rates),
                hr_mean=statistics.fmean(rates) if rates else None,
                hr_min=min(rates) if rates else None,
                hr_max=max(rates) if rates else None,
                rr_intervals=sum(len(r.rr_ms) for r in rows),
                band_state=states.most_common(1)[0][0] if states else None,
                seconds_asleep=states["SLEEP"],
                seconds_still=activities["still"],
                seconds_walk=activities["walk"],
                seconds_run=activities["run"],
                steps=sum(steps[i] for i in indexes),
                steps_hw=sum(steps_hw[i] for i in indexes),
                accel_mean_g=statistics.fmean(accel) if accel else None,
                skin_temp_c=statistics.fmean(skin) if skin else None,
            )
        )
    return result
