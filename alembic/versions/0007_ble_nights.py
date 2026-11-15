"""Nightly metrics from the band's Bluetooth records.

Revision ID: 0007
Revises: 0006

One row per wake date and metrics version. Grafana reads it (see GRAFANA_READABLE).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "ble_nights",
        sa.Column("wake_date", sa.Date(), nullable=False),
        sa.Column("metrics_version", sa.Text(), nullable=False),
        sa.Column("decoder_version", sa.Text(), nullable=False),
        sa.Column("sleep_start", TS, nullable=False),
        sa.Column("sleep_end", TS, nullable=False),
        sa.Column("seconds_recorded", sa.Integer(), nullable=False),
        sa.Column("rr_coverage", sa.Float(), nullable=True),
        sa.Column("rr_corrected_share", sa.Float(), nullable=True),
        sa.Column("rr_quality_ok", sa.Boolean(), nullable=True),
        sa.Column("rmssd_sleep_ms", sa.Float(), nullable=True),
        sa.Column("rmssd_fixed_ms", sa.Float(), nullable=True),
        sa.Column("rmssd_segment_median_ms", sa.Float(), nullable=True),
        sa.Column("rmssd_segments", sa.Integer(), nullable=False),
        sa.Column("resting_hr_median", sa.Float(), nullable=True),
        sa.Column("resting_hr_lowest_segment", sa.Float(), nullable=True),
        sa.Column("respiratory_rate", sa.Float(), nullable=True),
        sa.Column("respiratory_windows", sa.Integer(), nullable=False),
        sa.Column("spo2_mean", sa.Float(), nullable=True),
        sa.Column("spo2_windows", sa.Integer(), nullable=False),
        sa.Column("skin_temp_mean_c", sa.Float(), nullable=True),
        sa.Column("computed_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("wake_date", "metrics_version", name="pk_ble_nights"),
    )


def downgrade() -> None:
    op.drop_table("ble_nights")
