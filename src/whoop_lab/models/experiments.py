"""Tables for the experiment layer. See the `experiment-analysis` skill before changing these."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from whoop_lab.models.base import Base


class AppConfig(Base):
    """Single row. Holds what SQL needs to know but cannot read from the environment."""

    __tablename__ = "app_config"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    local_timezone: Mapped[str] = mapped_column(Text)


class DailyTag(Base):
    """Things WHOOP cannot see, keyed by local calendar date."""

    __tablename__ = "daily_tags"

    local_date: Mapped[date] = mapped_column(Date, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


ARM_TREATMENT = "treatment"
ARM_CONTROL = "control"

STATUS_DRAFT = "draft"
STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"


class Experiment(Base):
    """One pre-registered experiment. After `locked_at` is set only `status` may change;
    the application refuses other edits and a database trigger enforces it."""

    __tablename__ = "experiments"
    __table_args__ = (
        CheckConstraint("block_size >= 2 AND block_size % 2 = 0", name="block_size_even"),
        CheckConstraint("planned_days > 0", name="planned_days_positive"),
        CheckConstraint("washout_days >= 0", name="washout_days_non_negative"),
        CheckConstraint("status IN ('draft', 'active', 'completed')", name="status_known"),
        Index(
            "uq_experiments_one_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    hypothesis: Mapped[str] = mapped_column(Text)
    intervention: Mapped[str] = mapped_column(Text)
    control: Mapped[str] = mapped_column(Text)
    outcome_metric: Mapped[str] = mapped_column(Text)
    analysis_spec: Mapped[dict[str, Any]]
    block_size: Mapped[int] = mapped_column(Integer, server_default="4")
    washout_days: Mapped[int] = mapped_column(Integer, server_default="0")
    planned_days: Mapped[int] = mapped_column(Integer)
    stratify_weekend: Mapped[bool] = mapped_column(server_default=text("false"))
    status: Mapped[str] = mapped_column(Text, server_default=STATUS_DRAFT)
    started_on: Mapped[date | None] = mapped_column(Date)
    locked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Assignment(Base):
    """The randomization record. Immutable except `complied` and `note` (trigger-enforced)."""

    __tablename__ = "assignments"
    __table_args__ = (
        UniqueConstraint("experiment_id", "local_date"),
        UniqueConstraint("experiment_id", "stratum", "block_index", "block_position"),
        CheckConstraint("arm IN ('treatment', 'control')", name="arm_known"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id"))
    local_date: Mapped[date] = mapped_column(Date)
    arm: Mapped[str] = mapped_column(Text)
    stratum: Mapped[str] = mapped_column(Text)
    block_index: Mapped[int] = mapped_column(Integer)
    block_position: Mapped[int] = mapped_column(Integer)
    seed: Mapped[int] = mapped_column(BigInteger)  # the block's seed
    complied: Mapped[bool | None]
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ExperimentResult(Base):
    """Append-only (trigger-enforced). One row per outcome and method per computation."""

    __tablename__ = "experiment_results"
    __table_args__ = (Index("ix_experiment_results_experiment_id", "experiment_id", "computed_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id"))
    computed_at: Mapped[datetime] = mapped_column(server_default=func.now())
    outcome_metric: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text)
    n_treatment: Mapped[int] = mapped_column(Integer)
    n_control: Mapped[int] = mapped_column(Integer)
    n_missing_treatment: Mapped[int] = mapped_column(Integer)
    n_missing_control: Mapped[int] = mapped_column(Integer)
    effect: Mapped[float | None] = mapped_column(Float)
    ci_low: Mapped[float | None] = mapped_column(Float)
    ci_high: Mapped[float | None] = mapped_column(Float)
    p_value: Mapped[float | None] = mapped_column(Float)
    compromised: Mapped[bool]
    details: Mapped[dict[str, Any]]
