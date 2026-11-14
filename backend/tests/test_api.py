from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends
from fastapi.testclient import TestClient

from opsis.api.deps import require, require_feature
from opsis.auth.principal import Permission

ClientFor = Callable[..., TestClient]
SERVER = {
    "deploy_mode": "server",
    "auth_mode": "full",
    "mcp_auth": "token",
    "public_base_url": "https://bi.example.com",
}


def test_healthz(client_for: ClientFor) -> None:
    response = client_for().get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


def test_config_reflects_flags(client_for: ClientFor) -> None:
    local = client_for().get("/api/config").json()
    assert local["deployMode"] == "local" and local["authMode"] == "none"
    assert local["features"]["sqlLab"] is True
    assert local["features"]["publicShareLinks"] is False
    assert local["calendar"] == {
        "timezone": "Australia/Melbourne",
        "weekStart": "monday",
        "fiscalYearStartMonth": 7,
    }

    changed = client_for(feature_sql_lab=False, app_name="Acme BI", fiscal_year_start_month=4)
    body = changed.get("/api/config").json()
    assert body["features"]["sqlLab"] is False
    assert body["appName"] == "Acme BI" and body["branding"]["name"] == "Acme BI"
    assert body["calendar"]["fiscalYearStartMonth"] == 4

    server = client_for(**SERVER, feature_public_share_links=True).get("/api/config").json()
    assert server["deployMode"] == "server" and server["authMode"] == "full"
    assert server["features"]["publicShareLinks"] is True


def test_config_has_no_secrets(client_for: ClientFor) -> None:
    client = client_for()
    text = client.get("/api/config").text.lower()
    password = client.app.state.settings.postgres_password.get_secret_value()  # type: ignore[attr-defined]
    for word in ("password", "postgres", "secret", password.lower()):
        assert word not in text


def test_auth_mode_none_signs_in_the_builtin_admin(client_for: ClientFor) -> None:
    me = client_for().get("/api/me").json()
    assert me["role"] == "admin" and me["authMethod"] == "none"
    assert "users:manage" in me["permissions"]


def test_other_auth_modes_require_sign_in(client_for: ClientFor) -> None:
    response = client_for(**SERVER).get("/api/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


def test_feature_flag_guard_returns_404_and_permission_guard_runs(client_for: ClientFor) -> None:
    def add_routes(client: TestClient) -> None:
        app = client.app
        app.router.routes.pop()  # put the probes ahead of the SPA fallback
        app.add_api_route(  # type: ignore[attr-defined]
            "/api/_probe/sql",
            lambda: {"ok": True},
            dependencies=[
                Depends(require_feature("feature_sql_lab")),
                Depends(require(Permission.SQL_LAB_USE)),
            ],
        )

    on = client_for()
    add_routes(on)
    assert on.get("/api/_probe/sql").json() == {"ok": True}

    off = client_for(feature_sql_lab=False)
    add_routes(off)
    response = off.get("/api/_probe/sql")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_unknown_api_path_is_a_json_404(client_for: ClientFor) -> None:
    response = client_for().get("/api/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_security_headers(client_for: ClientFor) -> None:
    headers = client_for().get("/api/config").headers
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "Strict-Transport-Security" not in headers

    server = client_for(**SERVER).get("/api/config").headers
    assert "max-age" in server["Strict-Transport-Security"]
