"""Build a small simulated world in the database: an experiment with assignments,
plus the sleeps, recoveries and cycles WHOOP would have produced."""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import numpy as np
from sqlalchemy import select

from tests import payloads
from whoop_lab.db import SessionFactory
from whoop_lab.experiments import service
from whoop_lab.experiments.service import Draft
from whoop_lab.ingest import upsert
from whoop_lab.models import Assignment

TODAY = date(2026, 6, 1)  # a Monday, far from any DST change
START = TODAY + timedelta(days=1)
BASE_HRV = 50.0


def uid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def night_number(day: date) -> int:
    return day.toordinal() - date(2026, 1, 1).toordinal()


def sleep_id_for(day: date) -> str:
    """The main sleep whose recovery is the outcome for `day` (it ends the next morning)."""
    return uid(night_number(day))


async def lock_and_assign(
    session_factory: SessionFactory,
    *,
    planned_days: int = 28,
    calendar_days: int | None = None,
    **draft: Any,
) -> int:
    """`calendar_days`: how many evenings to run, when washout spreads the days out."""
    values: dict[str, Any] = {
        "name": "Caffeine cutoff",
        "hypothesis": "A noon caffeine cutoff raises next-morning HRV.",
        "intervention": "Last caffeine by noon",
        "control": "Last caffeine by 4pm",
        "planned_days": planned_days,
    }
    values.update(draft)
    async with session_factory() as session, session.begin():
        experiment = await service.create_draft(session, Draft(**values))
        await service.lock_experiment(session, experiment.id, TODAY)
        experiment_id = experiment.id
    for offset in range(calendar_days or planned_days):
        async with session_factory() as session, session.begin():
            await service.assign_day(session, experiment_id, START + timedelta(days=offset))
    return experiment_id


async def arms(session_factory: SessionFactory, experiment_id: int) -> dict[date, str]:
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Assignment).where(Assignment.experiment_id == experiment_id)
            )
        ).scalars()
        return {row.local_date: row.arm for row in rows}


async def add_outcomes(
    session_factory: SessionFactory,
    arm_by_day: dict[date, str],
    *,
    effect: float = 0.0,
    sigma: float = 0.05,
    baseline_days: int = 10,
    last_day: date | None = None,
    missing: frozenset[date] = frozenset(),
    pending: frozenset[date] = frozenset(),
    seed: int = 0,
) -> dict[date, float]:
    """Create one night per day from `baseline_days` before START to `last_day`.

    Returns ln(HRV) by local date for the days that got a scored recovery.
    """
    rng = np.random.default_rng(seed)
    end_day = last_day or max(arm_by_day)
    day = START - timedelta(days=baseline_days)
    outcomes: dict[date, float] = {}
    async with session_factory() as session, session.begin():
        while day <= end_day:
            wake = day + timedelta(days=1)
            ln_hrv = math.log(BASE_HRV) + float(rng.normal(0, sigma))
            if arm_by_day.get(day) == "treatment":
                ln_hrv += effect
            sleep_id = sleep_id_for(day)
            cycle_id = 5000 + night_number(wake)  # the cycle that covers the wake day
            await upsert.upsert_sleep(
                session,
                payloads.sleep(
                    sleep_id,
                    start=f"{wake.isoformat()}T03:00:00.000Z",
                    end=f"{wake.isoformat()}T11:00:00.000Z",
                    cycle_id=cycle_id,
                ),
            )
            cycle = payloads.cycle(cycle_id)
            cycle["score"]["strain"] = float(rng.uniform(6, 16))
            await upsert.upsert_cycle(session, cycle)
            if day in missing:
                pass
            elif day in pending:
                record = payloads.recovery(sleep_id, cycle_id=cycle_id, score_state="PENDING_SCORE")
                del record["score"]
                await upsert.upsert_recovery(session, record)
            else:
                record = payloads.recovery(sleep_id, cycle_id=cycle_id)
                record["score"]["hrv_rmssd_milli"] = math.exp(ln_hrv)
                await upsert.upsert_recovery(session, record)
                outcomes[day] = ln_hrv
            day += timedelta(days=1)
    return outcomes
