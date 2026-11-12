"""Scheduled experiment work: the evening assignment and the daily analysis."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx

from whoop_lab.analysis.pipeline import ResultRow, analyze_experiment
from whoop_lab.config import Settings
from whoop_lab.db import SessionFactory
from whoop_lab.experiments import service
from whoop_lab.models.experiments import ARM_TREATMENT, Assignment
from whoop_lab.notify import notify

log = logging.getLogger(__name__)


async def assign_tomorrow(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    http: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> Assignment | None:
    """In the evening, draw tomorrow's arm for the active experiment and send it.

    Safe to call repeatedly: the arm is drawn once and the notification is sent
    only when the assignment is created. Never runs before `assign_hour`, so
    tomorrow's arm does not exist earlier than the evening before.
    """
    local_now = (now or datetime.now(settings.tz)).astimezone(settings.tz)
    if local_now.hour < settings.assign_hour:
        return None
    tomorrow = local_now.date() + timedelta(days=1)
    async with session_factory() as session, session.begin():
        experiment = await service.active_experiment(session)
        if experiment is None:
            return None
        assignment, created = await service.assign_day(session, experiment.id, tomorrow)
        if assignment is None or not created:
            return None
        name, intervention, control = experiment.name, experiment.intervention, experiment.control
    treatment = assignment.arm == ARM_TREATMENT
    log.info("experiments: assigned %s for %s", assignment.arm, tomorrow)
    await notify(
        settings,
        f"Tomorrow: {'TREATMENT' if treatment else 'CONTROL'}",
        f"{tomorrow:%a %b %-d} · {name}\n{intervention if treatment else control}",
        tags="game_die",
        http=http,
    )
    return assignment


async def run_daily_analysis(
    session_factory: SessionFactory, settings: Settings, *, now: datetime | None = None
) -> list[ResultRow]:
    """Append today's results for the active experiment, then close it if its
    calendar has run out. Closing never depends on the results."""
    today = (now or datetime.now(settings.tz)).astimezone(settings.tz).date()
    async with session_factory() as session, session.begin():
        experiment = await service.active_experiment(session)
        if experiment is None:
            return []
        rows = await analyze_experiment(session, experiment, today)
        if await service.complete_if_finished(session, experiment.id, today):
            log.info("experiments: %r reached its planned days and is completed", experiment.name)
    return rows
