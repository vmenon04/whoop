from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from whoop_lab.analysis.pipeline import analyze_experiment
from whoop_lab.db import SessionFactory
from whoop_lab.experiments import service
from whoop_lab.experiments.service import Draft
from whoop_lab.models import Assignment, DailyTag, Experiment

ORIGIN = {"Origin": "https://whoop.test"}
FORM: dict[str, Any] = {
    "name": "Caffeine cutoff",
    "hypothesis": "A noon caffeine cutoff raises next-morning HRV.",
    "intervention": "Last caffeine by noon",
    "control": "Last caffeine by 4pm",
    "planned_days": "28",
    "outcome_metric": "ln_hrv_rmssd",
    "block_size": "4",
    "washout_days": "0",
}


def real_today() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()


async def running_experiment(session_factory: SessionFactory) -> tuple[int, dict[date, Assignment]]:
    """An experiment that started yesterday, with yesterday, today and tomorrow assigned."""
    today = real_today()
    async with session_factory() as session, session.begin():
        experiment = await service.create_draft(
            session,
            Draft(
                name="Caffeine cutoff",
                hypothesis="h",
                intervention="Last caffeine by noon",
                control="Last caffeine by 4pm",
                planned_days=28,
            ),
        )
        await service.lock_experiment(session, experiment.id, today - timedelta(days=2))
        experiment_id = experiment.id
    by_day: dict[date, Assignment] = {}
    for offset in (-1, 0, 1):
        async with session_factory() as session, session.begin():
            assignment, _ = await service.assign_day(
                session, experiment_id, today + timedelta(days=offset)
            )
            assert assignment is not None
            by_day[assignment.local_date] = assignment
    return experiment_id, by_day


def text_for(arm: str) -> str:
    return "Last caffeine by noon" if arm == "treatment" else "Last caffeine by 4pm"


# --- /today -------------------------------------------------------------------------------


async def test_today_without_an_experiment(api: httpx.AsyncClient) -> None:
    response = await api.get("/today")
    assert response.status_code == 200
    assert "No experiment is running" in response.text
    assert "alcohol" in response.text  # tags work without an experiment


async def test_today_shows_todays_and_tomorrows_arm(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    _, by_day = await running_experiment(session_factory)
    today = real_today()
    response = await api.get("/today")
    assert response.status_code == 200
    page = response.text
    assert f"Today · {by_day[today].arm}" in page
    assert f"Tomorrow · {by_day[today + timedelta(days=1)].arm}" in page
    assert text_for(by_day[today].arm) in page
    assert "Did you follow it?" in page


async def test_tomorrows_arm_is_not_shown_before_it_is_drawn(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    today = real_today()
    async with session_factory() as session, session.begin():
        experiment = await service.create_draft(
            session, Draft("X", "h", "do it", "do not", planned_days=8)
        )
        await service.lock_experiment(session, experiment.id, today - timedelta(days=1))
        await service.assign_day(session, experiment.id, today)
    page = (await api.get("/today")).text
    assert "Tomorrow ·" not in page
    assert "Tomorrow's arm is drawn after 20:00" in page


async def test_compliance_can_be_recorded_and_changed(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    experiment_id, _ = await running_experiment(session_factory)
    yesterday = real_today() - timedelta(days=1)

    response = await api.post(
        "/today/compliance",
        data={"day": yesterday.isoformat(), "complied": "no", "note": "coffee at 3pm"},
        headers=ORIGIN,
    )
    assert response.status_code == 303
    async with session_factory() as session:
        row = await service.get_assignment(session, experiment_id, yesterday)
    assert row is not None
    assert (row.complied, row.note) == (False, "coffee at 3pm")

    await api.post(
        "/today/compliance", data={"day": yesterday.isoformat(), "complied": "yes"}, headers=ORIGIN
    )
    async with session_factory() as session:
        row = await service.get_assignment(session, experiment_id, yesterday)
    assert row is not None
    assert row.complied is True
    assert "coffee at 3pm" not in (await api.get("/today")).text


async def test_compliance_for_a_future_day_is_refused(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    await running_experiment(session_factory)
    tomorrow = real_today() + timedelta(days=1)
    response = await api.post(
        "/today/compliance", data={"day": tomorrow.isoformat(), "complied": "yes"}, headers=ORIGIN
    )
    assert response.status_code == 400


async def test_tags_toggle_and_note(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    yesterday = real_today() - timedelta(days=1)
    day = yesterday.isoformat()

    async def tags() -> dict[str, str]:
        async with session_factory() as session:
            rows = (await session.execute(select(DailyTag))).scalars()
            return {row.key: row.value for row in rows if row.local_date == yesterday}

    assert (
        await api.post("/today/tags", data={"day": day, "key": "alcohol"}, headers=ORIGIN)
    ).status_code == 303
    await api.post("/today/note", data={"day": day, "note": " red-eye flight "}, headers=ORIGIN)
    assert await tags() == {"alcohol": "true", "note": "red-eye flight"}
    page = (await api.get("/today")).text
    assert 'aria-pressed="true">alcohol' in page
    assert "red-eye flight" in page

    await api.post("/today/tags", data={"day": day, "key": "alcohol"}, headers=ORIGIN)
    await api.post("/today/note", data={"day": day, "note": ""}, headers=ORIGIN)
    assert await tags() == {}


async def test_unknown_tags_and_old_dates_are_refused(api: httpx.AsyncClient) -> None:
    today = real_today()
    bad_key = await api.post(
        "/today/tags", data={"day": today.isoformat(), "key": "nope"}, headers=ORIGIN
    )
    old = await api.post(
        "/today/tags",
        data={"day": (today - timedelta(days=30)).isoformat(), "key": "alcohol"},
        headers=ORIGIN,
    )
    future = await api.get(f"/today?date={(today + timedelta(days=1)).isoformat()}")
    assert (bad_key.status_code, old.status_code, future.status_code) == (400, 400, 400)


async def test_posts_from_another_site_are_refused(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    day = real_today().isoformat()
    for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
        response = await api.post(
            "/today/tags", data={"day": day, "key": "alcohol"}, headers=headers
        )
        assert response.status_code == 403
        assert (await api.post("/experiments", data=FORM, headers=headers)).status_code == 403
    async with session_factory() as session:
        assert (await session.execute(select(DailyTag))).first() is None
        assert (await session.execute(select(Experiment))).first() is None


# --- /experiments -------------------------------------------------------------------------


async def test_create_lock_and_view_an_experiment(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    assert "No experiments yet" in (await api.get("/experiments")).text

    created = await api.post("/experiments", data=FORM, headers=ORIGIN)
    assert created.status_code == 303
    location = created.headers["location"]
    detail = await api.get(location)
    assert "Caffeine cutoff" in detail.text
    assert "draft" in detail.text
    assert "Lock and start" in detail.text
    assert "&#34;draws&#34;: 10000" in detail.text or '"draws": 10000' in detail.text

    locked = await api.post(f"{location}/lock", headers=ORIGIN)
    assert locked.status_code == 303
    async with session_factory() as session:
        experiment = (await session.execute(select(Experiment))).scalar_one()
    assert experiment.status == "active"
    assert experiment.started_on == real_today() + timedelta(days=1)

    detail = await api.get(location)
    assert "Lock and start" not in detail.text
    assert "No results yet" in detail.text
    listing = await api.get("/experiments")
    assert "0 of 28 days assigned" in listing.text

    # Locked: the page cannot delete it or lock it again.
    assert (await api.post(f"{location}/delete", headers=ORIGIN)).status_code == 400
    assert (await api.post(f"{location}/lock", headers=ORIGIN)).status_code == 400
    async with session_factory() as session:
        assert (await session.execute(select(Experiment))).scalar_one().status == "active"


async def test_invalid_draft_shows_the_reason(api: httpx.AsyncClient) -> None:
    response = await api.post("/experiments", data={**FORM, "block_size": "3"}, headers=ORIGIN)
    assert response.status_code == 400
    assert "block size must be an even number" in response.text


async def test_draft_can_be_deleted(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    created = await api.post("/experiments", data=FORM, headers=ORIGIN)
    deleted = await api.post(f"{created.headers['location']}/delete", headers=ORIGIN)
    assert deleted.status_code == 303
    async with session_factory() as session:
        assert (await session.execute(select(Experiment))).first() is None


async def test_second_experiment_cannot_be_locked_while_one_runs(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    await running_experiment(session_factory)
    created = await api.post("/experiments", data={**FORM, "name": "Screens"}, headers=ORIGIN)
    response = await api.post(f"{created.headers['location']}/lock", headers=ORIGIN)
    assert response.status_code == 400
    assert "one experiment at a time" in response.text


async def test_results_summary_is_shown_with_labels(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    from tests.world import START, add_outcomes, arms, lock_and_assign

    experiment_id = await lock_and_assign(session_factory)
    await add_outcomes(session_factory, await arms(session_factory, experiment_id), effect=0.3)
    async with session_factory() as session, session.begin():
        experiment = await session.get_one(Experiment, experiment_id)
        await analyze_experiment(session, experiment, START + timedelta(days=40))

    detail = (await api.get(f"/experiments/{experiment_id}")).text
    assert "Primary: intention to treat, randomization test" in detail
    assert "per protocol (biased" in detail
    assert "Exploratory outcome, no multiplicity correction" in detail
    assert "n = 14 treatment, 14 control" in detail
    assert "0 mornings missing" in detail
    assert "planned days whatever these numbers show" in detail
    assert "prove" not in detail.lower()

    listing = (await api.get("/experiments")).text
    assert "28 of 28 days assigned" in listing
    assert "95% interval" in listing


async def test_missing_experiment_is_404(api: httpx.AsyncClient) -> None:
    assert (await api.get("/experiments/999")).status_code == 404


async def test_privacy_page_has_no_private_links_or_scripts(api: httpx.AsyncClient) -> None:
    page = (await api.get("/privacy")).text
    assert "<script" not in page
    assert "/experiments" not in page
    assert "/today" not in page
