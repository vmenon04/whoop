"""Small request helpers shared by the page routes."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import HTTPException, Request

from whoop_lab.config import Settings


def settings_of(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def local_today(request: Request) -> date:
    return datetime.now(settings_of(request).tz).date()


async def same_origin(request: Request) -> None:
    """Refuse form posts that come from another site.

    Cloudflare Access authenticates the browser, not the page that sent the
    request, so a hostile page could otherwise post on the owner's behalf.
    """
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site not in ("same-origin", "none"):
        raise HTTPException(403, "cross-site request refused")
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") != settings_of(request).public_base_url:
        raise HTTPException(403, "cross-site request refused")
