"""FastAPI application factory.

The api process serves pages, the OAuth flow and the webhook receiver. Apart
from the one-time authorization-code exchange it never calls WHOOP.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from whoop_lab.api.routes import health, oauth
from whoop_lab.config import Settings, get_settings
from whoop_lab.db import SessionFactory, make_engine, make_session_factory
from whoop_lab.log import configure_logging


def create_app(
    settings: Settings | None = None, session_factory: SessionFactory | None = None
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Used once, for the authorization-code exchange in /oauth/callback.
        async with httpx.AsyncClient() as http:
            app.state.http = http
            if session_factory is not None:
                app.state.session_factory = session_factory
                yield
                return
            configure_logging(settings.log_level)
            engine = make_engine(settings.database_url)
            app.state.session_factory = make_session_factory(engine)
            try:
                yield
            finally:
                await engine.dispose()

    app = FastAPI(title="whoop-lab", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.include_router(health.router)
    app.include_router(oauth.router)
    return app
