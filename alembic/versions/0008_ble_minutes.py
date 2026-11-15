"""Per-minute summaries of the band's Bluetooth records, for the whole day.

Revision ID: 0008
Revises: 0007

One row per minute and aggregation version. Grafana reads it (see GRAFANA_READABLE).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "ble_minutes",
        sa.Column("minute", TS, nullable=False),
        sa.Column("minutes_version", sa.Text(), nullable=False),
        sa.Column("decoder_version", sa.Text(), nullable=False),
        sa.Column("seconds_recorded", sa.Integer(), nullable=False),
        sa.Column("seconds_with_hr", sa.Integer(), nullable=False),
        sa.Column("hr_mean", sa.Float(), nullable=True),
        sa.Column("hr_min", sa.Integer(), nullable=True),
        sa.Column("hr_max", sa.Integer(), nullable=True),
        sa.Column("rr_intervals", sa.Integer(), nullable=False),
        sa.Column("band_state", sa.Text(), nullable=True),
        sa.Column("seconds_asleep", sa.Integer(), nullable=False),
        sa.Column("seconds_still", sa.Integer(), nullable=False),
        sa.Column("seconds_walk", sa.Integer(), nullable=False),
        sa.Column("seconds_run", sa.Integer(), nullable=False),
        sa.Column("steps", sa.Integer(), nullable=False),
        sa.Column("steps_hw", sa.Integer(), nullable=False),
        sa.Column("accel_mean_g", sa.Float(), nullable=True),
        sa.Column("skin_temp_c", sa.Float(), nullable=True),
        sa.Column("computed_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("minute", "minutes_version", name="pk_ble_minutes"),
    )


def downgrade() -> None:
    op.drop_table("ble_minutes")
