"""Shared handles passed to every worker job."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from whoop_lab.config import Settings
from whoop_lab.db import SessionFactory


@dataclass(frozen=True)
class WorkerContext:
    settings: Settings
    session_factory: SessionFactory
    http: httpx.AsyncClient
