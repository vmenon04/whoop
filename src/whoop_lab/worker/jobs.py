"""Scheduled jobs. Each is a thin wrapper that logs and never raises into the scheduler."""

from __future__ import annotations

import logging

from whoop_lab.ble.importer import CaptureWatcher
from whoop_lab.experiments.jobs import assign_tomorrow, run_daily_analysis
from whoop_lab.health import check_morning_recovery
from whoop_lab.ingest.events import drain_events
from whoop_lab.ingest.sync import SyncSummary, reconcile
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


async def reconcile_job(ctx: WorkerContext) -> SyncSummary:
    """Cycle poll plus trailing-window reconciliation of every collection."""
    return await reconcile(
        ctx.whoop, ctx.session_factory, window_days=ctx.settings.reconcile_window_days
    )


async def morning_recovery_job(ctx: WorkerContext) -> bool:
    return await check_morning_recovery(ctx.session_factory, ctx.settings, http=ctx.http)


async def assign_tomorrow_job(ctx: WorkerContext) -> bool:
    return (await assign_tomorrow(ctx.session_factory, ctx.settings, http=ctx.http)) is not None


async def daily_analysis_job(ctx: WorkerContext) -> int:
    return len(await run_daily_analysis(ctx.session_factory, ctx.settings))


async def import_captures_job(ctx: WorkerContext, watcher: CaptureWatcher) -> bool:
    """Imports band captures when new ones have been copied to the server."""
    summary = await watcher.check(ctx.session_factory)
    if summary is None:
        return False
    log.info(
        "imported %d captures: %d nights and %d minutes written",
        len(summary.files),
        summary.nights_written,
        summary.minutes_written,
    )
    return True
