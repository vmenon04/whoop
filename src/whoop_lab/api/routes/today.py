"""/today: tonight's arm, yesterday's compliance and the daily tags. Phone-first."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from whoop_lab.api.deps import local_today, same_origin, settings_of
from whoop_lab.api.templating import templates
from whoop_lab.experiments import service
from whoop_lab.experiments.service import ExperimentError
from whoop_lab.models import DailyTag

router = APIRouter()

NOTE_KEY = "note"
TAG_WINDOW_DAYS = 14


def _tag_day(raw: str | None, today: date) -> date:
    """Tags can be edited for today and the two weeks before it. Default: yesterday."""
    if not raw:
        return today - timedelta(days=1)
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(400, "bad date") from None
    if not today - timedelta(days=TAG_WINDOW_DAYS) <= day <= today:
        raise HTTPException(400, "tags can only be edited for the last two weeks")
    return day


def _back(day: date) -> Response:
    return RedirectResponse(f"/today?date={day.isoformat()}", status_code=303)


@router.get("/today", response_class=HTMLResponse)
async def today_page(request: Request, date: str | None = None) -> Response:
    today = local_today(request)
    tag_day = _tag_day(date, today)
    yesterday = today - timedelta(days=1)
    async with request.app.state.session_factory() as session:
        experiment = await service.active_experiment(session)
        assignments = {}
        if experiment is not None:
            for label, day in (
                ("today", today),
                ("tomorrow", today + timedelta(days=1)),
                ("yesterday", yesterday),
            ):
                assignments[label] = await service.get_assignment(session, experiment.id, day)
        tags = {
            row.key: row.value
            for row in (
                await session.execute(select(DailyTag).where(DailyTag.local_date == tag_day))
            ).scalars()
        }
    settings = settings_of(request)
    return templates.TemplateResponse(
        request,
        "today.html",
        {
            "today": today,
            "yesterday": yesterday,
            "experiment": experiment,
            "assignments": assignments,
            "assign_hour": settings.assign_hour,
            "tag_day": tag_day,
            "tag_keys": settings.tag_key_list,
            "tags": tags,
            "note": tags.get(NOTE_KEY, ""),
            "previous_day": tag_day - timedelta(days=1)
            if tag_day > today - timedelta(days=TAG_WINDOW_DAYS)
            else None,
            "next_day": tag_day + timedelta(days=1) if tag_day < today else None,
        },
    )


@router.post("/today/compliance", dependencies=[Depends(same_origin)])
async def set_compliance(
    request: Request,
    day: Annotated[str, Form()],
    complied: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
) -> Response:
    today = local_today(request)
    try:
        target = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, "bad date") from None
    value = {"yes": True, "no": False, "clear": None}.get(complied, ...)
    if value is ...:
        raise HTTPException(400, "complied must be yes, no or clear")
    async with request.app.state.session_factory() as session, session.begin():
        experiment = await service.active_experiment(session)
        if experiment is None:
            raise HTTPException(400, "no experiment is running")
        try:
            await service.record_compliance(
                session, experiment.id, target, complied=value, note=note, today=today
            )
        except ExperimentError as exc:
            raise HTTPException(400, str(exc)) from None
    return RedirectResponse("/today", status_code=303)


@router.post("/today/tags", dependencies=[Depends(same_origin)])
async def toggle_tag(
    request: Request, day: Annotated[str, Form()], key: Annotated[str, Form()]
) -> Response:
    target = _tag_day(day, local_today(request))
    if key not in settings_of(request).tag_key_list:
        raise HTTPException(400, "unknown tag")
    async with request.app.state.session_factory() as session, session.begin():
        existing = await session.get(DailyTag, (target, key))
        if existing is None:
            session.add(DailyTag(local_date=target, key=key, value="true"))
        else:
            await session.delete(existing)
    return _back(target)


@router.post("/today/note", dependencies=[Depends(same_origin)])
async def set_note(
    request: Request, day: Annotated[str, Form()], note: Annotated[str, Form()] = ""
) -> Response:
    target = _tag_day(day, local_today(request))
    text_ = note.strip()[:500]
    async with request.app.state.session_factory() as session, session.begin():
        if text_:
            await session.execute(
                insert(DailyTag)
                .values(local_date=target, key=NOTE_KEY, value=text_)
                .on_conflict_do_update(index_elements=["local_date", "key"], set_={"value": text_})
            )
        else:
            await session.execute(
                delete(DailyTag).where(DailyTag.local_date == target, DailyTag.key == NOTE_KEY)
            )
    return _back(target)
