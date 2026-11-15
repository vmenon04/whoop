"""Generate Grafana dashboard JSON. Edit this file, not the JSON.

uv run python scripts/gen_dashboards.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from whoop_lab.ble.metrics import METRICS_VERSION
from whoop_lab.ble.minutes import MINUTES_VERSION

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


def state_timeline(title: str, sql: str, colors: dict[str, str]) -> Panel:
    return {
        "type": "state-timeline",
        "title": title,
        "datasource": DS,
        "targets": [target(sql)],
        "fieldConfig": {
            "defaults": {
                "mappings": [
                    {
                        "type": "value",
                        "options": {
                            value: {"color": color, "index": index}
                            for index, (value, color) in enumerate(colors.items())
                        },
                    }
                ],
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": [{"value": None, "color": "text"}]},
            },
            "overrides": [],
        },
        "options": {"mergeValues": True, "showValue": "never", "rowHeight": 0.8},
    }


def annotation(name: str, sql: str, color: str) -> dict[str, Any]:
    return {
        "name": name,
        "datasource": DS,
        "enable": True,
        "iconColor": color,
        "rawQuery": sql,
        "target": target(sql) | {"refId": "Anno"},
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


def dashboard(
    uid: str,
    title: str,
    panels: list[Panel],
    *,
    days: int,
    variables: list[dict[str, Any]] | None = None,
    annotations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "annotations": {"list": annotations or []},
        "templating": {"list": variables or []},
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

# `experiment_id::text = '${experiment}'` stays valid SQL when no experiment exists yet.
THIS = "experiment_id::text = '${experiment}'"
LATEST = f"{THIS} AND computed_at = (SELECT max(computed_at) FROM experiment_results WHERE {THIS})"
PRIMARY = f"{LATEST} AND method = 'itt_randomization'"

EXPERIMENT_VARIABLE = {
    "type": "query",
    "name": "experiment",
    "label": "Experiment",
    "datasource": DS,
    "query": "SELECT name AS __text, id::text AS __value FROM experiments "
    "WHERE locked_at IS NOT NULL ORDER BY id DESC",
    "refresh": 1,
    "sort": 0,
    "multi": False,
    "includeAll": False,
}

EXPERIMENT_RESULTS = dashboard(
    "whoop-experiment-results",
    "Experiment results",
    layout(
        [
            (
                4,
                [
                    stat(
                        "Primary estimate",
                        f"SELECT effect FROM experiment_results WHERE {PRIMARY}",
                        steps=[(None, "text")],
                    )
                    | {
                        "fieldConfig": {
                            "defaults": {
                                "decimals": 3,
                                "noValue": "none",
                                "thresholds": {
                                    "mode": "absolute",
                                    "steps": [{"value": None, "color": "text"}],
                                },
                            },
                            "overrides": [],
                        }
                    },
                    stat(
                        "p-value (randomization test)",
                        f"SELECT p_value FROM experiment_results WHERE {PRIMARY}",
                        steps=[(None, "text")],
                    )
                    | {
                        "fieldConfig": {
                            "defaults": {
                                "decimals": 3,
                                "noValue": "none",
                                "thresholds": {
                                    "mode": "absolute",
                                    "steps": [{"value": None, "color": "text"}],
                                },
                            },
                            "overrides": [],
                        }
                    },
                    stat(
                        "Days analysed (treatment + control)",
                        f"SELECT n_treatment + n_control FROM experiment_results WHERE {PRIMARY}",
                        steps=[(None, "text")],
                    ),
                    stat(
                        "Mornings missing",
                        "SELECT n_missing_treatment + n_missing_control "
                        f"FROM experiment_results WHERE {PRIMARY}",
                        steps=[(None, "green"), (1, "orange"), (6, "red")],
                    ),
                ],
            ),
            (
                9,
                [
                    series(
                        "Running primary estimate with its interval (the end date is fixed; this is not a stopping rule)",
                        'SELECT computed_at AS time, effect AS estimate, ci_low AS "interval low", '
                        'ci_high AS "interval high" FROM experiment_results '
                        f"WHERE {THIS} AND method = 'itt_randomization' ORDER BY 1",
                    )
                ],
            ),
            (
                8,
                [
                    series(
                        "Next-morning HRV by assigned arm (ms)",
                        "SELECT (a.local_date + 1)::timestamp AS time, a.arm AS metric, "
                        "o.hrv_rmssd_milli AS hrv FROM assignments a "
                        f"JOIN daily_outcomes o USING (local_date) WHERE a.{THIS} "
                        "AND o.outcome_scored ORDER BY 1",
                        unit="ms",
                        drawStyle="points",
                        pointSize=8,
                    ),
                    table(
                        "Assignments and compliance",
                        "SELECT local_date, arm, complied, note FROM assignments "
                        f"WHERE {THIS} ORDER BY local_date DESC",
                    ),
                ],
            ),
            (
                8,
                [
                    table(
                        "Latest results, every method (only the first row is the primary analysis)",
                        "SELECT outcome_metric, method, effect, ci_low, ci_high, p_value, n_treatment, "
                        "n_control, n_missing_treatment + n_missing_control AS missing, compromised "
                        f"FROM experiment_results WHERE {LATEST} ORDER BY id",
                    )
                ],
            ),
        ]
    ),
    days=60,
    variables=[EXPERIMENT_VARIABLE],
)

# Band metrics are shown for the current metrics version only: regenerate after a bump.
BAND = f"ble_nights WHERE metrics_version = '{METRICS_VERSION}'"
MINUTES = f"ble_minutes WHERE minutes_version = '{MINUTES_VERSION}'"
LOCAL_DAY = "(minute AT TIME ZONE (SELECT local_timezone FROM app_config))::date::timestamp"
# Grafana draws a gap, not a line, where minutes are missing.
GAPS = {"insertNulls": 120_000}
WAKE = "(local_date + 1)::timestamp"

# Per WHOOP cycle, WHOOP's value beside the band's over the same span. Only closed,
# scored cycles the band recorded for at least 95% of, so partial captures do not
# masquerade as disagreement.
CYCLE_BAND = (
    "SELECT c.start AS time, '{name}' AS metric, {band} AS value "
    f"FROM cycles c JOIN ble_minutes m ON m.minutes_version = '{MINUTES_VERSION}' "
    'AND m.minute >= c.start AND m.minute < c."end" '
    "WHERE c.deleted_at IS NULL AND c.score_state = 'SCORED' AND $__timeFilter(c.start) "
    'GROUP BY c.id, c.start, c."end" '
    'HAVING sum(m.seconds_recorded) >= 0.95 * extract(epoch FROM c."end" - c.start)'
)
CYCLE_CHECK = (
    "SELECT c.start AS time, 'WHOOP' AS metric, {whoop} AS value FROM cycles c "
    "WHERE c.deleted_at IS NULL AND c.score_state = 'SCORED' AND $__timeFilter(c.start) "
    'AND {whoop} IS NOT NULL AND c."end" IS NOT NULL '
    "AND (SELECT sum(seconds_recorded) FROM ble_minutes "
    f"WHERE minutes_version = '{MINUTES_VERSION}' "
    'AND minute >= c.start AND minute < c."end") '
    '>= 0.95 * extract(epoch FROM c."end" - c.start) '
    "UNION ALL " + CYCLE_BAND
)


def both(whoop_column: str, band_column: str) -> str:
    """WHOOP's value and the band's for each wake date, as two series."""
    return (
        f"SELECT {WAKE} AS time, 'WHOOP' AS metric, {whoop_column} AS value "
        f"FROM daily_outcomes WHERE outcome_scored AND $__timeFilter({WAKE}) "
        f"AND {whoop_column} IS NOT NULL "
        "UNION ALL "
        f"SELECT wake_date::timestamp, 'Band ({METRICS_VERSION})', {band_column} "
        f"FROM {BAND} AND $__timeFilter(wake_date::timestamp) AND {band_column} IS NOT NULL "
        "ORDER BY 1"
    )


NIGHTLY_HEALTH = dashboard(
    "whoop-nightly-health",
    "Nightly health",
    layout(
        [
            (
                4,
                [
                    stat(
                        "Days since the last band night",
                        f"SELECT current_date - max(wake_date) FROM {BAND}",
                        steps=[(None, "green"), (2, "orange"), (4, "red")],
                    ),
                    stat(
                        "Beats corrected, last band night (over 5% = poor)",
                        f"SELECT rr_corrected_share FROM {BAND} ORDER BY wake_date DESC LIMIT 1",
                        unit="percentunit",
                        steps=[(None, "green"), (0.05, "red")],
                    ),
                    stat(
                        "HRV last night, WHOOP (ms)",
                        "SELECT hrv_rmssd_milli FROM daily_outcomes WHERE outcome_scored "
                        "ORDER BY local_date DESC LIMIT 1",
                        unit="ms",
                        steps=[(None, "text")],
                    ),
                    stat(
                        "HRV last night, band (ms)",
                        f"SELECT rmssd_sleep_ms FROM {BAND} ORDER BY wake_date DESC LIMIT 1",
                        unit="ms",
                        steps=[(None, "text")],
                    ),
                ],
            ),
            (
                8,
                [
                    series(
                        "HRV, RMSSD over the night (ms). Different definitions: compare trends, not values",
                        both("hrv_rmssd_milli", "rmssd_sleep_ms"),
                        unit="ms",
                    ),
                    series(
                        "Resting heart rate (bpm); band: median over the sleep window",
                        both("resting_heart_rate", "resting_hr_median"),
                    ),
                ],
            ),
            (
                8,
                [
                    series(
                        "Respiratory rate (breaths per minute)",
                        both("respiratory_rate", "respiratory_rate"),
                    ),
                    series(
                        "Blood oxygen (%); band field disputed between sources",
                        both("spo2_percentage", "spo2_mean"),
                    ),
                ],
            ),
            (
                8,
                [
                    series(
                        "Skin temperature (C)",
                        both("skin_temp_celsius", "skin_temp_mean_c"),
                    ),
                    series(
                        "Beats corrected per night (over 5% = poor HRV quality)",
                        f"SELECT wake_date::timestamp AS time, rr_corrected_share AS corrected "
                        f"FROM {BAND} AND $__timeFilter(wake_date::timestamp) ORDER BY 1",
                        bars=True,
                        unit="percentunit",
                    ),
                ],
            ),
            (
                8,
                [
                    series(
                        "Steps per day (band counter; see the per-cycle check below)",
                        f"SELECT {LOCAL_DAY} AS time, sum(steps) AS steps "
                        f"FROM {MINUTES} AND $__timeFilter(minute) GROUP BY 1 ORDER BY 1",
                        bars=True,
                    ),
                    series(
                        "Active minutes per day (band: walking or running)",
                        f"SELECT {LOCAL_DAY} AS time, sum(seconds_walk) / 60.0 AS walking, "
                        "sum(seconds_run) / 60.0 AS running "
                        f"FROM {MINUTES} AND $__timeFilter(minute) GROUP BY 1 ORDER BY 1",
                        bars=True,
                        stacking={"mode": "normal"},
                    ),
                ],
            ),
            (
                8,
                [
                    series(
                        "Average heart rate per WHOOP cycle (cycles the band covered for 95%+)",
                        CYCLE_CHECK.format(
                            whoop="c.average_heart_rate",
                            band="sum(m.hr_mean * m.seconds_with_hr) / nullif(sum(m.seconds_with_hr), 0)",
                            name="Band",
                        )
                        + " ORDER BY 1",
                        drawStyle="points",
                        pointSize=8,
                    ),
                    series(
                        "Steps per WHOOP cycle: WHOOP vs the band's two counters (95%+ covered)",
                        CYCLE_CHECK.format(
                            whoop="c.step_count", band="sum(m.steps)", name="Band step_counter"
                        )
                        + " UNION ALL "
                        + CYCLE_BAND.format(band="sum(m.steps_hw)", name="Band hw_step_counter")
                        + " ORDER BY 1",
                        drawStyle="points",
                        pointSize=8,
                    ),
                ],
            ),
            (
                8,
                [
                    table(
                        "Band nights",
                        "SELECT wake_date, sleep_start, sleep_end, "
                        "round(rr_coverage::numeric, 3) AS rr_coverage, "
                        "round(rr_corrected_share::numeric, 3) AS corrected, rr_quality_ok, "
                        "round(rmssd_sleep_ms::numeric, 1) AS rmssd_sleep, "
                        "round(rmssd_fixed_ms::numeric, 1) AS rmssd_02_06, "
                        "round(resting_hr_median::numeric, 1) AS resting_hr, "
                        "round(respiratory_rate::numeric, 1) AS resp_rate, respiratory_windows, "
                        "round(spo2_mean::numeric, 1) AS spo2, "
                        "round(skin_temp_mean_c::numeric, 2) AS skin_temp, decoder_version, computed_at "
                        f"FROM {BAND} ORDER BY wake_date DESC LIMIT 30",
                    )
                ],
            ),
        ]
    ),
    days=30,
)

DAY_VIEW = dashboard(
    "whoop-day-view",
    "Day view",
    layout(
        [
            (
                5,
                [
                    state_timeline(
                        "Band state",
                        f"SELECT minute AS time, band_state AS state FROM {MINUTES} "
                        "AND $__timeFilter(minute) ORDER BY 1",
                        {
                            "SLEEP": "dark-blue",
                            "STILL": "light-blue",
                            "WAKE": "green",
                            "UP": "orange",
                        },
                    )
                ],
            ),
            (
                9,
                [
                    series(
                        "Heart rate per minute (bpm): mean, min and max",
                        'SELECT minute AS time, hr_mean AS "mean", hr_min AS "min", hr_max AS "max" '
                        f"FROM {MINUTES} AND $__timeFilter(minute) ORDER BY 1",
                        **GAPS,
                    )
                ],
            ),
            (
                8,
                [
                    series(
                        "Minutes walking and running, per 15 minutes",
                        "SELECT date_bin('15 minutes', minute, TIMESTAMPTZ '2000-01-01') AS time, "
                        "sum(seconds_walk) / 60.0 AS walking, sum(seconds_run) / 60.0 AS running "
                        f"FROM {MINUTES} AND $__timeFilter(minute) GROUP BY 1 ORDER BY 1",
                        bars=True,
                        stacking={"mode": "normal"},
                    ),
                    series(
                        "Steps per hour (band counter, being checked against WHOOP)",
                        "SELECT date_trunc('hour', minute) AS time, sum(steps) AS steps "
                        f"FROM {MINUTES} AND $__timeFilter(minute) GROUP BY 1 ORDER BY 1",
                        bars=True,
                    ),
                ],
            ),
            (
                8,
                [
                    series(
                        "Movement intensity per minute (g)",
                        f"SELECT minute AS time, accel_mean_g AS movement FROM {MINUTES} "
                        "AND $__timeFilter(minute) ORDER BY 1",
                        **GAPS,
                    ),
                    series(
                        "Skin temperature per minute (C)",
                        f"SELECT minute AS time, skin_temp_c AS skin FROM {MINUTES} "
                        "AND $__timeFilter(minute) ORDER BY 1",
                        **GAPS,
                    ),
                ],
            ),
        ]
    ),
    days=2,
    annotations=[
        annotation(
            "WHOOP workouts",
            'SELECT start AS time, "end" AS timeend, sport_name AS text FROM workouts '
            "WHERE deleted_at IS NULL AND $__timeFilter(start)",
            "orange",
        )
    ],
)

DASHBOARDS = {
    "data-health.json": DATA_HEALTH,
    "experiment-results.json": EXPERIMENT_RESULTS,
    "nightly-health.json": NIGHTLY_HEALTH,
    "day-view.json": DAY_VIEW,
}

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, body in DASHBOARDS.items():
        (OUT / name).write_text(json.dumps(body, indent=2) + "\n")
        print(f"wrote {OUT / name}")
