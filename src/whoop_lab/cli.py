"""`whoop-lab` command line.

Commands that call WHOOP are meant to run in the worker container:
    docker compose exec worker whoop-lab backfill
They read the stored token and never refresh it; the worker's schedule does that.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated

import httpx
import typer

from whoop_lab.config import get_settings
from whoop_lab.db import make_engine, make_session_factory
from whoop_lab.ingest import sync as sync_module
from whoop_lab.ingest.sync import SyncSummary
from whoop_lab.log import configure_logging
from whoop_lab.whoop.client import WhoopClient

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def _root() -> None:
    """WHOOP archive and n-of-1 experiments."""


async def _with_client(kind: str, since: datetime | None, days: int | None) -> SyncSummary:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = make_engine(settings.db_url)
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            session_factory = make_session_factory(engine)
            client = WhoopClient(http, session_factory, settings)
            if kind == "backfill":
                return await sync_module.backfill(client, session_factory, since=since)
            return await sync_module.reconcile(
                client,
                session_factory,
                window_days=days or settings.reconcile_window_days,
                kind="manual",
            )
    finally:
        await engine.dispose()


def _report(summary: SyncSummary) -> None:
    typer.echo(
        f"{summary.kind}: {summary.records_seen} records seen, "
        f"{summary.rows_upserted} upserted, {summary.rows_deleted} soft-deleted"
    )
    for error in summary.errors:
        typer.echo(f"error: {error}", err=True)
    if not summary.ok:
        raise typer.Exit(1)


@app.command()
def backfill(
    since: Annotated[
        datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Only load records from this UTC date on."),
    ] = None,
) -> None:
    """Load WHOOP history for every collection. Safe to re-run."""
    start = since.replace(tzinfo=UTC) if since else None
    _report(asyncio.run(_with_client("backfill", start, None)))


@app.command()
def sync(
    days: Annotated[int | None, typer.Option(min=1, help="Trailing window in days.")] = None,
) -> None:
    """Run the reconciliation now: poll cycles and re-fetch the trailing window."""
    _report(asyncio.run(_with_client("manual", None, days)))


def main() -> None:
    app()
