"""The worker's capture watcher. Empty synthetic captures; no real data."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from whoop_lab.ble.capture import NOTIFICATIONS_FILE
from whoop_lab.ble.importer import CaptureWatcher
from whoop_lab.db import SessionFactory

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 6, 11, 12, tzinfo=UTC)


def capture(root: Path, name: str, content: str = "") -> Path:
    directory = root / name
    directory.mkdir()
    path = directory / NOTIFICATIONS_FILE
    path.write_text(content)
    return path


async def test_imports_only_when_captures_change(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    watcher = CaptureWatcher(tmp_path, days=3, tz=NY)
    assert await watcher.check(session_factory, NOW) is None  # nothing copied yet

    capture(tmp_path, "20260610-080000-drain")
    capture(tmp_path, "20260601-080000-drain")  # older than three days: ignored
    first = await watcher.check(session_factory, NOW)
    assert first is not None
    assert [path.parent.name for path in first.files] == ["20260610-080000-drain"]
    assert await watcher.check(session_factory, NOW) is None  # unchanged

    capture(tmp_path, "20260611-080000-drain")
    second = await watcher.check(session_factory, NOW)
    assert second is not None
    assert len(second.files) == 2


async def test_a_missing_directory_is_not_an_error(session_factory: SessionFactory) -> None:
    watcher = CaptureWatcher(Path("/nonexistent/captures"), days=3, tz=NY)
    assert await watcher.check(session_factory, NOW) is None
