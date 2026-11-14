"""Typed settings, parsed once at startup. Invalid combinations fail fast.

Every flag in section 3 of the brief is a field here. `load_settings()` turns
any problem into a ConfigError whose messages name the environment variable and
say how to fix it. Nothing else in the codebase reads os.environ.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}
BASE_PATH_RE = re.compile(r"^(/[A-Za-z0-9._~-]+)*$")
# Values that ship in templates, docs or tutorials. Refused in server mode.
PLACEHOLDER_WORDS = ("change", "placeholder", "example", "password", "secret", "xxx")
PLACEHOLDER_VALUES = {"opsis-local-only"}  # the value in .env.local.example


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


def looks_like_placeholder(value: str) -> bool:
    """True for empty, short or template-looking secrets."""
    lowered = value.strip().lower()
    return (
        len(lowered) < 12
        or lowered in PLACEHOLDER_VALUES
        or any(word in lowered for word in PLACEHOLDER_WORDS)
    )


def is_fernet_key(value: str) -> bool:
    try:
        return len(base64.urlsafe_b64decode(value.encode())) == 32
    except (binascii.Error, ValueError):
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
    # The externally visible URL. Its path, if any, becomes the base path (3.1).
    public_base_url: str = "http://localhost:8080"
    # Comma-separated IPs or CIDR ranges allowed to set X-Forwarded-* headers.
    trusted_proxies: str = ""

    # Workspace calendar (ADR-012)
    workspace_timezone: str = "Australia/Melbourne"
    week_start: Literal["monday", "sunday"] = "monday"
    fiscal_year_start_month: int = Field(default=7, ge=1, le=12)
    # fy_end_year: "FY2027". year_span: "2026-27".
    fiscal_year_label: Literal["fy_end_year", "year_span"] = "fy_end_year"

    # Postgres (metadata only)
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "opsis"
    postgres_password: SecretStr = SecretStr("opsis")
    postgres_db: str = "opsis"

    # Secrets
    # Fernet key for connector secrets. Required in server mode. In local mode an
    # empty value means "generate one and keep it in the data volume".
    encryption_key: SecretStr | None = None

    # Data and limits
    max_upload_mb: int = Field(default=200, ge=1, le=10_000)
    snapshot_retention: int = Field(default=3, ge=2, le=100)
    sse_keepalive_seconds: int = Field(default=15, ge=1, le=300)

    # Paths inside the container
    data_dir: Path = Path("/data")
    static_dir: Path = Path("/app/static")
    branding_file: Path = Path("/app/branding.json")
    plugins_dir: Path = Path("/app/plugins")

    log_level: Literal["debug", "info", "warning", "error"] = "info"

    @field_validator("public_base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        value = value.rstrip("/")
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("must be an http or https URL, for example https://example.com/opsis")
        if parts.query or parts.fragment:
            raise ValueError("must not contain a query string or fragment")
        if not BASE_PATH_RE.match(parts.path):
            raise ValueError(
                "has a path that cannot be used as a base path. Use plain segments, "
                "for example https://example.com/opsis"
            )
        return value

    @field_validator("trusted_proxies")
    @classmethod
    def _check_trusted_proxies(cls, value: str) -> str:
        entries = [e.strip() for e in value.split(",") if e.strip()]
        for entry in entries:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"{entry!r} is not an IP address or CIDR range. "
                    "Use a comma-separated list such as 192.168.1.10,10.0.0.0/8"
                ) from exc
        return ",".join(entries)

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

    @field_validator("encryption_key")
    @classmethod
    def _check_encryption_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value().strip():
            return None
        if not is_fernet_key(value.get_secret_value().strip()):
            raise ValueError(
                "is not a valid key. Generate one with: "
                "docker compose run --rm --no-deps api generate-key"
            )
        return SecretStr(value.get_secret_value().strip())

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
        parts = urlsplit(self.public_base_url)
        if server and parts.scheme != "https" and parts.hostname not in LOCAL_HOSTNAMES:
            problems.append(
                "PUBLIC_BASE_URL must use https with DEPLOY_MODE=server "
                f"(got {self.public_base_url}). Plain http is accepted for localhost only."
            )
        if parts.scheme == "https" and not self.trusted_proxies:
            problems.append(
                "PUBLIC_BASE_URL is https, so a reverse proxy terminates TLS, but TRUSTED_PROXIES "
                "is empty. Set TRUSTED_PROXIES to the proxy's IP address (or CIDR range) so "
                "forwarded headers are accepted from it and from nowhere else."
            )
        if self.job_runner == "worker" and self.compose_profiles is not None:
            profiles = {p.strip() for p in self.compose_profiles.split(",")}
            if "server" not in profiles:
                problems.append(
                    "JOB_RUNNER=worker needs the worker container, which is in the 'server' "
                    "Compose profile. Add server to COMPOSE_PROFILES or set JOB_RUNNER=inline."
                )
        if server:
            if looks_like_placeholder(self.postgres_password.get_secret_value()):
                problems.append(
                    "POSTGRES_PASSWORD is a placeholder or too short for DEPLOY_MODE=server. "
                    "Set a unique value of at least 12 characters before the first start."
                )
            if self.encryption_key is None:
                problems.append(
                    "ENCRYPTION_KEY is required with DEPLOY_MODE=server. Generate one with: "
                    "docker compose run --rm --no-deps api generate-key"
                )
        if problems:
            raise ValueError("\n".join(problems))
        return self

    @property
    def base_path(self) -> str:
        """URL prefix the whole app lives under: "" locally, "/opsis" on the server."""
        return urlsplit(self.public_base_url).path

    @property
    def cookie_path(self) -> str:
        return self.base_path or "/"

    @property
    def trusted_proxy_list(self) -> list[str]:
        return [e for e in self.trusted_proxies.split(",") if e]

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
            secret = name in ("ENCRYPTION_KEY", "POSTGRES_PASSWORD")
            shown = f" (got {got!r})" if isinstance(got, str | int | bool) and not secret else ""
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
