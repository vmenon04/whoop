"""WHOOP data tables: cycles, sleeps, recoveries, workouts.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None

TS = sa.DateTime(timezone=True)


def _tail() -> list[sa.Column[object]]:
    return [
        sa.Column("raw", postgresql.JSONB(), nullable=False),
        sa.Column("fetched_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", TS, nullable=True),
    ]


def _nullable(names: list[str], type_: sa.types.TypeEngine[object]) -> list[sa.Column[object]]:
    return [sa.Column(name, type_, nullable=True) for name in names]


def upgrade() -> None:
    op.create_table(
        "cycles",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("start", TS, nullable=False),
        sa.Column("end", TS, nullable=True),
        sa.Column("timezone_offset", sa.Text(), nullable=False),
        sa.Column("score_state", sa.Text(), nullable=False),
        *_nullable(["strain", "kilojoule"], sa.Float()),
        *_nullable(["average_heart_rate", "max_heart_rate", "step_count"], sa.Integer()),
        *_tail(),
        sa.PrimaryKeyConstraint("id", name="pk_cycles"),
    )
    op.create_index("ix_cycles_start", "cycles", ["start"])

    op.create_table(
        "sleeps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cycle_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("start", TS, nullable=False),
        sa.Column("end", TS, nullable=False),
        sa.Column("timezone_offset", sa.Text(), nullable=False),
        sa.Column("nap", sa.Boolean(), nullable=False),
        sa.Column("score_state", sa.Text(), nullable=False),
        *_nullable(
            [
                "total_in_bed_time_milli",
                "total_awake_time_milli",
                "total_no_data_time_milli",
                "total_light_sleep_time_milli",
                "total_slow_wave_sleep_time_milli",
                "total_rem_sleep_time_milli",
                "sleep_cycle_count",
                "disturbance_count",
            ],
            sa.Integer(),
        ),
        *_nullable(
            [
                "sleep_needed_baseline_milli",
                "sleep_needed_need_from_sleep_debt_milli",
                "sleep_needed_need_from_recent_strain_milli",
                "sleep_needed_need_from_recent_nap_milli",
            ],
            sa.BigInteger(),
        ),
        *_nullable(
            [
                "respiratory_rate",
                "sleep_performance_percentage",
                "sleep_consistency_percentage",
                "sleep_efficiency_percentage",
            ],
            sa.Float(),
        ),
        *_tail(),
        sa.PrimaryKeyConstraint("id", name="pk_sleeps"),
    )
    op.create_index("ix_sleeps_end", "sleeps", ["end"])
    op.create_index("ix_sleeps_cycle_id", "sleeps", ["cycle_id"])

    op.create_table(
        "recoveries",
        sa.Column("sleep_id", sa.Uuid(), nullable=False),
        sa.Column("cycle_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("score_state", sa.Text(), nullable=False),
        sa.Column("user_calibrating", sa.Boolean(), nullable=True),
        *_nullable(
            [
                "recovery_score",
                "resting_heart_rate",
                "hrv_rmssd_milli",
                "spo2_percentage",
                "skin_temp_celsius",
            ],
            sa.Float(),
        ),
        *_tail(),
        sa.PrimaryKeyConstraint("sleep_id", name="pk_recoveries"),
    )
    op.create_index("ix_recoveries_cycle_id", "recoveries", ["cycle_id"])

    op.create_table(
        "workouts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("start", TS, nullable=False),
        sa.Column("end", TS, nullable=False),
        sa.Column("timezone_offset", sa.Text(), nullable=False),
        sa.Column("sport_name", sa.Text(), nullable=False),
        sa.Column("score_state", sa.Text(), nullable=False),
        sa.Column("strain", sa.Float(), nullable=True),
        *_nullable(["average_heart_rate", "max_heart_rate"], sa.Integer()),
        *_nullable(
            [
                "kilojoule",
                "percent_recorded",
                "distance_meter",
                "altitude_gain_meter",
                "altitude_change_meter",
            ],
            sa.Float(),
        ),
        *_nullable(
            [
                "zone_zero_milli",
                "zone_one_milli",
                "zone_two_milli",
                "zone_three_milli",
                "zone_four_milli",
                "zone_five_milli",
            ],
            sa.BigInteger(),
        ),
        *_tail(),
        sa.PrimaryKeyConstraint("id", name="pk_workouts"),
    )
    op.create_index("ix_workouts_start", "workouts", ["start"])


def downgrade() -> None:
    for table in ("workouts", "recoveries", "sleeps", "cycles"):
        op.drop_table(table)
