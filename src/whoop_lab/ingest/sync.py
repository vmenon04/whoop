"""Collection sync: backfill, the cycle poll and trailing-window reconciliation.

Cycles have no webhook, so this is how they arrive at all. For sleeps,
recoveries and workouts it is the safety net for missed or failed webhooks and
for rescored records. Do not remove it as redundant.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from whoop_lab.db import SessionFactory
from whoop_lab.ingest import upsert
from whoop_lab.log import redact
from whoop_lab.models import OAuthToken, Recovery, Sleep, SyncRun, Workout
from whoop_lab.whoop.client import (
    Collection,
    WhoopAuthError,
    WhoopClient,
    WhoopError,
    WhoopRateLimitedError,
)

log = logging.getLogger(__name__)

BATCH_SIZE = 100
# Pruning only touches rows well inside the fetched window, because the API
# does not say precisely which timestamp its `start` filter applies to.
PRUNE_EDGE_MARGIN = timedelta(days=1)
PRUNE_RECENT_MARGIN = timedelta(hours=1)

type Record = dict[str, Any]


@dataclass(frozen=True)
class _Spec:
    name: Collection
    key: str
    upsert: Callable[[AsyncSession, Record], Awaitable[bool]]


SPECS: tuple[_Spec, ...] = (
    _Spec("cycle", "id", upsert.upsert_cycle),
    _Spec("sleep", "id", upsert.upsert_sleep),
    _Spec("recovery", "sleep_id", upsert.upsert_recovery),
    _Spec("workout", "id", upsert.upsert_workout),
)


@dataclass
class SyncSummary:
    kind: str
    rows_upserted: int = 0
    rows_deleted: int = 0
    records_seen: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _describe(exc: BaseException) -> str:
    return redact(f"{type(exc).__name__}: {exc}")[:300]


async def _write_batch(
    session_factory: SessionFactory, spec: _Spec, batch: list[Record], summary: SyncSummary
) -> None:
    async with session_factory() as session, session.begin():
        for raw in batch:
            try:
                changed = await spec.upsert(session, raw)
            except ValidationError as exc:
                # One record WHOOP sent in an unexpected shape must not block the rest.
                message = f"{spec.name}: record failed validation ({exc.error_count()} errors)"
                if message not in summary.errors:
                    summary.errors.append(message)
                log.error("sync: %s", message)
                continue
            summary.rows_upserted += int(changed)


async def _prune(
    session_factory: SessionFactory,
    spec: _Spec,
    seen: set[str],
    window_start: datetime,
    run_started: datetime,
) -> int:
    """Soft-delete local rows inside the window that WHOOP no longer returns."""
    low = window_start + PRUNE_EDGE_MARGIN
    high = run_started - PRUNE_RECENT_MARGIN
    if low >= high or spec.name == "cycle":
        return 0
    deleted = 0
    async with session_factory() as session, session.begin():
        if spec.name == "recovery":
            query = (
                select(Recovery.sleep_id)
                .join(Sleep, Sleep.id == Recovery.sleep_id)
                .where(
                    Recovery.deleted_at.is_(None),
                    Recovery.fetched_at < run_started,
                    Sleep.start >= low,
                    Sleep.start < high,
                )
            )
        else:
            table = Sleep if spec.name == "sleep" else Workout
            query = select(table.id).where(
                table.deleted_at.is_(None),
                table.fetched_at < run_started,
                table.start >= low,
                table.start < high,
            )
        local_ids: list[UUID] = list((await session.execute(query)).scalars())
        for row_id in local_ids:
            if str(row_id) in seen:
                continue
            if spec.name == "sleep":
                removed = await upsert.soft_delete_sleep(session, row_id)
            elif spec.name == "recovery":
                removed = await upsert.soft_delete_recovery(session, row_id)
            else:
                removed = await upsert.soft_delete_workout(session, row_id)
            if removed:
                deleted += 1
                log.warning("sync: %s %s is gone from WHOOP, soft-deleted", spec.name, row_id)
    return deleted


async def _ensure_user_id(session_factory: SessionFactory, client: WhoopClient) -> None:
    async with session_factory() as session:
        row = await session.get(OAuthToken, 1)
    if row is None or row.whoop_user_id is not None:
        return
    profile = await client.get_profile()
    async with session_factory() as session, session.begin():
        await session.execute(
            update(OAuthToken)
            .where(OAuthToken.id == 1)
            .values(whoop_user_id=int(profile["user_id"]))
        )


async def run_sync(
    client: WhoopClient,
    session_factory: SessionFactory,
    *,
    kind: str,
    start: datetime | None,
    prune: bool,
) -> SyncSummary:
    """Fetch every collection from `start` (or from the beginning) and upsert.

    A failure in one collection is recorded and the others still run, except
    for auth and rate-limit failures, which would fail the same way again.
    """
    summary = SyncSummary(kind)
    run_started = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        run = SyncRun(kind=kind, started_at=run_started)
        session.add(run)
        await session.flush()
        run_id = run.id

    try:
        await _ensure_user_id(session_factory, client)
        for spec in SPECS:
            seen: set[str] = set()
            batch: list[Record] = []
            try:
                async for raw in client.iter_collection(spec.name, start=start):
                    seen.add(str(raw.get(spec.key)))
                    batch.append(raw)
                    if len(batch) >= BATCH_SIZE:
                        await _write_batch(session_factory, spec, batch, summary)
                        batch = []
                await _write_batch(session_factory, spec, batch, summary)
            except (WhoopAuthError, WhoopRateLimitedError):
                raise
            except WhoopError as exc:
                summary.errors.append(f"{spec.name}: {_describe(exc)}")
                log.error("sync: %s failed: %s", spec.name, _describe(exc))
                continue
            summary.records_seen += len(seen)
            log.info("sync[%s]: %s fetched %d records", kind, spec.name, len(seen))
            if prune and start is not None:
                if seen:
                    summary.rows_deleted += await _prune(
                        session_factory, spec, seen, start, run_started
                    )
                else:
                    # An empty answer is more likely an API hiccup than a week of deletions.
                    log.info("sync: %s returned nothing; skipping the prune step", spec.name)
    except WhoopError as exc:
        summary.errors.append(_describe(exc))
        log.warning("sync[%s] stopped: %s", kind, _describe(exc))
    finally:
        async with session_factory() as session, session.begin():
            await session.execute(
                update(SyncRun)
                .where(SyncRun.id == run_id)
                .values(
                    finished_at=datetime.now(UTC),
                    rows_upserted=summary.rows_upserted,
                    rows_deleted=summary.rows_deleted,
                    error="; ".join(summary.errors)[:2000] or None,
                )
            )
    log.info(
        "sync[%s] finished: %d seen, %d upserted, %d soft-deleted, %d errors",
        kind,
        summary.records_seen,
        summary.rows_upserted,
        summary.rows_deleted,
        len(summary.errors),
    )
    return summary


async def reconcile(
    client: WhoopClient,
    session_factory: SessionFactory,
    *,
    window_days: int,
    kind: str = "reconcile",
) -> SyncSummary:
    """Poll cycles and re-fetch the trailing window of every collection."""
    start = datetime.now(UTC) - timedelta(days=window_days)
    return await run_sync(client, session_factory, kind=kind, start=start, prune=True)


async def backfill(
    client: WhoopClient, session_factory: SessionFactory, *, since: datetime | None = None
) -> SyncSummary:
    """Load full history (or everything since a date). Never prunes."""
    return await run_sync(client, session_factory, kind="backfill", start=since, prune=False)
