"""In-memory stand-ins for WHOOP, wired in through respx."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl

import httpx
from sqlalchemy.dialects.postgresql import insert

from whoop_lab.db import SessionFactory
from whoop_lab.models import OAuthToken


class FakeTokenServer:
    """Behaves like WHOOP's token URL: refresh tokens rotate and old ones die."""

    def __init__(self) -> None:
        self.current_refresh = "rt-0"
        self.counter = 0
        self.calls = 0
        self.delay = 0.0
        self.fail_with: int | None = None
        self.forms: list[dict[str, str]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        form = dict(parse_qsl(request.content.decode()))
        self.forms.append(form)
        if self.fail_with is not None:
            return httpx.Response(self.fail_with, json={"error": "server_error"})
        if (
            form.get("grant_type") == "refresh_token"
            and form.get("refresh_token") != self.current_refresh
        ):
            return httpx.Response(400, json={"error": "invalid_grant"})
        if self.delay:
            await asyncio.sleep(self.delay)
        self.counter += 1
        self.current_refresh = f"rt-{self.counter}"
        return httpx.Response(
            200,
            json={
                "access_token": f"at-{self.counter}",
                "refresh_token": self.current_refresh,
                "expires_in": 3600,
                "scope": "offline read:recovery",
                "token_type": "bearer",
            },
        )


async def seed_token(
    session_factory: SessionFactory,
    *,
    access: str = "at-0",
    refresh: str = "rt-0",
    expires_in: int = 3600,
    status: str = "active",
) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            insert(OAuthToken).values(
                id=1,
                access_token=access,
                refresh_token=refresh,
                scope="offline",
                expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
                lifetime_seconds=3600,
                status=status,
            )
        )


async def read_token(session_factory: SessionFactory) -> OAuthToken | None:
    async with session_factory() as session:
        return await session.get(OAuthToken, 1)
