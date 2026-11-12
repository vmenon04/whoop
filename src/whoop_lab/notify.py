"""Push notifications through ntfy. A no-op when NTFY_URL is not set."""

from __future__ import annotations

import logging

import httpx

from whoop_lab.config import Settings

log = logging.getLogger(__name__)


async def notify(
    settings: Settings,
    title: str,
    message: str,
    *,
    priority: str = "default",
    tags: str = "",
    http: httpx.AsyncClient | None = None,
) -> bool:
    """Send one notification. Never raises: an alert failure must not break a job."""
    if not settings.ntfy_url:
        log.info("notify (ntfy not configured): %s: %s", title, message)
        return False
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if settings.ntfy_token is not None:
        headers["Authorization"] = f"Bearer {settings.ntfy_token.get_secret_value()}"
    try:
        if http is None:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(settings.ntfy_url, content=message, headers=headers)
        else:
            response = await http.post(settings.ntfy_url, content=message, headers=headers)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("notify: ntfy delivery failed (%s)", type(exc).__name__)
        return False
    return True
