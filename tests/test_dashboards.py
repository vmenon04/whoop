"""Every dashboard query runs as Grafana's read-only role.

This catches SQL errors and a table missing from GRAFANA_READABLE before Grafana does.
Grafana macros and variables are replaced with neutral SQL.
"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from tests.test_setup_db import PASSWORD, grafana_engine
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
