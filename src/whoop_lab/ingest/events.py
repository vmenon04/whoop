"""Webhook event worker.

Claims one pending event at a time with SELECT ... FOR UPDATE SKIP LOCKED,
fetches the resource it points at, and upserts. The row lock is held for the
whole attempt, so two workers can never process the same event.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from whoop_lab.config import Settings
from whoop_lab.db import SessionFactory
from whoop_lab.ingest import upsert
from whoop_lab.log import redact
from whoop_lab.models.plumbing import EVENT_DONE, EVENT_FAILED, EVENT_PENDING, WebhookEvent
from whoop_lab.whoop.client import (
    WhoopAuthError,
    WhoopClient,
    WhoopNotFoundError,
    WhoopRateLimitedError,
)

log = logging.getLogger(__name__)

AUTH_DEFER = timedelta(minutes=5)
RATE_LIMIT_DEFER = timedelta(minutes=15)
MAX_BACKOFF = timedelta(hours=6)


class PermanentEventError(Exception):
    """The event can never be processed (for example a non-UUID id). No retry."""


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise PermanentEventError("resource id is not a UUID (v1 webhook model?)") from None


async def _sleep_updated(session: AsyncSession, client: WhoopClient, sleep_id: UUID) -> None:
    try:
        raw = await client.get_sleep(sleep_id)
    except WhoopNotFoundError:
        await upsert.soft_delete_sleep(session, sleep_id)
        return
    await upsert.upsert_sleep(session, raw)


async def _recovery_updated(session: AsyncSession, client: WhoopClient, sleep_id: UUID) -> None:
    """A v2 recovery event carries the sleep UUID. The recovery itself is only
    reachable through the cycle, so: sleep -> cycle_id -> cycle recovery."""
    try:
        sleep_raw = await client.get_sleep(sleep_id)
    except WhoopNotFoundError:
        await upsert.soft_delete_sleep(session, sleep_id)
        return
    await upsert.upsert_sleep(session, sleep_raw)

    try:
        recovery_raw = await client.get_recovery_for_cycle(int(sleep_raw["cycle_id"]))
    except WhoopNotFoundError:
        await upsert.soft_delete_recovery(session, sleep_id)
        return
    await upsert.upsert_recovery(session, recovery_raw)
    if str(recovery_raw.get("sleep_id")) != str(sleep_id):
        # The cycle's recovery now belongs to a different sleep.
        await upsert.soft_delete_recovery(session, sleep_id)


async def _workout_updated(session: AsyncSession, client: WhoopClient, workout_id: UUID) -> None:
    try:
        raw = await client.get_workout(workout_id)
    except WhoopNotFoundError:
        await upsert.soft_delete_workout(session, workout_id)
        return
    await upsert.upsert_workout(session, raw)


async def handle_event(session: AsyncSession, client: WhoopClient, event: WebhookEvent) -> str:
    """Apply one event. Returns a short note stored on the row ('' when unremarkable)."""
    match event.type:
        case "sleep.updated":
            await _sleep_updated(session, client, _uuid(event.resource_id))
        case "sleep.deleted":
            await upsert.soft_delete_sleep(session, _uuid(event.resource_id))
        case "recovery.updated":
            await _recovery_updated(session, client, _uuid(event.resource_id))
        case "recovery.deleted":
            await upsert.soft_delete_recovery(session, _uuid(event.resource_id))
        case "workout.updated":
            await _workout_updated(session, client, _uuid(event.resource_id))
        case "workout.deleted":
            await upsert.soft_delete_workout(session, _uuid(event.resource_id))
        case _:
            return f"ignored: unknown event type {event.type[:60]!r}"
    return ""


def _describe(exc: BaseException) -> str:
    return redact(f"{type(exc).__name__}: {exc}")[:500]


async def process_next_event(
    session_factory: SessionFactory, client: WhoopClient, settings: Settings
) -> bool | None:
    """Process at most one due event.

    Returns True if one was processed, False if none was due, and None if the
    worker should stop draining for now (no usable token, or rate limited).
    """
    async with session_factory() as session, session.begin():
        event = (
            await session.execute(
                select(WebhookEvent)
                .where(
                    WebhookEvent.status == EVENT_PENDING,
                    WebhookEvent.next_attempt_at <= func.now(),
                )
                .order_by(WebhookEvent.next_attempt_at, WebhookEvent.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if event is None:
            return False

        now = datetime.now(UTC)
        try:
            # Savepoint: a failure half-way leaves no partial writes behind.
            async with session.begin_nested():
                note = await handle_event(session, client, event)
        except (WhoopAuthError, WhoopRateLimitedError) as exc:
            # Not this event's fault. Do not spend one of its attempts.
            defer = AUTH_DEFER if isinstance(exc, WhoopAuthError) else RATE_LIMIT_DEFER
            event.next_attempt_at = now + defer
            event.error = _describe(exc)
            log.warning("events: pausing (%s)", event.error)
            return None
        except Exception as exc:
            event.attempts += 1
            event.error = _describe(exc)
            permanent = isinstance(exc, PermanentEventError)
            if permanent or event.attempts >= settings.event_max_attempts:
                event.status = EVENT_FAILED
                event.processed_at = now
                log.error(
                    "events: %s %s failed permanently after %d attempts: %s",
                    event.type,
                    event.resource_id,
                    event.attempts,
                    event.error,
                )
            else:
                delay = timedelta(
                    seconds=settings.event_backoff_base_seconds * 2 ** (event.attempts - 1)
                )
                event.next_attempt_at = now + min(delay, MAX_BACKOFF)
                log.warning(
                    "events: %s %s attempt %d failed: %s",
                    event.type,
                    event.resource_id,
                    event.attempts,
                    event.error,
                )
            return True

        event.attempts += 1
        event.status = EVENT_DONE
        event.processed_at = now
        event.error = note or None
        log.info("events: %s %s done", event.type, event.resource_id)
        return True


async def drain_events(
    session_factory: SessionFactory, client: WhoopClient, settings: Settings
) -> int:
    """Process up to one batch of due events. Returns how many were attempted."""
    processed = 0
    for _ in range(settings.event_batch_size):
        outcome = await process_next_event(session_factory, client, settings)
        if not outcome:
            break
        processed += 1
    return processed
