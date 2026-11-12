from __future__ import annotations

import httpx

from whoop_lab.api.app import create_app
from whoop_lab.config import Settings
from whoop_lab.db import SessionFactory


async def test_healthz_ok(settings: Settings, session_factory: SessionFactory) -> None:
    app = create_app(settings, session_factory)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://whoop.test"
        ) as client,
    ):
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
