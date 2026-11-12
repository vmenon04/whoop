"""Generate Grafana dashboard JSON. Edit this file, not the JSON.

uv run python scripts/gen_dashboards.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DS = {"type": "postgres", "uid": "whoop-pg"}
OUT = Path(__file__).resolve().parent.parent / "grafana" / "dashboards"

Panel = dict[str, Any]


def target(sql: str, fmt: str = "table") -> dict[str, Any]:
    return {"datasource": DS, "rawSql": sql, "format": fmt, "rawQuery": True, "refId": "A"}


def stat(
    title: str, sql: str, *, unit: str = "none", steps: list[tuple[float | None, str]]
) -> Panel:
    return {
        "type": "stat",
        "title": title,
        "datasource": DS,
        "targets": [target(sql)],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "decimals": 0,
                "noValue": "none",
                "thresholds": {
                    "mode": "absolute",
                    "steps": [{"value": value, "color": color} for value, color in steps],
                },
            },
            "overrides": [],
        },
        "options": {
            "colorMode": "value",
            "graphMode": "none",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
    }


def series(title: str, sql: str, *, bars: bool = False, unit: str = "none", **custom: Any) -> Panel:
    return {
        "type": "timeseries",
        "title": title,
        "datasource": DS,
        "targets": [target(sql, "time_series")],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {
                    "drawStyle": "bars" if bars else "line",
                    "fillOpacity": 60 if bars else 0,
                    "showPoints": "never" if bars else "always",
                    "pointSize": 4,
                    "spanNulls": False,
                    **custom,
                },
            },
            "overrides": [],
        },
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
    }


def table(title: str, sql: str) -> Panel:
    return {
        "type": "table",
        "title": title,
        "datasource": DS,
        "targets": [target(sql)],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {"cellHeight": "sm"},
    }


def layout(rows: list[tuple[int, list[Panel]]]) -> list[Panel]:
    """Each row is (height, panels). Panels share the 24-column width equally."""
    placed: list[Panel] = []
    y = 0
    for height, panels in rows:
        width = 24 // len(panels)
        for index, panel in enumerate(panels):
            placed.append(
                {
                    **panel,
                    "id": len(placed) + 1,
                    "gridPos": {"h": height, "w": width, "x": index * width, "y": y},
                }
            )
        y += height
    return placed


def dashboard(uid: str, title: str, panels: list[Panel], *, days: int) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "tags": ["whoop-lab"],
        "timezone": "browser",
        "schemaVersion": 39,
        "editable": False,
        "refresh": "5m",
        "time": {"from": f"now-{days}d", "to": "now"},
        "panels": panels,
    }


OK_THEN_BAD = [(None, "green"), (1, "orange"), (5, "red")]

DATA_HEALTH = dashboard(
    "whoop-data-health",
    "Data health",
    layout(
        [
            (
                4,
                [
                    stat(
                        "Hours since last scored recovery",
                        "SELECT extract(epoch FROM now() - max(outcome_sleep_end)) / 3600 "
                        "FROM daily_outcomes WHERE outcome_scored",
                        unit="h",
                        steps=[(None, "green"), (30, "orange"), (40, "red")],
                    ),
                    stat(
                        "Hours since last clean sync",
                        "SELECT extract(epoch FROM now() - max(finished_at)) / 3600 "
                        "FROM sync_runs WHERE error IS NULL AND finished_at IS NOT NULL",
                        unit="h",
                        steps=[(None, "green"), (4, "orange"), (8, "red")],
                    ),
                    stat(
                        "Webhook events waiting",
                        "SELECT count(*) FROM webhook_events WHERE status = 'pending'",
                        steps=OK_THEN_BAD,
                    ),
                    stat(
                        "Webhook events failed",
                        "SELECT count(*) FROM webhook_events WHERE status = 'failed'",
                        steps=[(None, "green"), (1, "red")],
                    ),
                ],
            ),
            (
                7,
                [
                    series(
                        "Mornings with a scored recovery (1 = yes)",
                        "SELECT (local_date + 1)::timestamp AS time, outcome_scored::int AS scored "
                        "FROM daily_outcomes "
                        "WHERE $__timeFilter((local_date + 1)::timestamp) "
                        "AND local_date < (now() AT TIME ZONE (SELECT local_timezone FROM app_config))::date "
                        "ORDER BY 1",
                        bars=True,
                    ),
                    series(
                        "Webhook events per day",
                        "SELECT date_trunc('day', received_at) AS time, type AS metric, count(*) AS events "
                        "FROM webhook_events WHERE $__timeFilter(received_at) GROUP BY 1, 2 ORDER BY 1",
                        bars=True,
                        stacking={"mode": "normal"},
                    ),
                ],
            ),
            (
                7,
                [
                    series(
                        "Nightly HRV (ms)",
                        "SELECT (local_date + 1)::timestamp AS time, hrv_rmssd_milli AS hrv "
                        "FROM daily_outcomes WHERE outcome_scored "
                        "AND $__timeFilter((local_date + 1)::timestamp) ORDER BY 1",
                        unit="ms",
                    ),
                    series(
                        "Resting heart rate (bpm)",
                        "SELECT (local_date + 1)::timestamp AS time, resting_heart_rate AS rhr "
                        "FROM daily_outcomes WHERE outcome_scored "
                        "AND $__timeFilter((local_date + 1)::timestamp) ORDER BY 1",
                    ),
                ],
            ),
            (
                8,
                [
                    table(
                        "Recent syncs",
                        "SELECT started_at, kind, rows_upserted, rows_deleted, "
                        "round(extract(epoch FROM finished_at - started_at)) AS seconds, error "
                        "FROM sync_runs ORDER BY id DESC LIMIT 15",
                    ),
                    table(
                        "Rows by score state",
                        "SELECT 'cycles' AS record, score_state, count(*) AS rows FROM cycles GROUP BY 2 "
                        "UNION ALL SELECT 'sleeps', score_state, count(*) FROM sleeps "
                        "WHERE deleted_at IS NULL GROUP BY 2 "
                        "UNION ALL SELECT 'recoveries', score_state, count(*) FROM recoveries "
                        "WHERE deleted_at IS NULL GROUP BY 2 "
                        "UNION ALL SELECT 'workouts', score_state, count(*) FROM workouts "
                        "WHERE deleted_at IS NULL GROUP BY 2 ORDER BY 1, 2",
                    ),
                ],
            ),
            (
                6,
                [
                    table(
                        "Webhook events that failed or are retrying",
                        "SELECT received_at, type, resource_id, status, attempts, error "
                        "FROM webhook_events WHERE status <> 'done' ORDER BY received_at DESC LIMIT 20",
                    )
                ],
            ),
        ]
    ),
    days=30,
)

DASHBOARDS = {"data-health.json": DATA_HEALTH}

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, body in DASHBOARDS.items():
        (OUT / name).write_text(json.dumps(body, indent=2) + "\n")
        print(f"wrote {OUT / name}")
