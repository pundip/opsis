"""Typed settings, parsed once at startup. Invalid combinations fail fast.

Every flag in section 3 of the brief is a field here. `load_settings()` turns
any problem into a ConfigError whose messages name the environment variable and
say how to fix it. Nothing else in the codebase reads os.environ.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


class ConfigError(Exception):
    """Raised when the environment is invalid. `problems` is safe to print."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("Invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))


def is_loopback(address: str) -> bool:
    if address.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(address.strip("[]")).is_loopback
    except ValueError:
        return False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # Branding and mode
    app_name: str = "Opsis"
    deploy_mode: Literal["local", "server"] = "local"
    compose_profiles: str | None = None  # passed through from .env, used for checks only

    # Flags (brief section 3)
    auth_mode: Literal["none", "local", "full"] = "none"
    job_runner: Literal["inline", "worker"] = "inline"
    feature_scheduled_refresh: bool = False
    feature_public_share_links: bool = False
    feature_email: bool = False
    feature_assistant: bool = True
    assistant_provider: Literal["anthropic", "ollama"] = "ollama"
    mcp_auth: Literal["none", "token"] = "none"
    feature_sql_lab: bool = True
    feature_backups: bool = False
    bind_address: str = "127.0.0.1"
    http_port: int = Field(default=8080, ge=1, le=65535)
    public_base_url: str = "http://localhost:8080"

    # Workspace calendar
    workspace_timezone: str = "Australia/Melbourne"
    week_start: Literal["monday", "sunday"] = "monday"
    fiscal_year_start_month: int = Field(default=7, ge=1, le=12)

    # Postgres (metadata only)
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "opsis"
    postgres_password: SecretStr = SecretStr("opsis")
    postgres_db: str = "opsis"

    # Paths inside the container
    data_dir: Path = Path("/data")
    static_dir: Path = Path("/app/static")
    branding_file: Path = Path("/app/branding.json")

    log_level: Literal["debug", "info", "warning", "error"] = "info"

    @field_validator("public_base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        value = value.rstrip("/")
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("must be an http or https URL, for example https://bi.example.com")
        if parts.path or parts.query or parts.fragment:
            raise ValueError("must be an origin only, with no path or query")
        return value

    @field_validator("workspace_timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                "must be an IANA timezone name, for example Australia/Melbourne"
            ) from exc
        return value

    @model_validator(mode="after")
    def _check_combinations(self) -> Settings:
        problems: list[str] = []
        loopback = is_loopback(self.bind_address)
        server = self.deploy_mode == "server"

        if self.auth_mode == "none":
            if server:
                problems.append(
                    "AUTH_MODE=none is not allowed with DEPLOY_MODE=server. "
                    "Set AUTH_MODE=local or AUTH_MODE=full."
                )
            elif not loopback:
                problems.append(
                    f"AUTH_MODE=none requires a loopback BIND_ADDRESS (got {self.bind_address}). "
                    "Set BIND_ADDRESS=127.0.0.1, or set AUTH_MODE=local or AUTH_MODE=full."
                )
        if self.mcp_auth == "none":
            if server:
                problems.append(
                    "MCP_AUTH=none is not allowed with DEPLOY_MODE=server. Set MCP_AUTH=token."
                )
            elif not loopback:
                problems.append(
                    f"MCP_AUTH=none requires a loopback BIND_ADDRESS (got {self.bind_address}). "
                    "Set BIND_ADDRESS=127.0.0.1 or MCP_AUTH=token."
                )
        if self.feature_public_share_links:
            if self.auth_mode == "none":
                problems.append(
                    "FEATURE_PUBLIC_SHARE_LINKS=true requires sign-in. "
                    "Set AUTH_MODE=local or AUTH_MODE=full, or turn the flag off."
                )
            if not server:
                problems.append(
                    "FEATURE_PUBLIC_SHARE_LINKS=true is only available with DEPLOY_MODE=server."
                )
        if server:
            parts = urlsplit(self.public_base_url)
            if parts.scheme != "https" and parts.hostname not in LOCAL_HOSTNAMES:
                problems.append(
                    "PUBLIC_BASE_URL must use https with DEPLOY_MODE=server "
                    f"(got {self.public_base_url}). Plain http is accepted for localhost only."
                )
        if self.job_runner == "worker" and self.compose_profiles is not None:
            profiles = {p.strip() for p in self.compose_profiles.split(",")}
            if "server" not in profiles:
                problems.append(
                    "JOB_RUNNER=worker needs the worker container, which is in the 'server' "
                    "Compose profile. Add server to COMPOSE_PROFILES or set JOB_RUNNER=inline."
                )
        if problems:
            raise ValueError("\n".join(problems))
        return self

    @property
    def sqlalchemy_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    @property
    def is_https(self) -> bool:
        return self.public_base_url.startswith("https://")


def _problems_from(exc: ValidationError) -> list[str]:
    problems: list[str] = []
    for err in exc.errors():
        message = err["msg"].removeprefix("Value error, ")
        if err["loc"]:
            name = str(err["loc"][0]).upper()
            got = err.get("input")
            shown = f" (got {got!r})" if isinstance(got, str | int | bool) else ""
            problems.append(f"{name}{shown}: {message}")
        else:
            problems.extend(message.split("\n"))
    return problems


def build_settings(**values: Any) -> Settings:
    """Create settings from the environment plus overrides, with friendly errors."""
    try:
        return Settings(**values)
    except ValidationError as exc:
        raise ConfigError(_problems_from(exc)) from None


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    return build_settings()
