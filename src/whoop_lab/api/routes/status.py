"""Status page: is the token alive and is data arriving. Reads the database only.

The site's front page is the Health dashboard in Grafana; `/` sends you there.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select

from whoop_lab.api.templating import templates
from whoop_lab.models import Cycle, OAuthToken, Recovery, Sleep, SyncRun, WebhookEvent, Workout

router = APIRouter()


# Grafana's home dashboard (Health), served under /grafana by the same tunnel.
HOME_URL = "/grafana/"


@router.get("/")
async def home() -> Response:
    """The front page is the Health dashboard. 302, so browsers never cache the move."""
    return RedirectResponse(HOME_URL, status_code=302)


@router.get("/status", response_class=HTMLResponse)
async def status(request: Request) -> Response:
    tz = request.app.state.settings.tz
    async with request.app.state.session_factory() as session:
        token = await session.get(OAuthToken, 1)
        tables: list[dict[str, Any]] = []
        for label, model, when in (
            ("Cycles", Cycle, Cycle.start),
            ("Sleeps", Sleep, Sleep.end),
            ("Recoveries", Recovery, Recovery.created_at),
            ("Workouts", Workout, Workout.start),
        ):
            total, latest = (
                await session.execute(
                    select(func.count(), func.max(when)).where(model.deleted_at.is_(None))
                )
            ).one()
            tables.append({"label": label, "count": total, "latest": latest})
        events = dict(
            (
                await session.execute(
                    select(WebhookEvent.status, func.count()).group_by(WebhookEvent.status)
                )
            ).all()
        )
        last_event = (await session.execute(select(func.max(WebhookEvent.received_at)))).scalar()
        runs = list(
            (await session.execute(select(SyncRun).order_by(SyncRun.id.desc()).limit(5))).scalars()
        )

    return templates.TemplateResponse(
        request,
        "status.html",
        {
            # Only status and times. Token values never reach a template.
            "token_status": token.status if token else None,
            "token_expires_at": token.expires_at if token else None,
            "token_updated_at": token.updated_at if token else None,
            "tables": tables,
            "events": events,
            "last_event": last_event,
            "runs": runs,
            "tz": tz,
        },
    )


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request) -> Response:
    """Static page. WHOOP's app registration asks for a privacy policy URL."""
    return templates.TemplateResponse(request, "privacy.html", {})
