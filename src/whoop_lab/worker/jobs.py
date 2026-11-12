"""Scheduled jobs. Each is a thin wrapper that logs and never raises into the scheduler."""

from __future__ import annotations

import logging

from whoop_lab.ingest.events import drain_events
from whoop_lab.notify import notify
from whoop_lab.whoop.oauth import RefreshOutcome, refresh_if_needed
from whoop_lab.worker.context import WorkerContext

log = logging.getLogger(__name__)


async def refresh_token_job(ctx: WorkerContext) -> RefreshOutcome:
    result = await refresh_if_needed(ctx.session_factory, ctx.http, ctx.settings)
    if result.outcome is RefreshOutcome.REAUTH_REQUIRED:
        # Sent once: later ticks see AWAITING_REAUTH and stay quiet.
        await notify(
            ctx.settings,
            "whoop-lab: WHOOP sign-in needed",
            f"Token refresh was rejected ({result.detail}). "
            f"Open {ctx.settings.public_base_url}/oauth/start to reconnect.",
            priority="high",
            tags="warning",
            http=ctx.http,
        )
    return result.outcome


async def process_events_job(ctx: WorkerContext) -> int:
    return await drain_events(ctx.session_factory, ctx.whoop, ctx.settings)
