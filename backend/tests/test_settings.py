from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from opsis.settings import ConfigError, build_settings, looks_like_placeholder
from tests.conftest import SERVER, TEST_KEY


def test_defaults_are_the_local_mode_defaults() -> None:
    s = build_settings()
    assert (s.deploy_mode, s.auth_mode, s.job_runner, s.mcp_auth) == (
        "local",
        "none",
        "inline",
        "none",
    )
    assert s.bind_address == "127.0.0.1"
    assert s.base_path == "" and s.cookie_path == "/" and s.trusted_proxy_list == []
    assert s.feature_sql_lab and s.feature_assistant
    assert not s.feature_public_share_links and not s.feature_scheduled_refresh
    assert (s.workspace_timezone, s.week_start, s.fiscal_year_start_month) == (
        "Australia/Melbourne",
        "monday",
        7,
    )
    assert s.fiscal_year_label == "fy_end_year"
    assert s.encryption_key is None  # generated on first use in local mode


def test_server_settings_are_valid() -> None:
    s = build_settings(**SERVER, feature_public_share_links=True)
    assert s.deploy_mode == "server" and s.is_https
    assert s.base_path == "/opsis" and s.cookie_path == "/opsis"
    assert s.trusted_proxy_list == ["192.168.1.10"]


@pytest.mark.parametrize(
    ("url", "base_path"),
    [
        ("http://localhost:8080", ""),
        ("http://localhost:8080/", ""),
        ("http://localhost:8088/opsis", "/opsis"),
        ("http://localhost:8088/opsis/", "/opsis"),
        ("http://localhost/apps/bi", "/apps/bi"),
    ],
)
def test_base_path_is_derived_from_public_base_url(url: str, base_path: str) -> None:
    assert build_settings(public_base_url=url).base_path == base_path


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
        ({**SERVER, "public_base_url": "http://bi.test/opsis"}, "PUBLIC_BASE_URL must use https"),
        ({**SERVER, "trusted_proxies": ""}, "TRUSTED_PROXIES is empty"),
        ({"public_base_url": "https://localhost/opsis"}, "TRUSTED_PROXIES is empty"),
        ({"trusted_proxies": "proxy.internal"}, "TRUSTED_PROXIES"),
        ({"trusted_proxies": "10.0.0.1, 999.1.1.1"}, "is not an IP address or CIDR range"),
        ({"job_runner": "worker", "compose_profiles": ""}, "JOB_RUNNER=worker needs the worker"),
        ({"job_runner": "worker", "compose_profiles": "caddy"}, "JOB_RUNNER=worker needs"),
        ({"public_base_url": "localhost:8080"}, "PUBLIC_BASE_URL"),
        ({"public_base_url": "http://localhost:8080/a?b=1"}, "query string"),
        ({"public_base_url": "http://localhost:8080/a b"}, "base path"),
        ({"public_base_url": "http://localhost:8080//x"}, "base path"),
        ({"auth_mode": "sso"}, "AUTH_MODE"),
        ({"fiscal_year_start_month": 13}, "FISCAL_YEAR_START_MONTH"),
        ({"fiscal_year_label": "FY27"}, "FISCAL_YEAR_LABEL"),
        ({"workspace_timezone": "Mars/Olympus"}, "WORKSPACE_TIMEZONE"),
        ({"week_start": "friday"}, "WEEK_START"),
        ({"snapshot_retention": 1}, "SNAPSHOT_RETENTION"),
        ({"encryption_key": "not-a-key"}, "ENCRYPTION_KEY: is not a valid key"),
        ({**SERVER, "encryption_key": ""}, "ENCRYPTION_KEY is required"),
        ({**SERVER, "postgres_password": "change-me-before-first-start"}, "POSTGRES_PASSWORD"),
        ({**SERVER, "postgres_password": "opsis-local-only"}, "POSTGRES_PASSWORD is a placeholder"),
        ({**SERVER, "postgres_password": "short"}, "POSTGRES_PASSWORD is a placeholder"),
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


def test_secret_values_never_appear_in_errors_or_repr() -> None:
    with pytest.raises(ConfigError) as info:
        build_settings(**{**SERVER, "postgres_password": "hunter2-secret-xx"})
    assert "hunter2" not in str(info.value)
    with pytest.raises(ConfigError) as info:
        build_settings(encryption_key="definitely-not-valid-key-material")
    assert "definitely-not-valid" not in str(info.value)
    s = build_settings(postgres_password="s3cret-value", encryption_key=TEST_KEY)
    assert "s3cret-value" not in repr(s) and TEST_KEY not in repr(s)


def test_placeholder_detection() -> None:
    for value in ("", "changeme", "change-me-please-now", "password12345", "opsis-local-only"):
        assert looks_like_placeholder(value)
    assert not looks_like_placeholder("Zq7-fake-test-value-41")


def test_loopback_variants_are_accepted() -> None:
    for address in ("127.0.0.1", "localhost", "::1"):
        assert build_settings(bind_address=address).bind_address == address


def test_trusted_proxies_accept_addresses_and_ranges() -> None:
    s = build_settings(trusted_proxies=" 192.168.1.10 , 172.16.0.0/12,fd00::/8 ")
    assert s.trusted_proxy_list == ["192.168.1.10", "172.16.0.0/12", "fd00::/8"]


def _template(name: str) -> dict[str, Any]:
    root = os.environ.get("OPSIS_REPO_ROOT")
    if not root or not (Path(root) / name).is_file():
        pytest.skip("repo root is not mounted")
    return {k.lower(): v for k, v in dotenv_values(Path(root) / name).items() if v is not None}


def test_local_env_template_is_valid_as_shipped() -> None:
    assert build_settings(**_template(".env.local.example")).deploy_mode == "local"


def test_server_env_template_refuses_to_start_until_placeholders_are_replaced() -> None:
    values = _template(".env.server.example")
    with pytest.raises(ConfigError) as info:
        build_settings(**values)
    problems = "\n".join(info.value.problems)
    assert len(info.value.problems) == 6
    for name in (
        "TRUSTED_PROXIES",
        "POSTGRES_PASSWORD",
        "ENCRYPTION_KEY",
        "ADMIN_EMAIL",
        "GOOGLE_CLIENT_ID",
        "ANTHROPIC_API_KEY",
    ):
        assert name in problems

    values.update(
        trusted_proxies="192.168.1.10",
        postgres_password="Zq7-fake-test-value-41",
        encryption_key=TEST_KEY,
        admin_email="admin@example.org",
        google_client_id="1234.apps.googleusercontent.com",
        google_client_secret="GOCSPX-test-value-5",
        anthropic_api_key="sk-ant-test-value-7",
    )
    assert build_settings(**values).base_path == "/opsis"
