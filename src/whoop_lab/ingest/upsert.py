"""Idempotent writes of WHOOP records: raw payload plus typed columns.

Field names come from the generated models, so a spec change shows up as a
validation or type error here, not as a silently empty column.

Write rule, applied in SQL so it holds under concurrency:
- newer `updated_at` than the stored row: overwrite (a rescore).
- same `updated_at`: overwrite only if the payload differs or the row was
  soft-deleted. An exact replay changes nothing, not even `fetched_at`.
- older `updated_at`: ignore (a slow fetch that lost a race).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from whoop_lab.models import Base, Cycle, Recovery, Sleep, Workout
from whoop_lab.whoop import generated as gen

type Record = dict[str, Any]

_SLEEP_NEEDED_PREFIX = "sleep_needed_"


def _base_columns(model: Any, names: tuple[str, ...]) -> dict[str, Any]:
    return {name: getattr(model, name) for name in names}


def _score_state(model: Any) -> str:
    return str(model.score_state.value)


def cycle_values(raw: Record) -> dict[str, Any]:
    m = gen.Cycle.model_validate(raw)
    score = m.score
    return {
        **_base_columns(
            m, ("id", "user_id", "created_at", "updated_at", "start", "end", "timezone_offset")
        ),
        "score_state": _score_state(m),
        "step_count": m.step_count,
        "strain": score.strain if score else None,
        "kilojoule": score.kilojoule if score else None,
        "average_heart_rate": score.average_heart_rate if score else None,
        "max_heart_rate": score.max_heart_rate if score else None,
        "raw": raw,
    }


def sleep_values(raw: Record) -> dict[str, Any]:
    m = gen.Sleep.model_validate(raw)
    score = m.score
    values: dict[str, Any] = {
        **_base_columns(
            m,
            (
                "id",
                "cycle_id",
                "user_id",
                "created_at",
                "updated_at",
                "start",
                "end",
                "timezone_offset",
                "nap",
            ),
        ),
        "score_state": _score_state(m),
        "raw": raw,
    }
    for name in gen.SleepStageSummary.model_fields:
        values[name] = getattr(score.stage_summary, name) if score else None
    for name in gen.SleepNeeded.model_fields:
        values[_SLEEP_NEEDED_PREFIX + name] = getattr(score.sleep_needed, name) if score else None
    for name in (
        "respiratory_rate",
        "sleep_performance_percentage",
        "sleep_consistency_percentage",
        "sleep_efficiency_percentage",
    ):
        values[name] = getattr(score, name) if score else None
    return values


def recovery_values(raw: Record) -> dict[str, Any]:
    m = gen.Recovery.model_validate(raw)
    score = m.score
    values: dict[str, Any] = {
        **_base_columns(m, ("sleep_id", "cycle_id", "user_id", "created_at", "updated_at")),
        "score_state": _score_state(m),
        "raw": raw,
    }
    for name in gen.RecoveryScore.model_fields:
        values[name] = getattr(score, name) if score else None
    return values


def workout_values(raw: Record) -> dict[str, Any]:
    m = gen.WorkoutV2.model_validate(raw)
    score = m.score
    values: dict[str, Any] = {
        **_base_columns(
            m,
            (
                "id",
                "user_id",
                "created_at",
                "updated_at",
                "start",
                "end",
                "timezone_offset",
                "sport_name",
            ),
        ),
        "score_state": _score_state(m),
        "raw": raw,
    }
    for name in gen.WorkoutScore.model_fields:
        if name == "zone_durations":
            continue
        values[name] = getattr(score, name) if score else None
    for name in gen.ZoneDurations.model_fields:
        values[name] = getattr(score.zone_durations, name) if score else None
    return values


async def _upsert(
    session: AsyncSession, table: type[Base], key: str, values: dict[str, Any]
) -> bool:
    """Returns True when a row was inserted or changed."""
    cols = table.__table__.c
    unknown = set(values) - set(cols.keys())
    if unknown:  # a spec field with no column: fail loudly rather than drop data silently
        raise ValueError(f"{table.__tablename__}: no column for {sorted(unknown)}")
    values = {**values, "fetched_at": datetime.now(UTC)}
    statement = insert(table).values(**values)
    excluded = statement.excluded
    changes: dict[str, Any] = {name: excluded[name] for name in values if name != key}
    changes["deleted_at"] = None
    upsert = statement.on_conflict_do_update(
        index_elements=[key],
        set_=changes,
        where=or_(
            cols.updated_at < excluded.updated_at,
            and_(
                cols.updated_at == excluded.updated_at,
                or_(cols.raw.is_distinct_from(excluded.raw), cols.deleted_at.is_not(None)),
            ),
        ),
    ).returning(cols[key])
    return (await session.execute(upsert)).first() is not None


async def upsert_cycle(session: AsyncSession, raw: Record) -> bool:
    return await _upsert(session, Cycle, "id", cycle_values(raw))


async def upsert_sleep(session: AsyncSession, raw: Record) -> bool:
    return await _upsert(session, Sleep, "id", sleep_values(raw))


async def upsert_recovery(session: AsyncSession, raw: Record) -> bool:
    return await _upsert(session, Recovery, "sleep_id", recovery_values(raw))


async def upsert_workout(session: AsyncSession, raw: Record) -> bool:
    return await _upsert(session, Workout, "id", workout_values(raw))


async def _soft_delete(session: AsyncSession, table: type[Base], key: str, value: Any) -> bool:
    cols = table.__table__.c
    statement = (
        update(table)
        .where(cols[key] == value, cols.deleted_at.is_(None))
        .values(deleted_at=datetime.now(UTC))
        .returning(cols[key])
    )
    return (await session.execute(statement)).first() is not None


async def soft_delete_recovery(session: AsyncSession, sleep_id: UUID) -> bool:
    return await _soft_delete(session, Recovery, "sleep_id", sleep_id)


async def soft_delete_sleep(session: AsyncSession, sleep_id: UUID) -> bool:
    """WHOOP deletes a recovery when its sleep is deleted, so do the same."""
    deleted = await _soft_delete(session, Sleep, "id", sleep_id)
    await soft_delete_recovery(session, sleep_id)
    return deleted


async def soft_delete_workout(session: AsyncSession, workout_id: UUID) -> bool:
    return await _soft_delete(session, Workout, "id", workout_id)
