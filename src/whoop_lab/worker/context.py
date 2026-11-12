"""Shared handles passed to every worker job."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from whoop_lab.config import Settings
from whoop_lab.db import SessionFactory
from whoop_lab.whoop.client import WhoopClient


@dataclass(frozen=True)
class WorkerContext:
    settings: Settings
    session_factory: SessionFactory
    http: httpx.AsyncClient
    whoop: WhoopClient

    @classmethod
    def create(
        cls, settings: Settings, session_factory: SessionFactory, http: httpx.AsyncClient
    ) -> WorkerContext:
        return cls(settings, session_factory, http, WhoopClient(http, session_factory, settings))
