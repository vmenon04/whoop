"""Every dashboard query runs as Grafana's read-only role.

This catches SQL errors and a table missing from GRAFANA_READABLE before Grafana does.
Grafana macros and variables are replaced with neutral SQL.
"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from tests.test_setup_db import PASSWORD, grafana_engine
from whoop_lab.ble.minutes import MINUTES_VERSION
from whoop_lab.config import Settings
from whoop_lab.db import SessionFactory
from whoop_lab.setup_db import setup_database

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gen_dashboards.py"


def load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_dashboards", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def queries(board: dict[str, Any]) -> Iterator[tuple[str, str]]:
    for panel in board["panels"]:
        for target in panel.get("targets", []):
            yield panel["title"], target["rawSql"]
    for variable in board["templating"]["list"]:
        yield variable["name"], variable["query"]
    for note in board["annotations"]["list"]:
        yield note["name"], note["target"]["rawSql"]


def neutral(sql: str) -> str:
    sql = re.sub(r"\$__timeFilter\([^()]*(?:\([^()]*\)[^()]*)*\)", "TRUE", sql)
    return sql.replace("${experiment}", "0")


DASHBOARDS = load_generator().DASHBOARDS
CASES = [
    pytest.param(sql, id=f"{name}: {title}")
    for name, board in DASHBOARDS.items()
    for title, sql in queries(board)
]


async def test_neutral_replaces_every_macro() -> None:
    for param in CASES:
        (sql,) = param.values
        assert "$" not in neutral(str(sql))


@pytest.mark.parametrize("sql", CASES)
async def test_query_runs_as_grafana(
    sql: str, session_factory: SessionFactory, settings: Settings, database_url: str
) -> None:
    await setup_database(
        session_factory, settings.model_copy(update={"grafana_db_password": SecretStr(PASSWORD)})
    )
    engine = grafana_engine(database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(neutral(sql)))
    finally:
        await engine.dispose()


def panel_sql(board: str, title_start: str) -> str:
    for title, sql in queries(DASHBOARDS[board]):
        if title.startswith(title_start):
            return neutral(sql)
    raise KeyError(title_start)


async def test_cycle_comparison_pairs_only_cycles_the_band_covered(
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session, session.begin():
        for cycle_id, start in (
            (1, datetime(2026, 6, 10, 12, tzinfo=UTC)),
            (2, datetime(2026, 6, 10, 14, tzinfo=UTC)),
        ):
            await session.execute(
                text(
                    'INSERT INTO cycles (id, user_id, created_at, updated_at, start, "end", '
                    "timezone_offset, score_state, average_heart_rate, step_count, raw) "
                    "VALUES (:id, 1, now(), now(), :start, :end, "
                    "'+00:00', 'SCORED', 60, 500, '{}')"
                ),
                {"id": cycle_id, "start": start, "end": start + timedelta(hours=1)},
            )
        # Cycle 1 fully recorded by the band; cycle 2 only for its first ten minutes.
        await session.execute(
            text(
                "INSERT INTO ble_minutes (minute, minutes_version, decoder_version, "
                "seconds_recorded, seconds_with_hr, hr_mean, rr_intervals, seconds_asleep, "
                "seconds_still, seconds_walk, seconds_run, steps, steps_hw) "
                "SELECT m, :version, 'd', 60, 60, 62, 0, 0, 60, 0, 0, 8, 9 FROM "
                "generate_series(TIMESTAMPTZ '2026-06-10 12:00+00', "
                "TIMESTAMPTZ '2026-06-10 14:09+00', interval '1 minute') AS m "
                "WHERE m < '2026-06-10 13:00+00' OR m >= '2026-06-10 14:00+00'"
            ),
            {"version": MINUTES_VERSION},
        )
    async with session_factory() as session:
        heart_sql = panel_sql("nightly-health.json", "Average heart rate")
        steps_sql = panel_sql("nightly-health.json", "Steps per WHOOP")
        heart = (await session.execute(text(heart_sql))).all()
        steps = (await session.execute(text(steps_sql))).all()
    assert sorted((metric, value) for _, metric, value in heart) == [("Band", 62.0), ("WHOOP", 60)]
    assert sorted((metric, value) for _, metric, value in steps) == [
        ("Band hw_step_counter", 540),
        ("Band step_counter", 480),
        ("WHOOP", 500),
    ]
