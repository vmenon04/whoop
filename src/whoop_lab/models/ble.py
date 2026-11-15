"""Nightly metrics computed from the band's own Bluetooth records. See the `whoop-ble` skill."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, Float, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from whoop_lab.models.base import Base


class BleNight(Base):
    """One night, as computed by one version of `ble.metrics`.

    Keyed by wake date and metrics version, so results from different definitions sit
    side by side and are never mixed. Written only by `ble.store.store_nights`.
    """

    __tablename__ = "ble_nights"

    wake_date: Mapped[date] = mapped_column(Date, primary_key=True)
    metrics_version: Mapped[str] = mapped_column(Text, primary_key=True)
    decoder_version: Mapped[str] = mapped_column(Text)
    sleep_start: Mapped[datetime]
    sleep_end: Mapped[datetime]
    seconds_recorded: Mapped[int] = mapped_column(Integer)
    rr_coverage: Mapped[float | None] = mapped_column(Float)
    rr_corrected_share: Mapped[float | None] = mapped_column(Float)
    rr_quality_ok: Mapped[bool | None]
    rmssd_sleep_ms: Mapped[float | None] = mapped_column(Float)
    rmssd_fixed_ms: Mapped[float | None] = mapped_column(Float)
    rmssd_segment_median_ms: Mapped[float | None] = mapped_column(Float)
    rmssd_segments: Mapped[int] = mapped_column(Integer)
    resting_hr_median: Mapped[float | None] = mapped_column(Float)
    resting_hr_lowest_segment: Mapped[float | None] = mapped_column(Float)
    respiratory_rate: Mapped[float | None] = mapped_column(Float)
    respiratory_windows: Mapped[int] = mapped_column(Integer)
    spo2_mean: Mapped[float | None] = mapped_column(Float)
    spo2_windows: Mapped[int] = mapped_column(Integer)
    skin_temp_mean_c: Mapped[float | None] = mapped_column(Float)
    computed_at: Mapped[datetime] = mapped_column(server_default=func.now())
