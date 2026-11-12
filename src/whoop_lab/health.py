"""Data-health checks that alert through ntfy."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import text

from whoop_lab.config import Settings
from whoop_lab.db import SessionFactory
from whoop_lab.models import OAuthToken
from whoop_lab.notify import notify

log = logging.getLogger(__name__)


async def check_morning_recovery(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    http: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> bool:
    """Alert if this morning's recovery is not scored yet. Returns True when it is fine.

    "This morning's recovery" is yesterday's outcome, read from daily_outcomes so
    the definition matches the analysis exactly.
    """
    local_now = (now or datetime.now(settings.tz)).astimezone(settings.tz)
    yesterday = local_now.date() - timedelta(days=1)
    async with session_factory() as session:
        if await session.get(OAuthToken, 1) is None:
            return True  # WHOOP was never connected; nothing to expect yet
        scored = (
            await session.execute(
                text("SELECT outcome_scored FROM daily_outcomes WHERE local_date = :day"),
                {"day": yesterday},
            )
        ).scalar()
    if scored:
        return True
    log.warning("health: no scored recovery for the morning of %s", local_now.date())
    await notify(
        settings,
        "whoop-lab: no recovery this morning",
        f"No scored recovery for {local_now.date():%a %b %-d} by {local_now:%H:%M}. "
        f"Check the WHOOP app has synced, then {settings.public_base_url}/",
        priority="high",
        tags="warning",
        http=http,
    )
    return False
