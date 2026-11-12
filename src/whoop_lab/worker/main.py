"""Worker process: the scheduler. The only process that talks to WHOOP after sign-in."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from whoop_lab.config import Settings, get_settings
from whoop_lab.db import make_engine, make_session_factory
from whoop_lab.log import configure_logging
from whoop_lab.worker import jobs
from whoop_lab.worker.context import WorkerContext

log = logging.getLogger(__name__)

Job = Callable[[WorkerContext], Awaitable[Any]]


def _guarded(job: Job, ctx: WorkerContext) -> Callable[[], Awaitable[None]]:
    async def run() -> None:
        try:
            await job(ctx)
        except Exception:
            log.exception("job %s failed", job.__name__)

    return run


def build_scheduler(ctx: WorkerContext) -> AsyncIOScheduler:
    """Declare every job here. Jobs live in code, not in a job store."""
    settings = ctx.settings
    scheduler = AsyncIOScheduler(
        timezone=UTC,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
    )
    now = datetime.now(UTC)
    scheduler.add_job(
        _guarded(jobs.refresh_token_job, ctx),
        "interval",
        seconds=settings.token_refresh_check_seconds,
        next_run_time=now,
        id="refresh_token",
    )
    return scheduler


async def run(settings: Settings) -> None:
    configure_logging(settings.log_level)
    engine = make_engine(settings.database_url)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with httpx.AsyncClient(timeout=30.0) as http:
        ctx = WorkerContext(settings, make_session_factory(engine), http)
        scheduler = build_scheduler(ctx)
        scheduler.start()
        log.info("worker started with jobs: %s", [job.id for job in scheduler.get_jobs()])
        try:
            await stop.wait()
        finally:
            scheduler.shutdown(wait=False)
    await engine.dispose()
    log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(run(get_settings()))
