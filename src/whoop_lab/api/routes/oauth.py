"""OAuth start and callback. Behind Cloudflare Access like every page."""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from whoop_lab.api.templating import templates
from whoop_lab.whoop.oauth import (
    STATE_LENGTH,
    OAuthError,
    build_authorize_url,
    exchange_code,
    new_state,
)

router = APIRouter(prefix="/oauth")
log = logging.getLogger(__name__)

STATE_COOKIE = "whoop_oauth_state"


@router.get("/start")
async def start(request: Request) -> Response:
    state = new_state()
    response = RedirectResponse(build_authorize_url(request.app.state.settings, state), 302)
    response.set_cookie(
        STATE_COOKIE,
        state,
        max_age=600,
        path="/oauth",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response


def _result(request: Request, *, ok: bool, reason: str = "", status: int = 200) -> Response:
    response: Response = templates.TemplateResponse(
        request, "oauth_result.html", {"ok": ok, "reason": reason}, status_code=status
    )
    response.delete_cookie(STATE_COOKIE, path="/oauth")
    return response


@router.get("/callback", response_class=HTMLResponse)
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    expected = request.cookies.get(STATE_COOKIE, "")
    if (
        not state
        or len(expected) != STATE_LENGTH
        or not hmac.compare_digest(state.encode(), expected.encode())
    ):
        return _result(request, ok=False, reason="State did not match. Start again.", status=400)
    if error or not code:
        return _result(
            request,
            ok=False,
            reason=f"WHOOP returned: {error or 'no authorization code'}",
            status=400,
        )
    try:
        await exchange_code(
            request.app.state.session_factory,
            request.app.state.http,
            request.app.state.settings,
            code,
        )
    except OAuthError as exc:
        log.error("oauth: code exchange failed (%s)", exc)
        return _result(request, ok=False, reason=f"Token exchange failed ({exc}).", status=502)
    return _result(request, ok=True)
