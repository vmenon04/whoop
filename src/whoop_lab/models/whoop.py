"""WHOOP data tables.

Shape shared by all four: WHOOP's ID as primary key, typed columns named as in
the OpenAPI spec (score fields flattened), and `raw` holding the full payload.
Score columns are nullable because `score` is absent unless score_state is SCORED.
No foreign keys: records arrive in any order.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, Float, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from whoop_lab.models.base import Base

big = BigInteger


class Cycle(Base):
    __tablename__ = "cycles"
    __table_args__ = (Index("ix_cycles_start", "start"),)

    id: Mapped[int] = mapped_column(big, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(big)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    start: Mapped[datetime]
    end: Mapped[datetime | None]  # absent while the cycle is still open
    timezone_offset: Mapped[str] = mapped_column(Text)
    score_state: Mapped[str] = mapped_column(Text)
    strain: Mapped[float | None] = mapped_column(Float)
    kilojoule: Mapped[float | None] = mapped_column(Float)
    average_heart_rate: Mapped[int | None] = mapped_column(Integer)
    max_heart_rate: Mapped[int | None] = mapped_column(Integer)
    step_count: Mapped[int | None] = mapped_column(Integer)
    raw: Mapped[dict[str, Any]]
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())
    deleted_at: Mapped[datetime | None]


class Sleep(Base):
    __tablename__ = "sleeps"
    __table_args__ = (Index("ix_sleeps_end", "end"), Index("ix_sleeps_cycle_id", "cycle_id"))

    id: Mapped[UUID] = mapped_column(primary_key=True)
    cycle_id: Mapped[int] = mapped_column(big)
    user_id: Mapped[int] = mapped_column(big)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    start: Mapped[datetime]
    end: Mapped[datetime]
    timezone_offset: Mapped[str] = mapped_column(Text)
    nap: Mapped[bool]
    score_state: Mapped[str] = mapped_column(Text)
    # score.stage_summary
    total_in_bed_time_milli: Mapped[int | None] = mapped_column(Integer)
    total_awake_time_milli: Mapped[int | None] = mapped_column(Integer)
    total_no_data_time_milli: Mapped[int | None] = mapped_column(Integer)
    total_light_sleep_time_milli: Mapped[int | None] = mapped_column(Integer)
    total_slow_wave_sleep_time_milli: Mapped[int | None] = mapped_column(Integer)
    total_rem_sleep_time_milli: Mapped[int | None] = mapped_column(Integer)
    sleep_cycle_count: Mapped[int | None] = mapped_column(Integer)
    disturbance_count: Mapped[int | None] = mapped_column(Integer)
    # score.sleep_needed
    sleep_needed_baseline_milli: Mapped[int | None] = mapped_column(big)
    sleep_needed_need_from_sleep_debt_milli: Mapped[int | None] = mapped_column(big)
    sleep_needed_need_from_recent_strain_milli: Mapped[int | None] = mapped_column(big)
    sleep_needed_need_from_recent_nap_milli: Mapped[int | None] = mapped_column(big)
    # score
    respiratory_rate: Mapped[float | None] = mapped_column(Float)
    sleep_performance_percentage: Mapped[float | None] = mapped_column(Float)
    sleep_consistency_percentage: Mapped[float | None] = mapped_column(Float)
    sleep_efficiency_percentage: Mapped[float | None] = mapped_column(Float)
    raw: Mapped[dict[str, Any]]
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())
    deleted_at: Mapped[datetime | None]


class Recovery(Base):
    """Keyed on the sleep it belongs to: that is the ID a v2 recovery webhook carries."""

    __tablename__ = "recoveries"
    __table_args__ = (Index("ix_recoveries_cycle_id", "cycle_id"),)

    sleep_id: Mapped[UUID] = mapped_column(primary_key=True)
    cycle_id: Mapped[int] = mapped_column(big)
    user_id: Mapped[int] = mapped_column(big)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    score_state: Mapped[str] = mapped_column(Text)
    user_calibrating: Mapped[bool | None]
    recovery_score: Mapped[float | None] = mapped_column(Float)
    resting_heart_rate: Mapped[float | None] = mapped_column(Float)
    hrv_rmssd_milli: Mapped[float | None] = mapped_column(Float)
    spo2_percentage: Mapped[float | None] = mapped_column(Float)
    skin_temp_celsius: Mapped[float | None] = mapped_column(Float)
    raw: Mapped[dict[str, Any]]
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())
    deleted_at: Mapped[datetime | None]


class Workout(Base):
    __tablename__ = "workouts"
    __table_args__ = (Index("ix_workouts_start", "start"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(big)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    start: Mapped[datetime]
    end: Mapped[datetime]
    timezone_offset: Mapped[str] = mapped_column(Text)
    sport_name: Mapped[str] = mapped_column(Text)
    score_state: Mapped[str] = mapped_column(Text)
    strain: Mapped[float | None] = mapped_column(Float)
    average_heart_rate: Mapped[int | None] = mapped_column(Integer)
    max_heart_rate: Mapped[int | None] = mapped_column(Integer)
    kilojoule: Mapped[float | None] = mapped_column(Float)
    percent_recorded: Mapped[float | None] = mapped_column(Float)
    distance_meter: Mapped[float | None] = mapped_column(Float)
    altitude_gain_meter: Mapped[float | None] = mapped_column(Float)
    altitude_change_meter: Mapped[float | None] = mapped_column(Float)
    # score.zone_durations
    zone_zero_milli: Mapped[int | None] = mapped_column(big)
    zone_one_milli: Mapped[int | None] = mapped_column(big)
    zone_two_milli: Mapped[int | None] = mapped_column(big)
    zone_three_milli: Mapped[int | None] = mapped_column(big)
    zone_four_milli: Mapped[int | None] = mapped_column(big)
    zone_five_milli: Mapped[int | None] = mapped_column(big)
    raw: Mapped[dict[str, Any]]
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())
    deleted_at: Mapped[datetime | None]
