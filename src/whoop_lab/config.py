"""Runtime configuration, read from environment variables."""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

WHOOP_SCOPES = (
    "offline",
    "read:recovery",
    "read:cycles",
    "read:sleep",
    "read:workout",
    "read:profile",
    "read:body_measurement",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(description="postgresql+asyncpg://user:pass@host:5432/db")

    whoop_client_id: str
    whoop_client_secret: SecretStr
    public_base_url: str = "https://whoop.vasumenon.com"

    whoop_auth_url: str = "https://api.prod.whoop.com/oauth/oauth2/auth"
    whoop_token_url: str = "https://api.prod.whoop.com/oauth/oauth2/token"
    whoop_api_base_url: str = "https://api.prod.whoop.com/developer"

    local_timezone: str = "America/New_York"

    ntfy_url: str | None = Field(default=None, description="Full topic URL, e.g. https://ntfy.sh/x")
    ntfy_token: SecretStr | None = None

    # Token refresh: check often, refresh only when little life remains.
    token_refresh_check_seconds: int = 300
    token_refresh_threshold_seconds: int = 1200

    # Webhook queue.
    webhook_max_skew_seconds: int = 300
    event_poll_seconds: int = 5
    event_batch_size: int = 10
    event_max_attempts: int = 8
    event_backoff_base_seconds: int = 30

    # Polling and reconciliation.
    reconcile_interval_hours: int = 3
    reconcile_window_days: int = 7

    log_level: str = "INFO"

    @field_validator("public_base_url", "whoop_api_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("local_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value

    @field_validator("ntfy_url", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url}/oauth/callback"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.local_timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
