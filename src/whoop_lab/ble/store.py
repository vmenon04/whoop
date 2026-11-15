"""Write band metrics to `ble_nights` and `ble_minutes`, and pick captures to import."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from whoop_lab.ble.capture import NOTIFICATIONS_FILE
from whoop_lab.ble.metrics import NightMetrics
from whoop_lab.ble.minutes import Minute
from whoop_lab.db import SessionFactory
from whoop_lab.models import BleMinute, BleNight

# Capture directories are named by `ble_spike.py` as YYYYMMDD-HHMMSS-<stage>, in the
# capturing computer's local time.
CAPTURE_NAME = re.compile(r"^(\d{8}-\d{6})-")


def capture_time(directory: Path, tz: ZoneInfo) -> datetime:
    """When a capture was taken: from its name (read in `tz`), or its modification time."""
    match = CAPTURE_NAME.match(directory.name)
    if match:
        return datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=tz)
    return datetime.fromtimestamp(directory.stat().st_mtime, UTC)


def recent_captures(root: Path, since: datetime | None, tz: ZoneInfo) -> list[Path]:
    """Notification files of captures taken at or after `since` (all if None), oldest first.

    A capture only holds history recorded before it was taken, so a night ending at T
    is in no capture older than T. A re-read capture also holds older nights; those
    are recomputed only if they gain records (see `store_nights`).
    """
    found = [
        (capture_time(directory, tz), directory / NOTIFICATIONS_FILE)
        for directory in root.iterdir()
        if (directory / NOTIFICATIONS_FILE).is_file()
    ]
    return [path for taken, path in sorted(found) if since is None or taken >= since]


def _row(night: NightMetrics, decoder_version: str) -> dict[str, Any]:
    values = dataclasses.asdict(night)
    start = values.pop("sleep_start_unix")
    end = values.pop("sleep_end_unix")
    return {
        **values,
        "decoder_version": decoder_version,
        "sleep_start": datetime.fromtimestamp(start, UTC),
        "sleep_end": datetime.fromtimestamp(end, UTC),
    }


# Rows per INSERT statement; asyncpg allows at most 32,767 bind parameters.
CHUNK_ROWS = 1000


async def _upsert(
    session_factory: SessionFactory,
    table: type[BleNight] | type[BleMinute],
    keys: set[str],
    rows: Sequence[dict[str, Any]],
) -> int:
    """Insert rows, replacing an existing row only if the new one saw at least as many
    seconds of records, so a partial capture never overwrites one computed from fuller
    data. Returns how many rows were inserted or changed."""
    count = 0
    async with session_factory() as session, session.begin():
        for offset in range(0, len(rows), CHUNK_ROWS):
            chunk = rows[offset : offset + CHUNK_ROWS]
            statement = insert(table).values(list(chunk))
            update = {c: statement.excluded[c] for c in chunk[0] if c not in keys} | {
                "computed_at": func.now()
            }
            statement = statement.on_conflict_do_update(
                index_elements=sorted(keys),
                set_=update,
                where=statement.excluded.seconds_recorded >= table.seconds_recorded,
            )
            result = await session.execute(statement)
            count += result.rowcount  # type: ignore[attr-defined]
    return count


async def store_nights(
    session_factory: SessionFactory, nights: Sequence[NightMetrics], decoder_version: str
) -> int:
    """Upsert nights; returns how many rows were inserted or changed (see `_upsert`)."""
    rows = [_row(night, decoder_version) for night in nights]
    return await _upsert(session_factory, BleNight, {"wake_date", "metrics_version"}, rows)


async def store_minutes(
    session_factory: SessionFactory, summaries: Sequence[Minute], decoder_version: str
) -> int:
    """Upsert minutes; returns how many rows were inserted or changed (see `_upsert`)."""
    rows = []
    for summary in summaries:
        values = dataclasses.asdict(summary)
        start = values.pop("start_unix")
        rows.append(
            {
                **values,
                "decoder_version": decoder_version,
                "minute": datetime.fromtimestamp(start, UTC),
            }
        )
    return await _upsert(session_factory, BleMinute, {"minute", "minutes_version"}, rows)


def since_days(days: int | None, now: datetime | None = None) -> datetime | None:
    if days is None:
        return None
    return (now or datetime.now(UTC)) - timedelta(days=days)


def describe_files(files: Iterable[Path]) -> str:
    names = [path.parent.name for path in files]
    return ", ".join(names) if names else "none"
