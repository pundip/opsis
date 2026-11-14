from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from opsis.settings import ConfigError, build_settings

SERVER: dict[str, Any] = {
    "deploy_mode": "server",
    "auth_mode": "full",
    "mcp_auth": "token",
    "job_runner": "worker",
    "compose_profiles": "server",
    "public_base_url": "https://bi.example.com",
}


def test_defaults_are_the_local_mode_defaults() -> None:
    s = build_settings()
    assert (s.deploy_mode, s.auth_mode, s.job_runner, s.mcp_auth) == (
        "local",
        "none",
        "inline",
        "none",
    )
    assert s.bind_address == "127.0.0.1"
    assert s.feature_sql_lab and s.feature_assistant
    assert not s.feature_public_share_links and not s.feature_scheduled_refresh
    assert (s.workspace_timezone, s.week_start, s.fiscal_year_start_month) == (
        "Australia/Melbourne",
        "monday",
        7,
    )


def test_server_settings_are_valid() -> None:
    s = build_settings(**SERVER, feature_public_share_links=True)
    assert s.deploy_mode == "server" and s.is_https


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"bind_address": "0.0.0.0"}, "AUTH_MODE=none requires a loopback BIND_ADDRESS"),
        ({"bind_address": "192.168.1.10"}, "AUTH_MODE=none requires a loopback BIND_ADDRESS"),
        (
            {"bind_address": "0.0.0.0", "auth_mode": "local"},
            "MCP_AUTH=none requires a loopback BIND_ADDRESS",
        ),
        ({**SERVER, "auth_mode": "none"}, "AUTH_MODE=none is not allowed with DEPLOY_MODE=server"),
        ({**SERVER, "mcp_auth": "none"}, "MCP_AUTH=none is not allowed with DEPLOY_MODE=server"),
        ({"feature_public_share_links": True}, "FEATURE_PUBLIC_SHARE_LINKS=true requires sign-in"),
        (
            {"feature_public_share_links": True, "auth_mode": "local"},
            "only available with DEPLOY_MODE=server",
        ),
        ({**SERVER, "public_base_url": "http://bi.example.com"}, "PUBLIC_BASE_URL must use https"),
        ({"job_runner": "worker", "compose_profiles": ""}, "JOB_RUNNER=worker needs the worker"),
        ({"public_base_url": "localhost:8080"}, "PUBLIC_BASE_URL"),
        ({"public_base_url": "http://localhost:8080/app"}, "origin only"),
        ({"auth_mode": "sso"}, "AUTH_MODE"),
        ({"fiscal_year_start_month": 13}, "FISCAL_YEAR_START_MONTH"),
        ({"workspace_timezone": "Mars/Olympus"}, "WORKSPACE_TIMEZONE"),
        ({"week_start": "friday"}, "WEEK_START"),
    ],
)
def test_invalid_settings_fail_fast_with_a_clear_message(
    overrides: dict[str, Any], expected: str
) -> None:
    with pytest.raises(ConfigError) as info:
        build_settings(**overrides)
    assert expected in str(info.value)
    assert str(info.value).startswith("Invalid configuration:")


def test_all_problems_are_reported_together() -> None:
    with pytest.raises(ConfigError) as info:
        build_settings(bind_address="0.0.0.0", feature_public_share_links=True)
    assert len(info.value.problems) == 4


def test_loopback_variants_are_accepted() -> None:
    for address in ("127.0.0.1", "localhost", "::1"):
        assert build_settings(bind_address=address).bind_address == address


def test_server_mode_allows_http_on_localhost_for_trials() -> None:
    assert build_settings(**{**SERVER, "public_base_url": "http://localhost"})


def test_password_is_not_in_repr() -> None:
    s = build_settings(postgres_password="s3cret-value")
    assert "s3cret-value" not in repr(s)


@pytest.mark.parametrize("name", [".env.local.example", ".env.server.example"])
def test_env_templates_are_valid(name: str) -> None:
    root = os.environ.get("OPSIS_REPO_ROOT")
    if not root or not (Path(root) / name).is_file():
        pytest.skip("repo root is not mounted")
    values = {k.lower(): v for k, v in dotenv_values(Path(root) / name).items() if v is not None}
    settings = build_settings(**values)
    assert settings.deploy_mode == ("local" if "local" in name else "server")
