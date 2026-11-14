from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from fastapi import Depends
from fastapi.testclient import TestClient

from opsis.api.deps import require, require_feature
from opsis.api.sse import SSE_HEADERS, sse_response, with_keepalive
from opsis.auth.principal import Permission
from tests.conftest import SERVER

ClientFor = Callable[..., TestClient]


def add_probe(client: TestClient, path: str, methods: list[str], **kwargs: Any) -> None:
    """Add a test-only route ahead of the SPA fallback."""
    app: Any = client.app
    base = app.state.settings.base_path
    fallback = app.router.routes.pop()
    app.add_api_route(f"{base}{path}", lambda: {"ok": True}, methods=methods, **kwargs)
    app.router.routes.append(fallback)


def test_healthz(client_for: ClientFor) -> None:
    response = client_for().get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


def test_config_reflects_flags(client_for: ClientFor) -> None:
    local = client_for().get("/api/config").json()
    assert local["deployMode"] == "local" and local["authMode"] == "none"
    assert local["basePath"] == ""
    assert local["features"]["sqlLab"] is True
    assert local["features"]["publicShareLinks"] is False
    assert local["calendar"] == {
        "timezone": "Australia/Melbourne",
        "weekStart": "monday",
        "fiscalYearStartMonth": 7,
        "fiscalYearLabel": "fy_end_year",
    }

    changed = client_for(
        feature_sql_lab=False,
        app_name="Acme BI",
        fiscal_year_start_month=4,
        fiscal_year_label="year_span",
    )
    body = changed.get("/api/config").json()
    assert body["features"]["sqlLab"] is False
    assert body["appName"] == "Acme BI" and body["branding"]["name"] == "Acme BI"
    assert body["calendar"]["fiscalYearStartMonth"] == 4
    assert body["calendar"]["fiscalYearLabel"] == "year_span"

    server = client_for(**SERVER, feature_public_share_links=True).get("/api/config").json()
    assert server["deployMode"] == "server" and server["authMode"] == "full"
    assert server["basePath"] == "/opsis"
    assert server["features"]["publicShareLinks"] is True


def test_config_has_no_secrets(client_for: ClientFor) -> None:
    client = client_for()
    text = client.get("/api/config").text.lower()
    settings: Any = client.app.state.settings  # type: ignore[attr-defined]
    password = settings.postgres_password.get_secret_value().lower()
    key = settings.encryption_key.get_secret_value().lower()
    for word in ("password", "postgres", "secret", "encryption", password, key):
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
    guards = [
        Depends(require_feature("feature_sql_lab")),
        Depends(require(Permission.SQL_LAB_USE)),
    ]
    on = client_for()
    add_probe(on, "/api/_probe/sql", ["GET"], dependencies=guards)
    assert on.get("/api/_probe/sql").json() == {"ok": True}

    off = client_for(feature_sql_lab=False)
    add_probe(off, "/api/_probe/sql", ["GET"], dependencies=guards)
    response = off.get("/api/_probe/sql")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_unknown_api_path_is_a_json_404(client_for: ClientFor) -> None:
    response = client_for().get("/api/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_security_headers_and_no_hsts(client_for: ClientFor) -> None:
    for client in (client_for(), client_for(**SERVER)):
        headers = client.get("/api/config").headers
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["X-Content-Type-Options"] == "nosniff"
        csp = headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in csp and "script-src 'self';" in csp
        assert "unsafe-eval" not in csp
        # HSTS covers the whole domain, so it is the reverse proxy's decision.
        assert "Strict-Transport-Security" not in headers
        assert "access-control-allow-origin" not in headers  # CORS is off


# ---- Sub-path hosting (brief 3.1) -------------------------------------------


def static_dir(tmp_path: Path) -> Path:
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(
        '<!doctype html><html><head>\n<link rel="icon" href="favicon.svg" />'
        '<script type="module" src="./assets/app.js"></script></head>'
        '<body><div id="root"></div></body></html>'
    )
    (static / "assets" / "app.js").write_text("console.log('app')")
    (static / "favicon.svg").write_text("<svg/>")
    return static


def test_everything_lives_under_the_base_path(client_for: ClientFor, tmp_path: Path) -> None:
    client = client_for(
        public_base_url="http://localhost:8088/opsis", static_dir=static_dir(tmp_path)
    )
    root = TestClient(client.app, base_url="http://testserver", follow_redirects=False)

    # Inside the prefix.
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/config").json()["basePath"] == "/opsis"
    assert client.get("/assets/app.js").text == "console.log('app')"
    assert client.get("/favicon.svg").status_code == 200

    # Outside the prefix: nothing, not even health or the SPA fallback.
    for path in ("/", "/healthz", "/api/config", "/assets/app.js", "/dashboards", "/opsisx"):
        response = root.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "not_found"

    redirect = root.get("/opsis")
    assert redirect.status_code == 308 and redirect.headers["location"] == "/opsis/"


def test_index_gets_base_href_and_config_block(client_for: ClientFor, tmp_path: Path) -> None:
    static = static_dir(tmp_path)
    for url, base in (("http://localhost:8088/opsis", "/opsis"), ("http://localhost:8080", "")):
        client = client_for(public_base_url=url, static_dir=static, app_name="A<b>&C")
        for path in ("/", "/dashboards/42", "/datasets"):  # deep links and refresh
            response = client.get(path)
            assert response.status_code == 200, path
            html = response.text
            assert f'<base href="{base}/" />' in html
            assert html.index("<base ") < html.index("<link ")  # before any relative URL
            block = html.split('<script id="opsis-config" type="application/json">')[1]
            block = block.split("</script>")[0]
            assert "<" not in block
            assert json.loads(block) == {"basePath": base, "appName": "A<b>&C", "version": "0.1.0"}
        # API and unknown API paths never fall back to the SPA.
        assert client.get("/api/nope").status_code == 404
        assert client.get("/mcp/anything").status_code == 404


def test_csrf_cookie_is_prefixed_and_scoped_to_the_base_path(client_for: ClientFor) -> None:
    def cookie_header(**overrides: Any) -> str:
        client = client_for(csrf=False, **overrides)
        return client.get("/api/config").headers["set-cookie"]

    local = cookie_header()
    assert local.startswith("opsis_csrf=") and "Path=/;" in local + ";"
    assert "SameSite=lax" in local and "Secure" not in local and "HttpOnly" not in local

    sub = cookie_header(public_base_url="http://localhost:8088/opsis")
    assert "Path=/opsis" in sub

    server = cookie_header(**SERVER)
    assert "Path=/opsis" in server and "Secure" in server

    # Issued once: a client that already has the cookie is not sent another.
    client = client_for()
    assert "set-cookie" not in client.get("/api/config").headers


def test_unsafe_requests_need_the_csrf_header(client_for: ClientFor) -> None:
    client = client_for(csrf=False)
    add_probe(client, "/api/_probe/write", ["POST", "DELETE"])

    assert client.post("/api/_probe/write").status_code == 403  # no cookie yet
    token = client.cookies["opsis_csrf"]  # issued on that first response
    blocked = client.post("/api/_probe/write")
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "csrf_failed"
    assert client.post("/api/_probe/write", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.delete("/api/_probe/write", headers={"X-CSRF-Token": token}).status_code == 200
    assert client.post("/api/_probe/write", headers={"X-CSRF-Token": token}).json() == {"ok": True}
    # Bearer-token clients are not browsers and carry no cookie.
    assert (
        client.post("/api/_probe/write", headers={"Authorization": "Bearer x"}).status_code == 200
    )
    # Safe methods never need it.
    assert client.get("/api/config").status_code == 200


# ---- Streaming helper ---------------------------------------------------------


def test_sse_stream_has_proxy_headers_and_keepalives() -> None:
    async def events() -> AsyncIterator[tuple[str, Any]]:
        yield "version_created", {"version": 2}
        await asyncio.sleep(0.12)
        yield "done", {}

    async def collect() -> list[str]:
        return [chunk async for chunk in with_keepalive(events(), keepalive_seconds=0.05)]

    chunks = asyncio.run(collect())
    assert chunks[0] == 'event: version_created\ndata: {"version":2}\n\n'
    assert ": keep-alive\n\n" in chunks[1:-1]
    assert chunks[-1] == "event: done\ndata: {}\n\n"

    response = sse_response(events(), keepalive_seconds=15)
    assert response.media_type == "text/event-stream"
    assert response.headers["x-accel-buffering"] == "no"
    assert SSE_HEADERS["Cache-Control"].startswith("no-cache")
