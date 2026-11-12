"""Plumbing tables: the token row, the webhook inbox and the sync audit log."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, Index, Integer, SmallInteger, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from whoop_lab.models.base import Base

TOKEN_ACTIVE = "active"  # noqa: S105
TOKEN_REAUTH_REQUIRED = "reauth_required"  # noqa: S105

EVENT_PENDING = "pending"
EVENT_DONE = "done"
EVENT_FAILED = "failed"


class OAuthToken(Base):
    """Exactly one row (id = 1). Written by the code exchange and the worker's refresh job."""

    __tablename__ = "oauth_token"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime]
    lifetime_seconds: Mapped[int] = mapped_column(Integer)
    whoop_user_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text, server_default=TOKEN_ACTIVE)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    def __repr__(self) -> str:  # never include the tokens
        return f"OAuthToken(status={self.status!r}, expires_at={self.expires_at!r})"


class WebhookEvent(Base):
    """Durable inbox and work queue for WHOOP webhooks."""

    __tablename__ = "webhook_events"
    __table_args__ = (
        Index(
            "ix_webhook_events_pending",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trace_id: Mapped[str] = mapped_column(Text, unique=True)
    type: Mapped[str] = mapped_column(Text)
    resource_id: Mapped[str] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[dict[str, Any]]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    status: Mapped[str] = mapped_column(Text, server_default=EVENT_PENDING)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    processed_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)


class SyncRun(Base):
    """One row per backfill, reconcile or manual sync. Feeds the data-health panel."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
    rows_upserted: Mapped[int] = mapped_column(Integer, server_default="0")
    rows_deleted: Mapped[int] = mapped_column(Integer, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
