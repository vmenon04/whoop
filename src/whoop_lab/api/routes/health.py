"""Liveness and database reachability."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

router = APIRouter()
log = logging.getLogger(__name__)


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
