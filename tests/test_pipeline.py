from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pytest
import respx
from sqlalchemy import func, select, text

from tests.world import START, add_outcomes, arms, lock_and_assign
from whoop_lab.analysis.pipeline import (
    METHOD_ADJUSTED,
    METHOD_EXPLORATORY,
    METHOD_PER_PROTOCOL,
    METHOD_PRIMARY,
    ResultRow,
    analyze_experiment,
    describe,
    load_frame,
)
from whoop_lab.config import Settings
from whoop_lab.db import SessionFactory
from whoop_lab.experiments import service
from whoop_lab.experiments.jobs import assign_tomorrow, run_daily_analysis
from whoop_lab.models import Experiment, ExperimentResult
from whoop_lab.setup_db import apply_app_config

AFTER = START + timedelta(days=40)
NY = ZoneInfo("America/New_York")
NTFY = "https://ntfy.test/whoop-topic"


async def analyze(
    session_factory: SessionFactory, experiment_id: int, today: date = AFTER
) -> dict[tuple[str, str], ResultRow]:
    async with session_factory() as session, session.begin():
        experiment = await session.get_one(Experiment, experiment_id)
        rows = await analyze_experiment(session, experiment, today)
    return {(row.method, row.outcome_metric): row for row in rows}


async def test_primary_analysis_recovers_a_planted_effect(session_factory: SessionFactory) -> None:
    experiment_id = await lock_and_assign(session_factory)
    arm_by_day = await arms(session_factory, experiment_id)
    outcomes = await add_outcomes(session_factory, arm_by_day, effect=0.30, sigma=0.05)

    results = await analyze(session_factory, experiment_id)
    primary = results[(METHOD_PRIMARY, "ln_hrv_rmssd")]

    treated = [v for d, v in outcomes.items() if arm_by_day.get(d) == "treatment"]
    control = [v for d, v in outcomes.items() if arm_by_day.get(d) == "control"]
    assert primary.effect is not None
    assert primary.effect == pytest.approx(np.mean(treated) - np.mean(control), abs=1e-9)
    assert primary.effect == pytest.approx(0.30, abs=0.05)
    assert (primary.n_treatment, primary.n_control) == (14, 14)
    assert (primary.n_missing_treatment, primary.n_missing_control) == (0, 0)
    assert primary.p_value is not None
    assert primary.p_value < 0.001
    assert primary.ci_low is not None
    assert primary.ci_high is not None
    assert primary.ci_low < 0.30 < primary.ci_high
    assert primary.compromised is False
    assert primary.details["label"] == "primary"
    assert primary.details["draws"] == 10_000
    assert primary.details["percent_change"] == pytest.approx((np.exp(primary.effect) - 1) * 100)


async def test_stratified_washout_design_goes_through_the_whole_pipeline(
    session_factory: SessionFactory,
) -> None:
    experiment_id = await lock_and_assign(
        session_factory,
        planned_days=24,
        calendar_days=48,
        washout_days=1,
        stratify_weekend=True,
    )
    arm_by_day = await arms(session_factory, experiment_id)
    days = sorted(arm_by_day)
    assert [(d - START).days for d in days] == list(range(0, 48, 2))
    outcomes = await add_outcomes(
        session_factory, arm_by_day, effect=0.30, missing=frozenset(days[5:7])
    )

    primary = (await analyze(session_factory, experiment_id, START + timedelta(days=60)))[
        (METHOD_PRIMARY, "ln_hrv_rmssd")
    ]
    # Washout days have outcomes in the database but never enter the analysis.
    treated = [outcomes[d] for d in days if d in outcomes and arm_by_day[d] == "treatment"]
    control = [outcomes[d] for d in days if d in outcomes and arm_by_day[d] == "control"]
    assert primary.effect == pytest.approx(np.mean(treated) - np.mean(control), abs=1e-9)
    assert primary.n_treatment + primary.n_control == 22
    assert primary.n_missing_treatment + primary.n_missing_control == 2
    assert primary.details["days_due"] == 24
    assert primary.ci_low is not None
    assert primary.ci_high is not None
    assert primary.ci_low < 0.30 < primary.ci_high
    assert primary.p_value is not None
    assert primary.p_value < 0.001


async def test_null_data_gives_an_interval_around_zero(session_factory: SessionFactory) -> None:
    experiment_id = await lock_and_assign(session_factory)
    await add_outcomes(session_factory, await arms(session_factory, experiment_id), effect=0.0)
    primary = (await analyze(session_factory, experiment_id))[(METHOD_PRIMARY, "ln_hrv_rmssd")]
    assert primary.ci_low is not None
    assert primary.ci_high is not None
    assert primary.ci_low < 0 < primary.ci_high


async def test_every_method_is_reported_and_labelled(session_factory: SessionFactory) -> None:
    experiment_id = await lock_and_assign(session_factory)
    await add_outcomes(session_factory, await arms(session_factory, experiment_id), effect=0.2)
    results = await analyze(session_factory, experiment_id)

    assert set(results) == {
        (METHOD_PRIMARY, "ln_hrv_rmssd"),
        (METHOD_ADJUSTED, "ln_hrv_rmssd"),
        (METHOD_PER_PROTOCOL, "ln_hrv_rmssd"),
        (METHOD_EXPLORATORY, "resting_heart_rate"),
        (METHOD_EXPLORATORY, "sleep_efficiency_percentage"),
        (METHOD_EXPLORATORY, "deep_sleep_minutes"),
    }
    adjusted = results[(METHOD_ADJUSTED, "ln_hrv_rmssd")]
    assert adjusted.effect == pytest.approx(0.2, abs=0.06)
    assert adjusted.details["standard_errors"] == "HC3"
    assert adjusted.details["quantiles"] == "t"
    assert adjusted.details["ignores_blocks"] is True
    assert adjusted.effect is not None
    assert adjusted.ci_low is not None
    assert adjusted.ci_high is not None
    assert adjusted.ci_low < adjusted.effect < adjusted.ci_high
    assert "after assignment" in adjusted.details["caveat"]
    assert "biased" in results[(METHOD_PER_PROTOCOL, "ln_hrv_rmssd")].details["label"]
    assert "exploratory" in results[(METHOD_EXPLORATORY, "resting_heart_rate")].details["label"]


async def test_intention_to_treat_ignores_compliance(session_factory: SessionFactory) -> None:
    experiment_id = await lock_and_assign(session_factory)
    arm_by_day = await arms(session_factory, experiment_id)
    await add_outcomes(session_factory, arm_by_day, effect=0.2)
    before = (await analyze(session_factory, experiment_id))[(METHOD_PRIMARY, "ln_hrv_rmssd")]

    days = sorted(arm_by_day)
    async with session_factory() as session, session.begin():
        for index, day in enumerate(days):
            await service.record_compliance(
                session, experiment_id, day, complied=index % 4 != 0, note=None, today=AFTER
            )
    results = await analyze(session_factory, experiment_id)
    after = results[(METHOD_PRIMARY, "ln_hrv_rmssd")]
    per_protocol = results[(METHOD_PER_PROTOCOL, "ln_hrv_rmssd")]

    assert (after.effect, after.p_value, after.n_treatment) == (
        before.effect,
        before.p_value,
        before.n_treatment,
    )
    assert per_protocol.n_treatment + per_protocol.n_control == 21
    assert per_protocol.details["days_not_complied"] == 7


async def test_missing_and_unscored_mornings_are_dropped_and_counted(
    session_factory: SessionFactory,
) -> None:
    experiment_id = await lock_and_assign(session_factory)
    arm_by_day = await arms(session_factory, experiment_id)
    days = sorted(arm_by_day)
    missing, pending = frozenset(days[3:5]), frozenset(days[10:11])
    outcomes = await add_outcomes(
        session_factory, arm_by_day, effect=0.2, missing=missing, pending=pending
    )
    primary = (await analyze(session_factory, experiment_id))[(METHOD_PRIMARY, "ln_hrv_rmssd")]

    assert primary.n_treatment + primary.n_control == 25
    assert primary.n_missing_treatment + primary.n_missing_control == 3
    expected_missing_t = sum(arm_by_day[d] == "treatment" for d in missing | pending)
    assert primary.n_missing_treatment == expected_missing_t
    treated = [v for d, v in outcomes.items() if arm_by_day.get(d) == "treatment"]
    control = [v for d, v in outcomes.items() if arm_by_day.get(d) == "control"]
    assert primary.effect == pytest.approx(np.mean(treated) - np.mean(control), abs=1e-9)
    assert primary.compromised is False  # 3 of 28 is under 20%
    assert "3 mornings missing" in describe(primary)


async def test_more_than_twenty_percent_missing_is_flagged(session_factory: SessionFactory) -> None:
    experiment_id = await lock_and_assign(session_factory)
    arm_by_day = await arms(session_factory, experiment_id)
    missing = frozenset(sorted(arm_by_day)[::4][:6])  # 6 of 28 = 21%
    await add_outcomes(session_factory, arm_by_day, missing=missing)
    primary = (await analyze(session_factory, experiment_id))[(METHOD_PRIMARY, "ln_hrv_rmssd")]
    assert primary.details["missing_fraction"] > 0.20
    assert primary.compromised is True
    assert "COMPROMISED" in describe(primary)


async def test_missingness_imbalance_between_arms_is_flagged(
    session_factory: SessionFactory,
) -> None:
    experiment_id = await lock_and_assign(session_factory)
    arm_by_day = await arms(session_factory, experiment_id)
    treated_days = [d for d in sorted(arm_by_day) if arm_by_day[d] == "treatment"]
    await add_outcomes(session_factory, arm_by_day, missing=frozenset(treated_days[:4]))
    primary = (await analyze(session_factory, experiment_id))[(METHOD_PRIMARY, "ln_hrv_rmssd")]
    assert (primary.n_missing_treatment, primary.n_missing_control) == (4, 0)
    assert primary.details["missing_fraction"] < 0.20
    assert primary.compromised is True


async def test_days_whose_morning_has_not_arrived_are_neither_analysed_nor_missing(
    session_factory: SessionFactory,
) -> None:
    experiment_id = await lock_and_assign(session_factory)
    arm_by_day = await arms(session_factory, experiment_id)
    today = START + timedelta(days=10)
    await add_outcomes(session_factory, arm_by_day, last_day=today - timedelta(days=1))
    primary = (await analyze(session_factory, experiment_id, today))[
        (METHOD_PRIMARY, "ln_hrv_rmssd")
    ]
    assert primary.details["days_due"] == 10
    assert primary.n_treatment + primary.n_control == 10
    assert primary.n_missing_treatment + primary.n_missing_control == 0


async def test_an_unscored_morning_is_not_missing_until_the_day_after(
    session_factory: SessionFactory,
) -> None:
    experiment_id = await lock_and_assign(session_factory)
    arm_by_day = await arms(session_factory, experiment_id)
    today = START + timedelta(days=10)
    yesterday = today - timedelta(days=1)
    await add_outcomes(
        session_factory, arm_by_day, last_day=yesterday, pending=frozenset({yesterday})
    )
    key = (METHOD_PRIMARY, "ln_hrv_rmssd")

    # This morning's recovery is still being scored: yesterday waits, uncounted.
    early = (await analyze(session_factory, experiment_id, today))[key]
    assert early.details["days_due"] == 9
    assert early.n_missing_treatment + early.n_missing_control == 0

    # A day later it has had its whole morning and counts as missing.
    late = (await analyze(session_factory, experiment_id, today + timedelta(days=1)))[key]
    assert late.details["days_due"] == 10
    assert late.n_missing_treatment + late.n_missing_control == 1


async def test_skipped_evenings_are_recorded_and_washout_days_are_not(
    session_factory: SessionFactory,
) -> None:
    key = (METHOD_PRIMARY, "ln_hrv_rmssd")
    experiment_id = await lock_and_assign(
        session_factory, planned_days=8, calendar_days=24, washout_days=2
    )
    await add_outcomes(session_factory, await arms(session_factory, experiment_id))
    complete = (await analyze(session_factory, experiment_id))[key]
    assert complete.details["days_skipped"] == 0

    async with session_factory() as session, session.begin():
        await session.execute(text("TRUNCATE assignments"))  # skips the row triggers
    # The first evening and one later evening are missed.
    for offset in (1, 4, 7, 11, 14):
        async with session_factory() as session, session.begin():
            await service.assign_day(session, experiment_id, START + timedelta(days=offset))
    gappy = (await analyze(session_factory, experiment_id))[key]
    assert gappy.details["days_due"] == 5
    assert gappy.details["days_skipped"] == 2


async def test_nothing_is_reported_before_the_first_outcome(
    session_factory: SessionFactory,
) -> None:
    experiment_id = await lock_and_assign(session_factory)
    assert await analyze(session_factory, experiment_id, START) == {}


async def test_prior_mean_uses_only_days_before_d(session_factory: SessionFactory) -> None:
    experiment_id = await lock_and_assign(session_factory)
    arm_by_day = await arms(session_factory, experiment_id)
    outcomes = await add_outcomes(session_factory, arm_by_day, effect=0.2)
    async with session_factory() as session:
        frame = await load_frame(session, await session.get_one(Experiment, experiment_id))
    days: list[date] = list(frame["local_date"])
    priors: list[float] = [float(v) for v in frame["prior_ln_hrv_rmssd"]]
    for day, prior in zip(days, priors, strict=True):
        expected = np.mean([outcomes[day - timedelta(days=k)] for k in range(1, 8)])
        assert prior == pytest.approx(expected)
    assert list(frame["local_date"]) == sorted(arm_by_day)


async def test_results_are_appended_never_rewritten(session_factory: SessionFactory) -> None:
    experiment_id = await lock_and_assign(session_factory)
    await add_outcomes(session_factory, await arms(session_factory, experiment_id), effect=0.2)
    await analyze(session_factory, experiment_id, START + timedelta(days=14))
    async with session_factory() as session:
        first = [
            (r.id, r.effect, r.computed_at)
            for r in (await session.execute(select(ExperimentResult))).scalars()
        ]
    await analyze(session_factory, experiment_id)
    async with session_factory() as session:
        everything = {
            r.id: (r.id, r.effect, r.computed_at)
            for r in (await session.execute(select(ExperimentResult))).scalars()
        }
    assert len(everything) == 2 * len(first) == 12
    assert all(everything[row[0]] == row for row in first)


async def test_same_data_gives_the_same_result(session_factory: SessionFactory) -> None:
    experiment_id = await lock_and_assign(session_factory)
    await add_outcomes(session_factory, await arms(session_factory, experiment_id), effect=0.1)
    a = (await analyze(session_factory, experiment_id))[(METHOD_PRIMARY, "ln_hrv_rmssd")]
    b = (await analyze(session_factory, experiment_id))[(METHOD_PRIMARY, "ln_hrv_rmssd")]
    assert (a.effect, a.p_value, a.ci_low, a.ci_high) == (b.effect, b.p_value, b.ci_low, b.ci_high)


def test_describe_never_claims_proof() -> None:
    row = ResultRow("ln_hrv_rmssd", METHOD_PRIMARY, 14, 14, 1, 0, 0.05, -0.01, 0.11, 0.09, False)
    row.details["percent_change"] = 5.1
    sentence = describe(row)
    assert "estimate +0.050 ln ms (about +5.1%)" in sentence
    assert "95% interval -0.010 to +0.110" in sentence
    assert "n = 14 treatment, 14 control" in sentence
    assert "1 mornings missing" in sentence
    assert "prove" not in sentence.lower()
    assert "significant" not in sentence.lower()


async def test_intervals_use_the_alpha_fixed_in_the_spec(session_factory: SessionFactory) -> None:
    experiment_id = await lock_and_assign(session_factory)
    await add_outcomes(session_factory, await arms(session_factory, experiment_id), effect=0.1)
    at_95 = await analyze(session_factory, experiment_id)
    async with session_factory() as session, session.begin():
        # A stand-in for a spec locked with alpha 0.10 (TRUNCATE-style bypass of the guard).
        await session.execute(text("ALTER TABLE experiments DISABLE TRIGGER experiments_guard"))
        await session.execute(
            text("UPDATE experiments SET analysis_spec = analysis_spec || '{\"alpha\": 0.1}'")
        )
        await session.execute(text("ALTER TABLE experiments ENABLE TRIGGER experiments_guard"))
    at_90 = await analyze(session_factory, experiment_id)

    for key in ((METHOD_PRIMARY, "ln_hrv_rmssd"), (METHOD_ADJUSTED, "ln_hrv_rmssd")):
        wide, narrow = at_95[key], at_90[key]
        assert wide.ci_low is not None
        assert wide.ci_high is not None
        assert narrow.ci_low is not None
        assert narrow.ci_high is not None
        assert narrow.effect == wide.effect
        assert wide.ci_low < narrow.ci_low < narrow.ci_high < wide.ci_high
        assert narrow.details["alpha"] == 0.1
        assert "90% interval" in describe(narrow)
        assert "95% interval" in describe(wide)


# --- scheduled jobs -----------------------------------------------------------------------


async def test_evening_job_assigns_tomorrow_once_and_notifies_once(
    session_factory: SessionFactory,
    settings: Settings,
    http: httpx.AsyncClient,
    whoop_mock: respx.MockRouter,
) -> None:
    settings = settings.model_copy(update={"ntfy_url": NTFY})
    route = whoop_mock.post(NTFY).mock(return_value=httpx.Response(200))
    experiment_id = await lock_and_assign(session_factory, planned_days=4)
    # lock_and_assign filled all four days; start again from none (TRUNCATE skips row triggers).
    async with session_factory() as session, session.begin():
        await session.execute(text("TRUNCATE assignments"))

    afternoon = datetime(2026, 6, 1, 15, 0, tzinfo=NY)
    evening = datetime(2026, 6, 1, 20, 30, tzinfo=NY)
    assert await assign_tomorrow(session_factory, settings, http=http, now=afternoon) is None
    assert route.call_count == 0  # never earlier than the evening before

    assignment = await assign_tomorrow(session_factory, settings, http=http, now=evening)
    assert assignment is not None
    assert assignment.local_date == date(2026, 6, 2)
    assert await assign_tomorrow(session_factory, settings, http=http, now=evening) is None
    assert route.call_count == 1

    sent = route.calls[0].request
    expected_title = "Tomorrow: TREATMENT" if assignment.arm == "treatment" else "Tomorrow: CONTROL"
    assert sent.headers["title"] == expected_title
    body = sent.content.decode()
    assert ("by noon" in body) == (assignment.arm == "treatment")
    async with session_factory() as session:
        assert await service.assigned_count(session, experiment_id) == 1


async def test_evening_job_does_nothing_without_an_active_experiment(
    session_factory: SessionFactory, settings: Settings, http: httpx.AsyncClient
) -> None:
    evening = datetime(2026, 6, 1, 21, 0, tzinfo=NY)
    assert await assign_tomorrow(session_factory, settings, http=http, now=evening) is None


async def test_daily_analysis_appends_results_and_closes_on_schedule(
    session_factory: SessionFactory, settings: Settings
) -> None:
    experiment_id = await lock_and_assign(session_factory, planned_days=8)
    await add_outcomes(session_factory, await arms(session_factory, experiment_id), effect=0.2)
    last = START + timedelta(days=7)

    async def run(day: date) -> int:
        now = datetime(day.year, day.month, day.day, 13, 0, tzinfo=NY)
        return len(await run_daily_analysis(session_factory, settings, now=now))

    async def status() -> str:
        async with session_factory() as session:
            return (await session.get_one(Experiment, experiment_id)).status

    assert await run(last) == 6
    assert await status() == "active"
    assert await run(last + timedelta(days=1)) == 6
    assert await status() == "active"
    assert await run(last + timedelta(days=2)) == 6  # the final analysis, then it closes
    assert await status() == "completed"
    assert await run(last + timedelta(days=3)) == 0
    async with session_factory() as session:
        total = (
            await session.execute(select(func.count()).select_from(ExperimentResult))
        ).scalar_one()
    assert total == 18


async def test_timezone_cannot_change_while_an_experiment_runs(
    session_factory: SessionFactory, settings: Settings
) -> None:
    await lock_and_assign(session_factory, planned_days=4)
    with pytest.raises(RuntimeError, match="while experiment"):
        await apply_app_config(
            session_factory, settings.model_copy(update={"local_timezone": "Europe/London"})
        )
    await apply_app_config(session_factory, settings)  # unchanged zone is fine
