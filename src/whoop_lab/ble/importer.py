"""Import Bluetooth captures into `ble_nights` and `ble_minutes`.

Used by `whoop-lab ble-import` and by the worker, which imports whenever the captures
copied to the server change.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from whoop_lab.ble.decode import DECODER_VERSION, load_biometrics
from whoop_lab.ble.metrics import NightMetrics, nightly
from whoop_lab.ble.minutes import Minute, minutes
from whoop_lab.ble.store import recent_captures, since_days, store_minutes, store_nights
from whoop_lab.db import SessionFactory

# What identifies the captures' contents without reading them: path, size, mtime.
Fingerprint = tuple[tuple[str, int, int], ...]


@dataclass(frozen=True)
class ImportSummary:
    files: list[Path]
    records: int
    nights: list[NightMetrics]
    minutes: int
    nights_written: int
    minutes_written: int


def fingerprint(files: list[Path]) -> Fingerprint:
    stats = [(path, path.stat()) for path in files]
    return tuple((str(path), stat.st_size, stat.st_mtime_ns) for path, stat in stats)


def _compute(files: list[Path], tz: ZoneInfo) -> tuple[int, list[NightMetrics], list[Minute]]:
    records = load_biometrics(files)
    return len(records), nightly(records, tz), minutes(records)


async def import_captures(
    session_factory: SessionFactory, files: list[Path], tz: ZoneInfo
) -> ImportSummary:
    """Compute and store nights and minutes from these capture files.

    Decoding runs in a thread: a day of captures takes long enough to stall the worker's
    other jobs if it ran on the event loop.
    """
    count, nights, summaries = await asyncio.to_thread(_compute, files, tz)
    nights_written = await store_nights(session_factory, nights, DECODER_VERSION)
    minutes_written = await store_minutes(session_factory, summaries, DECODER_VERSION)
    return ImportSummary(files, count, nights, len(summaries), nights_written, minutes_written)


class CaptureWatcher:
    """Imports recent captures when they differ from what it last imported.

    The first check after start imports once; rows are upserted, so that is harmless.
    """

    def __init__(self, root: Path, days: int, tz: ZoneInfo) -> None:
        self.root = root
        self.days = days
        self.tz = tz
        self.seen: Fingerprint | None = None

    def recent(self, now: datetime | None = None) -> list[Path]:
        if not self.root.is_dir():
            return []
        return recent_captures(self.root, since_days(self.days, now), self.tz)

    async def check(
        self, session_factory: SessionFactory, now: datetime | None = None
    ) -> ImportSummary | None:
        files = self.recent(now)
        current = fingerprint(files)
        if not files or current == self.seen:
            return None
        summary = await import_captures(session_factory, files, self.tz)
        self.seen = current
        return summary
