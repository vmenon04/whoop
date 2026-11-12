"""Liveness (`/healthz`) and an end-to-end check for an uptime monitor (`/uptime`)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select, text

from whoop_lab.models import OAuthToken, SyncRun
from whoop_lab.models.plumbing import TOKEN_ACTIVE

router = APIRouter()
log = logging.getLogger(__name__)

SYNC_GRACE = timedelta(minutes=30)


@router.get("/healthz")
async def healthz(request: Request, response: Response) -> dict[str, str]:
    try:
        async with request.app.state.session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        log.exception("healthz: database check failed")
        response.status_code = 503
        return {"status": "unavailable"}
    return {"status": "ok"}


@router.get("/uptime")
async def uptime(request: Request, response: Response) -> dict[str, object]:
    """200 only while the whole chain works: the database answers, the WHOOP token
    is usable, and the worker finished a sync without error recently enough.

    The body names which check failed and nothing else: no data, times or counts.
    """
    settings = request.app.state.settings
    checks = {"database": "failed", "token": "unknown", "sync": "unknown"}
    try:
        async with request.app.state.session_factory() as session:
            token = await session.get(OAuthToken, 1)
            last_good_sync = (
                await session.execute(
                    select(func.max(SyncRun.finished_at)).where(SyncRun.error.is_(None))
                )
            ).scalar()
        checks["database"] = "ok"
        if token is None:
            checks["token"] = "missing"
        else:
            checks["token"] = "ok" if token.status == TOKEN_ACTIVE else token.status
        # Two missed reconciliations in a row means the worker is down or WHOOP calls fail.
        allowed = 2 * timedelta(hours=settings.reconcile_interval_hours) + SYNC_GRACE
        if last_good_sync is None:
            checks["sync"] = "never"
        else:
            fresh = datetime.now(UTC) - last_good_sync <= allowed
            checks["sync"] = "ok" if fresh else "stale"
    except Exception:
        log.exception("uptime: database check failed")
    healthy = all(value == "ok" for value in checks.values())
    if not healthy:
        response.status_code = 503
    return {"status": "ok" if healthy else "degraded", "checks": checks}
