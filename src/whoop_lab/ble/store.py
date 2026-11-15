"""Write nightly metrics to `ble_nights`, and pick which captures an import reads."""

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
from whoop_lab.db import SessionFactory
from whoop_lab.models import BleNight

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


async def store_nights(
    session_factory: SessionFactory, nights: Sequence[NightMetrics], decoder_version: str
) -> int:
    """Upsert nights; returns how many rows were inserted or changed.

    An existing row for the same night and metrics version is replaced only when the
    new computation saw at least as many seconds of sleep, so importing a partial
    capture never overwrites a night computed from fuller data.
    """
    if not nights:
        return 0
    rows = [_row(night, decoder_version) for night in nights]
    statement = insert(BleNight).values(rows)
    keys = {"wake_date", "metrics_version"}
    update = {column: statement.excluded[column] for column in rows[0] if column not in keys} | {
        "computed_at": func.now()
    }
    statement = statement.on_conflict_do_update(
        index_elements=sorted(keys),
        set_=update,
        where=statement.excluded.seconds_recorded >= BleNight.seconds_recorded,
    )
    async with session_factory() as session, session.begin():
        result = await session.execute(statement)
    count: int = result.rowcount  # type: ignore[attr-defined]
    return count


def since_days(days: int | None, now: datetime | None = None) -> datetime | None:
    if days is None:
        return None
    return (now or datetime.now(UTC)) - timedelta(days=days)


def describe_files(files: Iterable[Path]) -> str:
    names = [path.parent.name for path in files]
    return ", ".join(names) if names else "none"
