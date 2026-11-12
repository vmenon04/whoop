"""Experiments, assignments, results, and the triggers that keep them honest.

Revision ID: 0004
Revises: 0003

The triggers are the backstop for the invariants in the experiment-analysis skill:
locked experiments are frozen, assignments are immutable apart from `complied`
and `note`, and results are append-only.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None

TS = sa.DateTime(timezone=True)

TRIGGERS = [
    """
    CREATE FUNCTION experiments_guard() RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            IF OLD.locked_at IS NOT NULL THEN
                RAISE EXCEPTION 'experiment % is locked and cannot be deleted', OLD.id;
            END IF;
            RETURN OLD;
        END IF;
        IF OLD.locked_at IS NOT NULL AND (
               NEW.id               IS DISTINCT FROM OLD.id
            OR NEW.name             IS DISTINCT FROM OLD.name
            OR NEW.hypothesis       IS DISTINCT FROM OLD.hypothesis
            OR NEW.intervention     IS DISTINCT FROM OLD.intervention
            OR NEW.control          IS DISTINCT FROM OLD.control
            OR NEW.outcome_metric   IS DISTINCT FROM OLD.outcome_metric
            OR NEW.analysis_spec    IS DISTINCT FROM OLD.analysis_spec
            OR NEW.block_size       IS DISTINCT FROM OLD.block_size
            OR NEW.washout_days     IS DISTINCT FROM OLD.washout_days
            OR NEW.planned_days     IS DISTINCT FROM OLD.planned_days
            OR NEW.stratify_weekend IS DISTINCT FROM OLD.stratify_weekend
            OR NEW.started_on       IS DISTINCT FROM OLD.started_on
            OR NEW.locked_at        IS DISTINCT FROM OLD.locked_at
        ) THEN
            RAISE EXCEPTION 'experiment % is locked: only status may change', OLD.id;
        END IF;
        RETURN NEW;
    END $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER experiments_guard BEFORE UPDATE OR DELETE ON experiments
    FOR EACH ROW EXECUTE FUNCTION experiments_guard()
    """,
    """
    CREATE FUNCTION assignments_guard() RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'assignments are never deleted';
        END IF;
        IF    NEW.id             IS DISTINCT FROM OLD.id
           OR NEW.experiment_id  IS DISTINCT FROM OLD.experiment_id
           OR NEW.local_date     IS DISTINCT FROM OLD.local_date
           OR NEW.arm            IS DISTINCT FROM OLD.arm
           OR NEW.stratum        IS DISTINCT FROM OLD.stratum
           OR NEW.block_index    IS DISTINCT FROM OLD.block_index
           OR NEW.block_position IS DISTINCT FROM OLD.block_position
           OR NEW.seed           IS DISTINCT FROM OLD.seed
           OR NEW.created_at     IS DISTINCT FROM OLD.created_at
        THEN
            RAISE EXCEPTION 'assignments are immutable: only complied and note may change';
        END IF;
        RETURN NEW;
    END $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER assignments_guard BEFORE UPDATE OR DELETE ON assignments
    FOR EACH ROW EXECUTE FUNCTION assignments_guard()
    """,
    """
    CREATE FUNCTION experiment_results_guard() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'experiment_results is append-only';
    END $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER experiment_results_guard BEFORE UPDATE OR DELETE ON experiment_results
    FOR EACH ROW EXECUTE FUNCTION experiment_results_guard()
    """,
]


def upgrade() -> None:
    op.create_table(
        "experiments",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("intervention", sa.Text(), nullable=False),
        sa.Column("control", sa.Text(), nullable=False),
        sa.Column("outcome_metric", sa.Text(), nullable=False),
        sa.Column("analysis_spec", postgresql.JSONB(), nullable=False),
        sa.Column("block_size", sa.Integer(), server_default="4", nullable=False),
        sa.Column("washout_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column("planned_days", sa.Integer(), nullable=False),
        sa.Column(
            "stratify_weekend", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("started_on", sa.Date(), nullable=True),
        sa.Column("locked_at", TS, nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_experiments"),
        sa.UniqueConstraint("name", name="uq_experiments_name"),
        sa.CheckConstraint(
            "block_size >= 2 AND block_size % 2 = 0", name="ck_experiments_block_size_even"
        ),
        sa.CheckConstraint("planned_days > 0", name="ck_experiments_planned_days_positive"),
        sa.CheckConstraint("washout_days >= 0", name="ck_experiments_washout_days_non_negative"),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'completed')", name="ck_experiments_status_known"
        ),
    )
    op.create_index(
        "uq_experiments_one_active",
        "experiments",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "assignments",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("experiment_id", sa.BigInteger(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("arm", sa.Text(), nullable=False),
        sa.Column("stratum", sa.Text(), nullable=False),
        sa.Column("block_index", sa.Integer(), nullable=False),
        sa.Column("block_position", sa.Integer(), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("complied", sa.Boolean(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_assignments"),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["experiments.id"], name="fk_assignments_experiment_id_experiments"
        ),
        sa.UniqueConstraint(
            "experiment_id", "local_date", name="uq_assignments_experiment_id_local_date"
        ),
        sa.UniqueConstraint(
            "experiment_id",
            "stratum",
            "block_index",
            "block_position",
            name="uq_assignments_experiment_id_stratum_block_index_block_position",
        ),
        sa.CheckConstraint("arm IN ('treatment', 'control')", name="ck_assignments_arm_known"),
    )
    op.create_table(
        "experiment_results",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("experiment_id", sa.BigInteger(), nullable=False),
        sa.Column("computed_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("outcome_metric", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("n_treatment", sa.Integer(), nullable=False),
        sa.Column("n_control", sa.Integer(), nullable=False),
        sa.Column("n_missing_treatment", sa.Integer(), nullable=False),
        sa.Column("n_missing_control", sa.Integer(), nullable=False),
        sa.Column("effect", sa.Float(), nullable=True),
        sa.Column("ci_low", sa.Float(), nullable=True),
        sa.Column("ci_high", sa.Float(), nullable=True),
        sa.Column("p_value", sa.Float(), nullable=True),
        sa.Column("compromised", sa.Boolean(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_experiment_results"),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
            name="fk_experiment_results_experiment_id_experiments",
        ),
    )
    op.create_index(
        "ix_experiment_results_experiment_id",
        "experiment_results",
        ["experiment_id", "computed_at"],
    )
    for statement in TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("experiment_results")
    op.drop_table("assignments")
    op.drop_table("experiments")
    for function in ("experiments_guard", "assignments_guard", "experiment_results_guard"):
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")
