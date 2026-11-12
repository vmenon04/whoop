from __future__ import annotations

import pytest
from pydantic import ValidationError

from whoop_lab.config import Settings


def make(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "whoop_client_id": "cid",
        "whoop_client_secret": "secret",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_redirect_uri_is_built_from_public_base_url() -> None:
    settings = make(public_base_url="https://example.test/")
    assert settings.redirect_uri == "https://example.test/oauth/callback"


def test_secret_is_not_in_repr() -> None:
    assert "secret" not in repr(make().whoop_client_secret)


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make(local_timezone="Mars/Olympus")
