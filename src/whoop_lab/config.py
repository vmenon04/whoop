"""Runtime configuration, read from environment variables."""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL, make_url

from whoop_lab.db import build_database_url

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

    # Either the parts (what Compose passes; any password works) or a full, already
    # percent-encoded DATABASE_URL, which wins when both are set.
    database_url: SecretStr | None = None
    postgres_host: str = "db"
    postgres_port: int = 5432
    postgres_user: str | None = None
    postgres_password: SecretStr | None = None
    postgres_db: str | None = None

    whoop_client_id: str
    whoop_client_secret: SecretStr
    public_base_url: str = "https://whoop.vasumenon.com"

    whoop_auth_url: str = "https://api.prod.whoop.com/oauth/oauth2/auth"
    whoop_token_url: str = "https://api.prod.whoop.com/oauth/oauth2/token"
    whoop_api_base_url: str = "https://api.prod.whoop.com/developer"

    local_timezone: str = "America/New_York"

    ntfy_url: str | None = Field(default=None, description="Full topic URL, e.g. https://ntfy.sh/x")
    ntfy_token: SecretStr | None = None

    # Password for the read-only `grafana_ro` database role. Unset: the role is not created.
    grafana_db_password: SecretStr | None = None

    # Local hour at which a missing morning recovery triggers an alert.
    morning_alert_hour: int = Field(default=12, ge=0, le=23)

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

    @field_validator("ntfy_url", "ntfy_token", "grafana_db_password", "database_url", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def _database_is_configured(self) -> Settings:
        if self.database_url is None and not (
            self.postgres_user and self.postgres_password and self.postgres_db
        ):
            raise ValueError(
                "set DATABASE_URL, or POSTGRES_USER, POSTGRES_PASSWORD and POSTGRES_DB"
            )
        return self

    @property
    def db_url(self) -> URL:
        if self.database_url is not None:
            return make_url(self.database_url.get_secret_value())
        if not (self.postgres_user and self.postgres_password and self.postgres_db):
            raise ValueError("database is not configured")  # unreachable after validation
        return build_database_url(
            user=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            database=self.postgres_db,
            host=self.postgres_host,
            port=self.postgres_port,
        )

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url}/oauth/callback"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.local_timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
