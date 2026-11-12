"""WHOOP OAuth: authorize URL, code exchange and the scheduled refresh.

Refresh tokens rotate: every refresh returns a new refresh token and WHOOP
forgets the old one. Two rules follow, and both are enforced here.

1. Every read-then-write of the token row happens inside one transaction that
   holds TOKEN_LOCK_KEY, and the new pair is written in that same transaction.
2. A refresh that WHOOP rejects is never retried with the same token. The row
   is marked `reauth_required` and a human signs in again.
"""

from __future__ import annotations

import enum
import logging
import secrets
import string
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from whoop_lab.config import WHOOP_SCOPES, Settings
from whoop_lab.db import TOKEN_LOCK_KEY, SessionFactory, advisory_xact_lock
from whoop_lab.models.plumbing import TOKEN_ACTIVE, TOKEN_REAUTH_REQUIRED, OAuthToken

log = logging.getLogger(__name__)

STATE_LENGTH = 8  # WHOOP requires exactly eight characters
_STATE_ALPHABET = string.ascii_letters + string.digits
TOKEN_TIMEOUT = httpx.Timeout(30.0)


class OAuthError(Exception):
    """Base class. Messages never contain token material."""


class TokenRejectedError(OAuthError):
    """WHOOP refused the grant (4xx). Retrying with the same input cannot succeed."""


class TransientOAuthError(OAuthError):
    """Network failure, 429 or 5xx. The outcome on WHOOP's side may be unknown."""


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str = ""


class RefreshOutcome(enum.Enum):
    NO_TOKEN = "no_token"
    NOT_DUE = "not_due"
    REFRESHED = "refreshed"
    REAUTH_REQUIRED = "reauth_required"  # newly detected on this call
    AWAITING_REAUTH = "awaiting_reauth"  # already marked; nothing was sent to WHOOP
    TRANSIENT_FAILURE = "transient_failure"


@dataclass(frozen=True)
class RefreshResult:
    outcome: RefreshOutcome
    detail: str = ""


def new_state() -> str:
    return "".join(secrets.choice(_STATE_ALPHABET) for _ in range(STATE_LENGTH))


def build_authorize_url(settings: Settings, state: str) -> str:
    if len(state) != STATE_LENGTH:
        raise ValueError("state must be exactly 8 characters")
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.whoop_client_id,
            "redirect_uri": settings.redirect_uri,
            "scope": " ".join(WHOOP_SCOPES),
            "state": state,
        }
    )
    return f"{settings.whoop_auth_url}?{query}"


def _error_code(response: httpx.Response) -> str:
    """The OAuth `error` field if the body has one. Never the body itself."""
    try:
        body = response.json()
    except ValueError:
        return "unparseable_error_body"
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return str(body["error"])[:80]
    return "unknown_error"


async def _post_token(
    http: httpx.AsyncClient, settings: Settings, form: dict[str, str]
) -> TokenResponse:
    data = {
        **form,
        "client_id": settings.whoop_client_id,
        "client_secret": settings.whoop_client_secret.get_secret_value(),
    }
    try:
        response = await http.post(settings.whoop_token_url, data=data, timeout=TOKEN_TIMEOUT)
    except httpx.HTTPError as exc:
        raise TransientOAuthError(type(exc).__name__) from None
    if response.status_code == 429 or response.status_code >= 500:
        raise TransientOAuthError(f"HTTP {response.status_code}")
    if response.status_code != 200:
        raise TokenRejectedError(f"HTTP {response.status_code} {_error_code(response)}")
    try:
        return TokenResponse.model_validate(response.json())
    except (ValueError, ValidationError):
        # A 200 we cannot read. The old token may already be dead, so do not guess.
        raise TokenRejectedError("token response missing required fields") from None


async def _store(session: AsyncSession, token: TokenResponse, now: datetime) -> None:
    values = {
        "access_token": token.access_token,
        "refresh_token": token.refresh_token,
        "scope": token.scope,
        "expires_at": now + timedelta(seconds=token.expires_in),
        "lifetime_seconds": token.expires_in,
        "status": TOKEN_ACTIVE,
        "updated_at": now,
    }
    statement = insert(OAuthToken).values(id=1, **values)
    await session.execute(statement.on_conflict_do_update(index_elements=["id"], set_=values))


async def exchange_code(
    session_factory: SessionFactory, http: httpx.AsyncClient, settings: Settings, code: str
) -> None:
    """One-time authorization-code exchange. The only token call the api process makes."""
    async with session_factory() as session, session.begin():
        await advisory_xact_lock(session, TOKEN_LOCK_KEY)
        token = await _post_token(
            http,
            settings,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.redirect_uri,
            },
        )
        await _store(session, token, datetime.now(UTC))
    log.info("oauth: authorization code exchanged, token stored")


async def refresh_if_needed(
    session_factory: SessionFactory,
    http: httpx.AsyncClient,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> RefreshResult:
    """Refresh the token when little life remains. Worker only.

    Safe to call concurrently: the second caller waits on the advisory lock,
    then sees the fresh row and returns NOT_DUE without calling WHOOP.
    """
    async with session_factory() as session, session.begin():
        await advisory_xact_lock(session, TOKEN_LOCK_KEY)
        row = (
            await session.execute(select(OAuthToken).where(OAuthToken.id == 1).with_for_update())
        ).scalar_one_or_none()
        if row is None:
            return RefreshResult(RefreshOutcome.NO_TOKEN)
        if row.status != TOKEN_ACTIVE:
            return RefreshResult(RefreshOutcome.AWAITING_REAUTH)

        current = now or datetime.now(UTC)
        threshold = min(settings.token_refresh_threshold_seconds, row.lifetime_seconds // 2)
        if (row.expires_at - current).total_seconds() > threshold:
            return RefreshResult(RefreshOutcome.NOT_DUE)

        try:
            token = await _post_token(
                http,
                settings,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": row.refresh_token,
                    "scope": "offline",
                },
            )
        except TransientOAuthError as exc:
            log.warning("oauth: refresh failed transiently (%s); will try on the next tick", exc)
            return RefreshResult(RefreshOutcome.TRANSIENT_FAILURE, str(exc))
        except TokenRejectedError as exc:
            row.status = TOKEN_REAUTH_REQUIRED
            row.updated_at = current
            log.error("oauth: refresh rejected (%s); manual re-auth required", exc)
            return RefreshResult(RefreshOutcome.REAUTH_REQUIRED, str(exc))

        await _store(session, token, datetime.now(UTC) if now is None else now)
    log.info("oauth: token refreshed")
    return RefreshResult(RefreshOutcome.REFRESHED)
