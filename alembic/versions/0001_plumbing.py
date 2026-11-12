"""Plumbing tables: oauth_token, webhook_events, sync_runs.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_token",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lifetime_seconds", sa.Integer(), nullable=False),
        sa.Column("whoop_user_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_oauth_token"),
        sa.CheckConstraint("id = 1", name="ck_oauth_token_single_row"),
    )
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_events"),
        sa.UniqueConstraint("trace_id", name="uq_webhook_events_trace_id"),
    )
    op.create_index(
        "ix_webhook_events_pending",
        "webhook_events",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rows_upserted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rows_deleted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_sync_runs"),
    )


def downgrade() -> None:
    op.drop_table("sync_runs")
    op.drop_index("ix_webhook_events_pending", table_name="webhook_events")
    op.drop_table("webhook_events")
    op.drop_table("oauth_token")
