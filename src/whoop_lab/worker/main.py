"""Worker process: the scheduler. The only process that talks to WHOOP after sign-in."""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from whoop_lab.config import Settings, get_settings
from whoop_lab.db import SessionFactory, make_engine, make_session_factory
from whoop_lab.log import configure_logging

log = logging.getLogger(__name__)


def build_scheduler(settings: Settings, session_factory: SessionFactory) -> AsyncIOScheduler:
    """Declare every job here. Jobs live in code, not in a job store."""
    scheduler = AsyncIOScheduler(
        timezone=UTC,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
    )
    return scheduler


async def run(settings: Settings) -> None:
    configure_logging(settings.log_level)
    engine = make_engine(settings.database_url)
    scheduler = build_scheduler(settings, make_session_factory(engine))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    scheduler.start()
    log.info("worker started with %d jobs", len(scheduler.get_jobs()))
    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        await engine.dispose()
        log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(run(get_settings()))
