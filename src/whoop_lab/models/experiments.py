"""Tables for the experiment layer. See the `experiment-analysis` skill before changing these."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, SmallInteger, Text, func
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
