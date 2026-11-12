"""Experiment lifecycle: draft, lock, assign, comply.

Rules enforced here (and again by database triggers):
- a locked experiment is frozen; only its status changes
- assignments are created once, for tomorrow, and never regenerated
- nothing here ends an experiment early or looks at results
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from whoop_lab.experiments.outcomes import OUTCOMES, default_analysis_spec
from whoop_lab.experiments.randomize import arm_for, stratum_for
from whoop_lab.models.experiments import (
    ARM_TREATMENT,
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    STATUS_DRAFT,
    Assignment,
    Experiment,
)

FROZEN_AFTER_LOCK = frozenset(
    {
        "name",
        "hypothesis",
        "intervention",
        "control",
        "outcome_metric",
        "analysis_spec",
        "block_size",
        "washout_days",
        "planned_days",
        "stratify_weekend",
        "started_on",
        "locked_at",
    }
)


class ExperimentError(Exception):
    """A request the rules do not allow. The message is safe to show."""


class LockedError(ExperimentError):
    pass


@dataclass(frozen=True)
class Draft:
    name: str
    hypothesis: str
    intervention: str
    control: str
    planned_days: int
    outcome_metric: str = "ln_hrv_rmssd"
    block_size: int = 4
    washout_days: int = 0
    stratify_weekend: bool = False


def _validate(values: dict[str, Any]) -> None:
    for text_field in ("name", "hypothesis", "intervention", "control"):
        if text_field in values and not str(values[text_field]).strip():
            raise ExperimentError(f"{text_field} is required")
    if "outcome_metric" in values and values["outcome_metric"] not in OUTCOMES:
        raise ExperimentError(f"unknown outcome metric: {values['outcome_metric']}")
    if "block_size" in values and (values["block_size"] < 2 or values["block_size"] % 2):
        raise ExperimentError("block size must be an even number, 2 or more")
    if "planned_days" in values and values["planned_days"] < 1:
        raise ExperimentError("planned days must be at least 1")
    if "washout_days" in values and values["washout_days"] < 0:
        raise ExperimentError("washout days cannot be negative")


async def create_draft(session: AsyncSession, draft: Draft) -> Experiment:
    values = {
        "name": draft.name.strip(),
        "hypothesis": draft.hypothesis.strip(),
        "intervention": draft.intervention.strip(),
        "control": draft.control.strip(),
        "outcome_metric": draft.outcome_metric,
        "planned_days": draft.planned_days,
        "block_size": draft.block_size,
        "washout_days": draft.washout_days,
        "stratify_weekend": draft.stratify_weekend,
    }
    _validate(values)
    taken = await session.scalar(select(Experiment.id).where(Experiment.name == values["name"]))
    if taken is not None:
        raise ExperimentError(f"an experiment named {values['name']!r} already exists")
    experiment = Experiment(
        **values,
        # The analysis seed is fixed now, before any data exists.
        analysis_spec=default_analysis_spec(draft.outcome_metric, secrets.randbits(31)),
        status=STATUS_DRAFT,
    )
    session.add(experiment)
    await session.flush()
    return experiment


async def _get_for_update(session: AsyncSession, experiment_id: int) -> Experiment:
    experiment = (
        await session.execute(
            select(Experiment).where(Experiment.id == experiment_id).with_for_update()
        )
    ).scalar_one_or_none()
    if experiment is None:
        raise ExperimentError("no such experiment")
    return experiment


async def update_experiment(
    session: AsyncSession, experiment_id: int, **changes: Any
) -> Experiment:
    """Edit a draft. Refuses any frozen field once the experiment is locked."""
    experiment = await _get_for_update(session, experiment_id)
    unknown = set(changes) - FROZEN_AFTER_LOCK
    if unknown:
        raise ExperimentError(f"cannot set {sorted(unknown)}")
    if experiment.locked_at is not None:
        raise LockedError(
            f"experiment {experiment.name!r} is locked; {sorted(changes)} cannot change"
        )
    _validate(changes)
    for name, value in changes.items():
        setattr(experiment, name, value)
    await session.flush()
    return experiment


async def delete_draft(session: AsyncSession, experiment_id: int) -> None:
    experiment = await _get_for_update(session, experiment_id)
    if experiment.locked_at is not None:
        raise LockedError(f"experiment {experiment.name!r} is locked and cannot be deleted")
    await session.delete(experiment)
    await session.flush()


async def lock_experiment(session: AsyncSession, experiment_id: int, today: date) -> Experiment:
    """Freeze the spec and start the experiment. The first assigned day is tomorrow."""
    experiment = await _get_for_update(session, experiment_id)
    if experiment.locked_at is not None:
        raise LockedError(f"experiment {experiment.name!r} is already locked")
    active = await session.scalar(select(Experiment.name).where(Experiment.status == STATUS_ACTIVE))
    if active is not None:
        raise ExperimentError(f"{active!r} is still running; one experiment at a time")
    experiment.started_on = today + timedelta(days=1)
    experiment.status = STATUS_ACTIVE
    experiment.locked_at = datetime.now(UTC)
    await session.flush()
    return experiment


async def active_experiment(session: AsyncSession) -> Experiment | None:
    return (
        await session.execute(select(Experiment).where(Experiment.status == STATUS_ACTIVE))
    ).scalar_one_or_none()


async def assigned_count(session: AsyncSession, experiment_id: int) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(Assignment)
            .where(Assignment.experiment_id == experiment_id)
        )
    ).scalar_one()


async def get_assignment(session: AsyncSession, experiment_id: int, day: date) -> Assignment | None:
    return (
        await session.execute(
            select(Assignment).where(
                Assignment.experiment_id == experiment_id, Assignment.local_date == day
            )
        )
    ).scalar_one_or_none()


async def assign_day(
    session: AsyncSession, experiment_id: int, day: date
) -> tuple[Assignment | None, bool]:
    """Assign `day` if the design calls for it. Returns (assignment, created).

    Returns (existing, False) if the day is already assigned: an assignment is
    never redrawn. Returns (None, False) when the day gets no assignment: before
    the start, after planned_days are used up, or inside a washout window.
    """
    experiment = await _get_for_update(session, experiment_id)  # serialises concurrent callers
    if experiment.status != STATUS_ACTIVE or experiment.started_on is None:
        return None, False
    existing = await get_assignment(session, experiment_id, day)
    if existing is not None:
        return existing, False
    if day < experiment.started_on:
        return None, False
    if await assigned_count(session, experiment_id) >= experiment.planned_days:
        return None, False
    latest = await session.scalar(
        select(func.max(Assignment.local_date)).where(Assignment.experiment_id == experiment_id)
    )
    if latest is not None and day <= latest:
        return None, False  # never fill in a day that was skipped
    if experiment.washout_days > 0:
        recent_treatment = await session.scalar(
            select(Assignment.id)
            .where(
                Assignment.experiment_id == experiment_id,
                Assignment.arm == ARM_TREATMENT,
                Assignment.local_date >= day - timedelta(days=experiment.washout_days),
                Assignment.local_date < day,
            )
            .limit(1)
        )
        if recent_treatment is not None:
            return None, False

    stratum = stratum_for(day, stratify_weekend=experiment.stratify_weekend)
    last = (
        await session.execute(
            select(Assignment)
            .where(Assignment.experiment_id == experiment_id, Assignment.stratum == stratum)
            .order_by(Assignment.block_index.desc(), Assignment.block_position.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is None or last.block_position + 1 >= experiment.block_size:
        block_index = 0 if last is None else last.block_index + 1
        position = 0
        seed = secrets.randbits(62)  # a fresh seed per block, stored on its rows
    else:
        block_index, position, seed = last.block_index, last.block_position + 1, last.seed

    assignment = Assignment(
        experiment_id=experiment_id,
        local_date=day,
        arm=arm_for(seed, experiment.block_size, position),
        stratum=stratum,
        block_index=block_index,
        block_position=position,
        seed=seed,
    )
    session.add(assignment)
    await session.flush()
    return assignment, True


async def record_compliance(
    session: AsyncSession,
    experiment_id: int,
    day: date,
    *,
    complied: bool | None,
    note: str | None,
    today: date,
) -> Assignment:
    if day > today:
        raise ExperimentError("compliance can only be recorded for today or earlier")
    assignment = await get_assignment(session, experiment_id, day)
    if assignment is None:
        raise ExperimentError(f"no assignment on {day}")
    assignment.complied = complied
    assignment.note = (note or "").strip() or None
    await session.flush()
    return assignment


async def complete_if_finished(session: AsyncSession, experiment_id: int, today: date) -> bool:
    """Mark the experiment completed once every planned day is assigned and its
    last outcome morning has passed. Depends only on the calendar, never on results."""
    experiment = await _get_for_update(session, experiment_id)
    if experiment.status != STATUS_ACTIVE:
        return False
    if await assigned_count(session, experiment_id) < experiment.planned_days:
        return False
    last_day = await session.scalar(
        select(func.max(Assignment.local_date)).where(Assignment.experiment_id == experiment_id)
    )
    if last_day is None or today <= last_day + timedelta(days=1):
        return False
    experiment.status = STATUS_COMPLETED
    await session.flush()
    return True
