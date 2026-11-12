"""The only code that calls the WHOOP data API.

Responsibilities: bearer auth from the stored token, pagination, retry with
backoff, and rate-limit handling. It never refreshes the token. On a 401 it
re-reads the token row once (the refresh job may have rotated it mid-flight)
and otherwise gives up.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import httpx
from sqlalchemy import select

from whoop_lab.config import Settings
from whoop_lab.db import SessionFactory
from whoop_lab.models.plumbing import TOKEN_ACTIVE, OAuthToken

log = logging.getLogger(__name__)

Collection = Literal["cycle", "sleep", "recovery", "workout"]
COLLECTION_PATHS: dict[Collection, str] = {
    "cycle": "/v2/cycle",
    "sleep": "/v2/activity/sleep",
    "recovery": "/v2/recovery",
    "workout": "/v2/activity/workout",
}
PAGE_LIMIT = 25  # the maximum the API accepts
MAX_RETRIES = 5
MAX_RATE_LIMIT_WAIT_SECONDS = 900.0
DEFAULT_429_WAIT_SECONDS = 60.0

type Record = dict[str, Any]


class WhoopError(Exception):
    """Base class for client failures."""


class WhoopAuthError(WhoopError):
    """No usable token. Retrying will not help until the token row changes."""


class WhoopNotFoundError(WhoopError):
    """404: the resource does not exist (any more)."""


class WhoopRateLimitedError(WhoopError):
    """The limit will not reset soon enough to wait for."""


class WhoopAPIError(WhoopError):
    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _header_number(response: httpx.Response, name: str) -> float | None:
    try:
        return float(response.headers[name])
    except (KeyError, ValueError):
        return None


class WhoopClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        session_factory: SessionFactory,
        settings: Settings,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = http
        self._session_factory = session_factory
        self._base_url = settings.whoop_api_base_url
        self._sleep = sleep
        self._remaining: float | None = None
        self._reset_at: float = 0.0
        self.request_count = 0

    # --- resources -----------------------------------------------------------------

    async def get_sleep(self, sleep_id: UUID | str) -> Record:
        return await self._get(f"/v2/activity/sleep/{sleep_id}")

    async def get_workout(self, workout_id: UUID | str) -> Record:
        return await self._get(f"/v2/activity/workout/{workout_id}")

    async def get_cycle(self, cycle_id: int) -> Record:
        return await self._get(f"/v2/cycle/{cycle_id}")

    async def get_recovery_for_cycle(self, cycle_id: int) -> Record:
        """The API has no recovery-by-sleep endpoint; a recovery is reached through its cycle."""
        return await self._get(f"/v2/cycle/{cycle_id}/recovery")

    async def get_profile(self) -> Record:
        return await self._get("/v2/user/profile/basic")

    async def iter_collection(
        self,
        collection: Collection,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> AsyncIterator[Record]:
        """Yield every record in the window, following next_token to the end."""
        params: dict[str, str | int] = {"limit": PAGE_LIMIT}
        if start is not None:
            params["start"] = _iso(start)
        if end is not None:
            params["end"] = _iso(end)
        while True:
            page = await self._get(COLLECTION_PATHS[collection], params)
            for record in page.get("records") or []:
                yield record
            next_token = page.get("next_token")
            if not next_token:
                return
            params["nextToken"] = next_token

    # --- transport -----------------------------------------------------------------

    async def _access_token(self) -> str:
        async with self._session_factory() as session:
            row = (
                await session.execute(select(OAuthToken).where(OAuthToken.id == 1))
            ).scalar_one_or_none()
        if row is None:
            raise WhoopAuthError("no WHOOP token stored; run the OAuth flow")
        if row.status != TOKEN_ACTIVE:
            raise WhoopAuthError("WHOOP token needs re-authorization")
        return row.access_token

    async def _wait_for_budget(self) -> None:
        """Pause when the last response said the current window is spent."""
        if self._remaining is None or self._remaining > 1:
            return
        wait = self._reset_at - time.monotonic()
        if wait <= 0:
            return
        if wait > MAX_RATE_LIMIT_WAIT_SECONDS:
            raise WhoopRateLimitedError(f"rate limit resets in {wait:.0f}s")
        log.info("whoop: rate limit nearly spent, waiting %.0fs", wait)
        await self._sleep(wait + random.uniform(0.1, 1.0))  # noqa: S311
        self._remaining = None

    def _note_rate_limit(self, response: httpx.Response) -> None:
        remaining = _header_number(response, "X-RateLimit-Remaining")
        reset = _header_number(response, "X-RateLimit-Reset")
        if remaining is not None and reset is not None:
            self._remaining = remaining
            self._reset_at = time.monotonic() + reset

    async def _backoff(self, attempt: int) -> None:
        await self._sleep(min(2.0**attempt, 60.0) + random.uniform(0, 0.5))  # noqa: S311

    async def _get(self, path: str, params: dict[str, str | int] | None = None) -> Record:
        url = f"{self._base_url}{path}"
        attempt = 0
        reread_token = False
        while True:
            await self._wait_for_budget()
            token = await self._access_token()
            self.request_count += 1
            try:
                response = await self._http.get(
                    url, params=params, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.TransportError as exc:
                attempt += 1
                if attempt > MAX_RETRIES:
                    raise WhoopAPIError(None, f"{type(exc).__name__} on GET {path}") from None
                await self._backoff(attempt)
                continue

            self._note_rate_limit(response)
            status = response.status_code

            if status == 200:
                body = response.json()
                if not isinstance(body, dict):
                    raise WhoopAPIError(status, f"unexpected body on GET {path}")
                return body
            if status == 401:
                if reread_token:
                    raise WhoopAuthError(f"401 on GET {path} with the current token")
                reread_token = True
                continue
            if status == 404:
                raise WhoopNotFoundError(f"404 on GET {path}")
            if status == 429:
                attempt += 1
                wait = _header_number(response, "X-RateLimit-Reset") or DEFAULT_429_WAIT_SECONDS
                if attempt > MAX_RETRIES or wait > MAX_RATE_LIMIT_WAIT_SECONDS:
                    raise WhoopRateLimitedError(f"429 on GET {path}; reset in {wait:.0f}s")
                log.warning("whoop: 429 on GET %s, waiting %.0fs", path, wait)
                await self._sleep(wait + random.uniform(0.1, 1.0))  # noqa: S311
                self._remaining = None
                continue
            if status >= 500:
                attempt += 1
                if attempt > MAX_RETRIES:
                    raise WhoopAPIError(status, f"HTTP {status} on GET {path}")
                await self._backoff(attempt)
                continue
            raise WhoopAPIError(status, f"HTTP {status} on GET {path}")
