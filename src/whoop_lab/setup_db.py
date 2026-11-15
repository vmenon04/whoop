"""Post-migration database setup: app_config and the read-only Grafana role.

Runs after `alembic upgrade head` on every start (the `migrate` service). Idempotent.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from whoop_lab.config import Settings
from whoop_lab.db import SessionFactory
from whoop_lab.models import AppConfig

log = logging.getLogger(__name__)

GRAFANA_ROLE = "grafana_ro"

# Whole tables and views Grafana may read. Never oauth_token.
GRAFANA_READABLE = (
    "cycles",
    "sleeps",
    "recoveries",
    "workouts",
    "sync_runs",
    "app_config",
    "daily_tags",
    "daily_outcomes",
    "experiments",
    "experiment_results",
    "ble_nights",
    "ble_minutes",
)
# Tables Grafana may read only in part.
GRAFANA_READABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    # No `seed`, `block_index` or `block_position`: with a block's seed anyone can
    # compute the arms of its days that have not been assigned yet.
    "assignments": (
        "id",
        "experiment_id",
        "local_date",
        "arm",
        "stratum",
        "complied",
        "note",
        "created_at",
    ),
}
# webhook_events without `payload`.
GRAFANA_EVENT_COLUMNS = (
    "id",
    "trace_id",
    "type",
    "resource_id",
    "received_at",
    "status",
    "attempts",
    "next_attempt_at",
    "processed_at",
    "error",
)


async def apply_app_config(session_factory: SessionFactory, settings: Settings) -> None:
    """Copy LOCAL_TIMEZONE into app_config, where the daily_outcomes view reads it."""
    async with session_factory() as session, session.begin():
        current = await session.get(AppConfig, 1)
        if current is not None and current.local_timezone == settings.local_timezone:
            return
        running = await session.scalar(
            text("SELECT name FROM experiments WHERE status = 'active' LIMIT 1")
        )
        if current is not None and running is not None:
            # Experiment days are local calendar dates. Moving the zone mid-experiment
            # would silently remap which morning belongs to which assigned day.
            raise RuntimeError(
                f"LOCAL_TIMEZONE changed from {current.local_timezone} to "
                f"{settings.local_timezone} while experiment {running!r} is running. "
                "Set it back, or wait until the experiment has completed."
            )
        values = {"local_timezone": settings.local_timezone}
        await session.execute(
            insert(AppConfig)
            .values(id=1, **values)
            .on_conflict_do_update(index_elements=["id"], set_=values)
        )
    log.info("setup: app_config.local_timezone = %s", settings.local_timezone)


async def _scalar(conn: AsyncConnection, sql: str, **params: object) -> object:
    return (await conn.execute(text(sql), params)).scalar()


async def ensure_grafana_role(session_factory: SessionFactory, password: str) -> None:
    """Create or update a login role that can only SELECT analysis data."""
    async with session_factory() as session, session.begin():
        conn = await session.connection()
        exists = await _scalar(conn, "SELECT 1 FROM pg_roles WHERE rolname = :r", r=GRAFANA_ROLE)
        verb = "ALTER" if exists else "CREATE"
        # DDL cannot take bind parameters, so let Postgres quote the literal.
        statement = await _scalar(
            conn,
            "SELECT format('%s ROLE %I LOGIN PASSWORD %L', CAST(:verb AS text), "
            "CAST(:role AS text), CAST(:pw AS text))",
            verb=verb,
            role=GRAFANA_ROLE,
            pw=password,
        )
        await conn.exec_driver_sql(str(statement))
        database = await _scalar(conn, "SELECT format('%I', current_database())")
        await conn.exec_driver_sql(f"GRANT CONNECT ON DATABASE {database} TO {GRAFANA_ROLE}")
        await conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {GRAFANA_ROLE}")
        await conn.exec_driver_sql(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {GRAFANA_ROLE}")
        present = set(
            (
                await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
            ).scalars()
        )
        for name in GRAFANA_READABLE:
            if name in present:
                await conn.exec_driver_sql(f"GRANT SELECT ON {name} TO {GRAFANA_ROLE}")
        partial = {**GRAFANA_READABLE_COLUMNS, "webhook_events": GRAFANA_EVENT_COLUMNS}
        for name, names in partial.items():
            if name in present:
                columns = ", ".join(names)
                await conn.exec_driver_sql(f"GRANT SELECT ({columns}) ON {name} TO {GRAFANA_ROLE}")
    log.info("setup: role %s is ready", GRAFANA_ROLE)


async def setup_database(session_factory: SessionFactory, settings: Settings) -> None:
    await apply_app_config(session_factory, settings)
    if settings.grafana_db_password is not None:
        await ensure_grafana_role(session_factory, settings.grafana_db_password.get_secret_value())
    else:
        log.info("setup: GRAFANA_DB_PASSWORD not set; skipping the Grafana role")
