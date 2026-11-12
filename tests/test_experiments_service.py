from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from whoop_lab.db import SessionFactory
from whoop_lab.experiments import service
from whoop_lab.experiments.randomize import arm_for
from whoop_lab.experiments.service import Draft, ExperimentError, LockedError
from whoop_lab.models import Assignment, Experiment, ExperimentResult

TODAY = date(2026, 11, 9)  # a Monday
START = TODAY + timedelta(days=1)


def draft(**overrides: Any) -> Draft:
    values: dict[str, Any] = {
        "name": "Caffeine cutoff",
        "hypothesis": "A noon caffeine cutoff raises next-morning HRV.",
        "intervention": "Last caffeine by noon",
        "control": "Last caffeine by 4pm",
        "planned_days": 28,
    }
    values.update(overrides)
    return Draft(**values)


async def make_locked(session_factory: SessionFactory, **overrides: Any) -> int:
    async with session_factory() as session, session.begin():
        experiment = await service.create_draft(session, draft(**overrides))
        await service.lock_experiment(session, experiment.id, TODAY)
        return experiment.id


async def assign_range(session_factory: SessionFactory, experiment_id: int, days: int) -> None:
    for offset in range(days):
        async with session_factory() as session, session.begin():
            await service.assign_day(session, experiment_id, START + timedelta(days=offset))


async def assignments(session_factory: SessionFactory, experiment_id: int) -> list[Assignment]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(Assignment)
                    .where(Assignment.experiment_id == experiment_id)
                    .order_by(Assignment.local_date)
                )
            ).scalars()
        )


# --- drafts and locking -----------------------------------------------------------------


async def test_draft_gets_a_default_spec_with_a_stored_seed(
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session, session.begin():
        experiment = await service.create_draft(session, draft())
    spec = experiment.analysis_spec
    assert experiment.status == "draft"
    assert experiment.outcome_metric == "ln_hrv_rmssd"
    assert spec["primary_outcome"] == "ln_hrv_rmssd"
    assert spec["draws"] == 10_000
    assert isinstance(spec["seed"], int)
    assert "recovery_score" not in spec["secondary_outcomes"]


@pytest.mark.parametrize(
    "bad",
    [
        {"outcome_metric": "recovery_score"},
        {"block_size": 3},
        {"planned_days": 0},
        {"washout_days": -1},
        {"name": "  "},
    ],
)
async def test_invalid_drafts_are_rejected(
    session_factory: SessionFactory, bad: dict[str, Any]
) -> None:
    async with session_factory() as session, session.begin():
        with pytest.raises(ExperimentError):
            await service.create_draft(session, draft(**bad))


async def test_a_draft_can_be_edited_and_deleted(session_factory: SessionFactory) -> None:
    async with session_factory() as session, session.begin():
        experiment = await service.create_draft(session, draft())
        await service.update_experiment(session, experiment.id, planned_days=42)
        assert experiment.planned_days == 42
        await service.delete_draft(session, experiment.id)
    async with session_factory() as session:
        assert (await session.execute(select(Experiment))).first() is None


async def test_lock_starts_tomorrow(session_factory: SessionFactory) -> None:
    experiment_id = await make_locked(session_factory)
    async with session_factory() as session:
        experiment = await session.get_one(Experiment, experiment_id)
    assert experiment.status == "active"
    assert experiment.started_on == START
    assert experiment.locked_at is not None


@pytest.mark.parametrize(
    "change",
    [
        {"analysis_spec": {"primary_outcome": "resting_heart_rate"}},
        {"outcome_metric": "resting_heart_rate"},
        {"planned_days": 14},
        {"block_size": 2},
        {"washout_days": 2},
        {"hypothesis": "something else"},
    ],
)
async def test_application_refuses_changes_to_a_locked_experiment(
    session_factory: SessionFactory, change: dict[str, Any]
) -> None:
    experiment_id = await make_locked(session_factory)
    async with session_factory() as session, session.begin():
        with pytest.raises(LockedError):
            await service.update_experiment(session, experiment_id, **change)
    async with session_factory() as session, session.begin():
        with pytest.raises(LockedError):
            await service.delete_draft(session, experiment_id)
    async with session_factory() as session, session.begin():
        with pytest.raises(LockedError):
            await service.lock_experiment(session, experiment_id, TODAY)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE experiments SET analysis_spec = '{}'::jsonb",
        "UPDATE experiments SET outcome_metric = 'resting_heart_rate'",
        "UPDATE experiments SET planned_days = planned_days - 7",
        "UPDATE experiments SET block_size = 2",
        "UPDATE experiments SET washout_days = 1",
        "UPDATE experiments SET stratify_weekend = true",
        "UPDATE experiments SET locked_at = NULL",
        "UPDATE experiments SET started_on = started_on + 1",
        "DELETE FROM experiments",
    ],
)
async def test_database_refuses_changes_to_a_locked_experiment(
    session_factory: SessionFactory, sql: str
) -> None:
    await make_locked(session_factory)
    async with session_factory() as session:
        with pytest.raises(DBAPIError, match="locked"):
            await session.execute(text(sql))


async def test_status_of_a_locked_experiment_may_change(session_factory: SessionFactory) -> None:
    await make_locked(session_factory)
    async with session_factory() as session, session.begin():
        await session.execute(text("UPDATE experiments SET status = 'completed'"))


async def test_only_one_experiment_runs_at_a_time(session_factory: SessionFactory) -> None:
    await make_locked(session_factory)
    async with session_factory() as session, session.begin():
        second = await service.create_draft(session, draft(name="Screens"))
        with pytest.raises(ExperimentError, match="one experiment at a time"):
            await service.lock_experiment(session, second.id, TODAY)


# --- assignment ---------------------------------------------------------------------------


async def test_blocks_are_balanced_and_reproducible_from_stored_seeds(
    session_factory: SessionFactory,
) -> None:
    experiment_id = await make_locked(session_factory, planned_days=28)
    await assign_range(session_factory, experiment_id, 28)
    rows = await assignments(session_factory, experiment_id)

    assert len(rows) == 28
    assert [r.local_date for r in rows] == [START + timedelta(days=i) for i in range(28)]
    blocks: dict[int, list[Assignment]] = defaultdict(list)
    for row in rows:
        blocks[row.block_index].append(row)
    assert sorted(blocks) == list(range(7))
    for block in blocks.values():
        assert Counter(r.arm for r in block) == {"treatment": 2, "control": 2}
        assert [r.block_position for r in block] == [0, 1, 2, 3]
        assert len({r.seed for r in block}) == 1  # one seed per block
    assert len({b[0].seed for b in blocks.values()}) == 7  # a different seed for each block
    # The stored seeds alone reproduce the whole sequence.
    assert [arm_for(r.seed, 4, r.block_position) for r in rows] == [r.arm for r in rows]


async def test_assigning_a_day_twice_never_redraws(session_factory: SessionFactory) -> None:
    experiment_id = await make_locked(session_factory)
    async with session_factory() as session, session.begin():
        first, created = await service.assign_day(session, experiment_id, START)
    assert created
    assert first is not None
    for _ in range(5):
        async with session_factory() as session, session.begin():
            again, created = await service.assign_day(session, experiment_id, START)
        assert not created
        assert again is not None
        assert (again.id, again.arm, again.seed) == (first.id, first.arm, first.seed)
    assert len(await assignments(session_factory, experiment_id)) == 1


async def test_concurrent_assignment_creates_one_row(session_factory: SessionFactory) -> None:
    experiment_id = await make_locked(session_factory)

    async def attempt() -> bool:
        async with session_factory() as session, session.begin():
            return (await service.assign_day(session, experiment_id, START))[1]

    created = await asyncio.gather(*(attempt() for _ in range(6)))
    assert sum(created) == 1
    assert len(await assignments(session_factory, experiment_id)) == 1


async def test_no_assignment_before_start_or_after_planned_days(
    session_factory: SessionFactory,
) -> None:
    experiment_id = await make_locked(session_factory, planned_days=4)
    async with session_factory() as session, session.begin():
        assert await service.assign_day(session, experiment_id, TODAY) == (None, False)
    await assign_range(session_factory, experiment_id, 10)
    rows = await assignments(session_factory, experiment_id)
    assert len(rows) == 4
    assert Counter(r.arm for r in rows) == {"treatment": 2, "control": 2}


async def test_a_skipped_day_is_never_filled_in_later(session_factory: SessionFactory) -> None:
    experiment_id = await make_locked(session_factory)
    async with session_factory() as session, session.begin():
        await service.assign_day(session, experiment_id, START)
        await service.assign_day(session, experiment_id, START + timedelta(days=2))
    async with session_factory() as session, session.begin():
        missed = await service.assign_day(session, experiment_id, START + timedelta(days=1))
    assert missed == (None, False)
    rows = await assignments(session_factory, experiment_id)
    assert [r.block_position for r in rows] == [0, 1]  # block positions stay contiguous


async def test_washout_days_after_treatment_get_no_assignment(
    session_factory: SessionFactory,
) -> None:
    experiment_id = await make_locked(session_factory, planned_days=12, washout_days=2)
    await assign_range(session_factory, experiment_id, 60)
    rows = await assignments(session_factory, experiment_id)
    assert len(rows) == 12
    by_date = {r.local_date: r.arm for r in rows}
    for day, arm in by_date.items():
        if arm == "treatment":
            assert day + timedelta(days=1) not in by_date
            assert day + timedelta(days=2) not in by_date
    # Blocks are still complete and balanced even though the dates have gaps.
    blocks: dict[int, list[str]] = defaultdict(list)
    for row in rows:
        blocks[row.block_index].append(row.arm)
    assert all(Counter(b) == {"treatment": 2, "control": 2} for b in blocks.values())


async def test_weekend_stratification_keeps_separate_block_streams(
    session_factory: SessionFactory,
) -> None:
    experiment_id = await make_locked(session_factory, planned_days=56, stratify_weekend=True)
    await assign_range(session_factory, experiment_id, 56)
    rows = await assignments(session_factory, experiment_id)
    assert {r.stratum for r in rows} == {"weekday", "weekend"}
    for row in rows:
        assert row.stratum == ("weekend" if row.local_date.weekday() >= 5 else "weekday")
    blocks: dict[tuple[str, int], list[str]] = defaultdict(list)
    for row in rows:
        blocks[(row.stratum, row.block_index)].append(row.arm)
    for arms in blocks.values():
        if len(arms) == 4:
            assert Counter(arms) == {"treatment": 2, "control": 2}
    assert [arm_for(r.seed, 4, r.block_position) for r in rows] == [r.arm for r in rows]


# --- immutability -------------------------------------------------------------------------


async def test_compliance_and_note_can_change(session_factory: SessionFactory) -> None:
    experiment_id = await make_locked(session_factory)
    await assign_range(session_factory, experiment_id, 1)
    async with session_factory() as session, session.begin():
        await service.record_compliance(
            session, experiment_id, START, complied=False, note="  coffee at 3pm ", today=START
        )
    (row,) = await assignments(session_factory, experiment_id)
    assert (row.complied, row.note) == (False, "coffee at 3pm")
    async with session_factory() as session, session.begin():
        with pytest.raises(ExperimentError):
            await service.record_compliance(
                session, experiment_id, START, complied=True, note=None, today=TODAY
            )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE assignments SET arm = CASE arm WHEN 'control' THEN 'treatment' ELSE 'control' END",
        "UPDATE assignments SET local_date = local_date + 30",
        "UPDATE assignments SET seed = seed + 1",
        "UPDATE assignments SET block_index = block_index + 9",
        "UPDATE assignments SET block_position = block_position + 2",
        "UPDATE assignments SET stratum = 'weekend'",
        "UPDATE assignments SET experiment_id = experiment_id",  # no-op is fine, see below
        "DELETE FROM assignments",
    ],
)
async def test_database_refuses_changes_to_assignments(
    session_factory: SessionFactory, sql: str
) -> None:
    experiment_id = await make_locked(session_factory)
    await assign_range(session_factory, experiment_id, 2)
    before = [
        (r.arm, r.seed, r.local_date) for r in await assignments(session_factory, experiment_id)
    ]
    async with session_factory() as session:
        if "experiment_id = experiment_id" in sql:
            await session.execute(text(sql))  # changes nothing, so nothing to refuse
            await session.commit()
        else:
            with pytest.raises(DBAPIError, match=r"immutable|never deleted"):
                await session.execute(text(sql))
    after = [
        (r.arm, r.seed, r.local_date) for r in await assignments(session_factory, experiment_id)
    ]
    assert after == before


async def test_results_are_append_only(session_factory: SessionFactory) -> None:
    experiment_id = await make_locked(session_factory)
    async with session_factory() as session, session.begin():
        session.add(
            ExperimentResult(
                experiment_id=experiment_id,
                outcome_metric="ln_hrv_rmssd",
                method="itt_randomization",
                n_treatment=1,
                n_control=1,
                n_missing_treatment=0,
                n_missing_control=0,
                compromised=False,
                details={},
            )
        )
    for sql in ("UPDATE experiment_results SET effect = 1", "DELETE FROM experiment_results"):
        async with session_factory() as session:
            with pytest.raises(DBAPIError, match="append-only"):
                await session.execute(text(sql))


# --- completion ---------------------------------------------------------------------------


async def test_experiment_completes_on_the_calendar_only(session_factory: SessionFactory) -> None:
    experiment_id = await make_locked(session_factory, planned_days=4)
    await assign_range(session_factory, experiment_id, 3)
    last = START + timedelta(days=3)

    async def complete(today: date) -> bool:
        async with session_factory() as session, session.begin():
            return await service.complete_if_finished(session, experiment_id, today)

    assert await complete(last + timedelta(days=30)) is False  # a planned day is still unassigned
    await assign_range(session_factory, experiment_id, 4)
    assert await complete(last) is False
    assert await complete(last + timedelta(days=1)) is False  # the last outcome arrives today
    assert await complete(last + timedelta(days=2)) is True
    async with session_factory() as session:
        assert (await session.get_one(Experiment, experiment_id)).status == "completed"
    # A completed experiment gets no further assignments.
    async with session_factory() as session, session.begin():
        assert await service.assign_day(session, experiment_id, last + timedelta(days=3)) == (
            None,
            False,
        )
