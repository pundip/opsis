"""Milestone 6: sign-in, sessions, lockouts, invitations, access lists, restricted
datasets, access tokens on the REST API, share links, View as, Sign in with
Google (against a fake provider), the audit log, and local mode exposing none of it.
"""

from __future__ import annotations

import base64
import hashlib
import urllib.parse
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from opsis.auth import google
from opsis.db.models import AuditLog, LoginAttempt, ShareLink, User, UserLink
from opsis.db.models import Session as SessionRow
from opsis.services import accounts, tokens, users
from opsis.settings import ConfigError
from tests.conftest import SERVER, make_settings
from tests.test_ingestion import ok
from tests.test_query_api import MODEL_YAML, load
from tests.test_query_api import source as source  # noqa: F401  (fixture)

ClientFor = Callable[..., TestClient]

ADMIN = "admin@example.test"
INITIAL = "Initial-Admin-Pass-41"
CHANGED = "Changed-Admin-Pass-92"
PASSWORD = "Person-Pass-Word-77"
SIGN_IN: dict[str, Any] = {
    "auth_mode": "local",
    "mcp_auth": "token",
    "admin_email": ADMIN,
    "admin_initial_password": INITIAL,
}
# Server mode with Google, share links and the sub-path, as on the real server.
SERVER_SIGN_IN: dict[str, Any] = {
    **SERVER,
    "admin_email": ADMIN,
    "admin_initial_password": INITIAL,
    "google_client_id": "client-123",
    "google_client_secret": "test-client-key-0099",
    "feature_public_share_links": True,
}


def admin_client(client_for: ClientFor, **settings: Any) -> TestClient:
    """A client signed in as the first admin, password already changed."""
    client = client_for(**{**SIGN_IN, **settings})
    response = client.post("/api/auth/login", json={"email": ADMIN, "password": CHANGED})
    if response.status_code == 401:
        ok(client.post("/api/auth/login", json={"email": ADMIN, "password": INITIAL}))
        ok(
            client.post(
                "/api/auth/password", json={"currentPassword": INITIAL, "newPassword": CHANGED}
            )
        )
    else:
        assert response.status_code == 200, response.text
    return client


def person(
    client_for: ClientFor,
    admin: TestClient,
    email: str,
    role: str = "viewer",
    attributes: dict[str, Any] | None = None,
    **settings: Any,
) -> tuple[TestClient, str]:
    """Invite someone, accept the invitation in a fresh browser; returns (client, user id)."""
    made = ok(
        admin.post(
            "/api/users", json={"email": email, "role": role, "attributes": attributes or {}}
        ),
        201,
    )
    token = made["link"].rsplit("/", 1)[1]
    client = client_for(**{**SIGN_IN, **settings})
    ok(client.post(f"/api/auth/links/{token}", json={"password": PASSWORD}))
    return client, made["user"]["id"]


def cookies_set(response: Any) -> list[str]:
    return [v for k, v in response.headers.multi_items() if k.lower() == "set-cookie"]


def simple_spec(title: str = "Access test", **extra_items: Any) -> dict[str, Any]:
    return {
        "specVersion": 1,
        "title": title,
        "layout": {"columns": 12, "rowHeight": 40},
        "filters": [],
        "items": [
            {"id": "k", "visual": "kpi", "position": {"x": 0, "y": 0, "w": 3, "h": 3},
             "dataset": "gifts", "bindings": {"value": ["gifts.total_amount"]}},
            {"id": "t", "visual": "table", "position": {"x": 0, "y": 3, "w": 12, "h": 6},
             "dataset": "gifts", "bindings": {"columns": ["gifts.region", "gifts.gift_count"]},
             "sort": [{"field": "gifts.region", "dir": "asc"}]},
            *extra_items.get("more", []),
        ],
        "interactions": {"crossFilter": True},
    }  # fmt: skip


# ---- configuration -------------------------------------------------------------------------


def test_settings_for_sign_in() -> None:
    def problems(**values: Any) -> str:
        with pytest.raises(ConfigError) as info:
            make_settings(**values)
        return str(info.value)

    assert "needs ADMIN_EMAIL" in problems(auth_mode="local", mcp_auth="token")
    assert "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET" in problems(
        auth_mode="full", mcp_auth="token", admin_email=ADMIN
    )
    assert "any Google account could sign in" in problems(
        **{**SERVER_SIGN_IN, "google_auto_provision": True}
    )
    assert "MCP_AUTH=none only works with AUTH_MODE=none" in problems(
        auth_mode="local", admin_email=ADMIN
    )
    placeholder = problems(**{**SERVER_SIGN_IN, "admin_initial_password": "change-me-now"})
    assert "ADMIN_INITIAL_PASSWORD is a placeholder" in placeholder
    assert "change-me-now" not in placeholder  # secrets are never echoed
    make_settings(**SERVER_SIGN_IN)
    make_settings(**SIGN_IN)


# ---- password sign-in, sessions and lockouts -------------------------------------------------


def test_password_sign_in_sessions_and_lockout(client_for: ClientFor, db: Session) -> None:
    first = client_for(**SIGN_IN)
    assert first.get("/api/me").status_code == 401
    assert first.get("/api/dashboards").json()["error"]["code"] == "not_authenticated"

    wrong = first.post("/api/auth/login", json={"email": ADMIN, "password": "not-it-at-all"})
    assert wrong.status_code == 401 and wrong.json()["error"]["message"] == (
        "The email or password is not right."
    )
    nobody = first.post("/api/auth/login", json={"email": "nobody@example.test", "password": "x"})
    assert nobody.json()["error"]["message"] == wrong.json()["error"]["message"]  # no hint

    signed = first.post("/api/auth/login", json={"email": ADMIN, "password": INITIAL})
    assert signed.json() == {"signedIn": True, "mustChangePassword": True}
    session_cookie = next(c for c in cookies_set(signed) if c.startswith("opsis_session="))
    assert "HttpOnly" in session_cookie and "SameSite=lax" in session_cookie
    assert "Path=/" in session_cookie and "Secure" not in session_cookie  # http locally
    stored = db.scalars(select(SessionRow)).all()
    assert len(stored) == 1 and stored[0].id != session_cookie.split(";")[0].split("=", 1)[1]

    # Until the first password is changed, nothing else works.
    assert first.get("/api/me").json()["mustChangePassword"] is True
    blocked = first.get("/api/dashboards")
    assert (
        blocked.status_code == 403 and blocked.json()["error"]["code"] == "password_change_required"
    )
    weak = first.post(
        "/api/auth/password", json={"currentPassword": INITIAL, "newPassword": "short"}
    )
    assert weak.status_code == 422 and weak.json()["error"]["details"][0]["path"] == "/newPassword"
    bad = first.post("/api/auth/password", json={"currentPassword": "nope", "newPassword": CHANGED})
    assert bad.json()["error"]["details"][0]["path"] == "/currentPassword"
    ok(first.post("/api/auth/password", json={"currentPassword": INITIAL, "newPassword": CHANGED}))
    assert first.get("/api/dashboards").status_code == 200

    # A password change ends every other session, and keeps this one.
    second = admin_client(client_for)
    assert second.get("/api/me").status_code == 200
    newer = "Another-Admin-Pass-63"
    ok(first.post("/api/auth/password", json={"currentPassword": CHANGED, "newPassword": newer}))
    assert first.get("/api/me").status_code == 200 and second.get("/api/me").status_code == 401
    ok(first.post("/api/auth/password", json={"currentPassword": newer, "newPassword": CHANGED}))

    # Sign out; sign out everywhere.
    ok(first.post("/api/auth/logout"))
    assert first.get("/api/me").status_code == 401
    one, two = admin_client(client_for), admin_client(client_for)
    assert ok(one.post("/api/auth/logout-everywhere"))["sessionsEnded"] >= 2
    assert one.get("/api/me").status_code == 401 and two.get("/api/me").status_code == 401

    # Idle and absolute timeouts.
    idle = admin_client(client_for)
    db.execute(update(SessionRow).values(last_seen_at=datetime.now(UTC) - timedelta(hours=13)))
    db.commit()
    assert idle.get("/api/me").status_code == 401
    old = admin_client(client_for)
    db.execute(update(SessionRow).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    db.commit()
    assert old.get("/api/me").status_code == 401

    # Lockout: five failures lock the email for a minute, then longer for each failure.
    locker = client_for(**SIGN_IN)
    for _ in range(5):
        assert (
            locker.post(
                "/api/auth/login", json={"email": ADMIN, "password": "wrong-guess-1"}
            ).status_code
            == 401
        )
    locked = locker.post("/api/auth/login", json={"email": ADMIN, "password": CHANGED})
    assert (
        locked.status_code == 429 and "Try again in 1 minute" in locked.json()["error"]["message"]
    )
    row = db.scalar(select(LoginAttempt).where(LoginAttempt.key == f"email:{ADMIN}"))
    assert row is not None and row.failures == 5
    db.execute(update(LoginAttempt).values(locked_until=datetime.now(UTC) - timedelta(seconds=1)))
    db.commit()
    assert (
        locker.post(
            "/api/auth/login", json={"email": ADMIN, "password": "wrong-guess-2"}
        ).status_code
        == 401
    )
    db.expire_all()
    again = db.scalar(select(LoginAttempt).where(LoginAttempt.key == f"email:{ADMIN}"))
    assert again is not None and again.locked_until is not None
    assert again.locked_until - datetime.now(UTC) > timedelta(seconds=90)  # backoff: 2 minutes
    db.execute(update(LoginAttempt).values(locked_until=None))
    db.commit()
    assert (
        locker.post("/api/auth/login", json={"email": ADMIN, "password": CHANGED}).status_code
        == 200
    )
    assert db.scalar(select(LoginAttempt).where(LoginAttempt.key == f"email:{ADMIN}")) is None

    actions = [a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.id)).all()]
    assert "auth.login_failed" in actions and "auth.login" in actions
    assert "user.password_changed" in actions and "auth.logout_everywhere" in actions


def test_invitations_users_and_groups(client_for: ClientFor, db: Session) -> None:
    admin = admin_client(client_for)
    made = ok(
        admin.post("/api/users", json={"email": "Vera@Example.test", "displayName": "Vera"}), 201
    )
    assert made["user"]["email"] == "vera@example.test" and made["user"]["status"] == "invited"
    assert made["link"].startswith("http://localhost:8080/set-password/")
    assert admin.post("/api/users", json={"email": "vera@example.test"}).status_code == 409
    token = made["link"].rsplit("/", 1)[1]
    assert db.scalar(select(UserLink.token_hash)) == hashlib.sha256(token.encode()).hexdigest()

    vera = client_for(**SIGN_IN)
    assert ok(vera.get(f"/api/auth/links/{token}")) == {
        "email": "vera@example.test", "displayName": "Vera", "purpose": "invite",
    }  # fmt: skip
    assert vera.post(f"/api/auth/links/{token}", json={"password": "short"}).status_code == 422
    ok(vera.post(f"/api/auth/links/{token}", json={"password": PASSWORD}))
    assert ok(vera.get("/api/me"))["role"] == "viewer"
    assert vera.post(f"/api/auth/links/{token}", json={"password": PASSWORD}).json()["error"][
        "code"
    ] == ("link_not_valid")
    assert vera.get("/api/users").status_code == 403

    # A reset link, which expires.
    vera_id = made["user"]["id"]
    reset = ok(admin.post(f"/api/users/{vera_id}/password-link"))["link"]
    db.execute(update(UserLink).values(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
    db.commit()
    expired = client_for(**SIGN_IN).get(f"/api/auth/links/{reset.rsplit('/', 1)[1]}")
    assert expired.status_code == 410 and "expired" in expired.json()["error"]["message"]

    # The last admin is protected; nobody deactivates themselves.
    admin_id = ok(admin.get("/api/me"))["id"]
    assert (
        admin.patch(f"/api/users/{admin_id}", json={"role": "editor"}).json()["error"]["code"]
        == "last_admin"
    )
    assert admin.patch(f"/api/users/{admin_id}", json={"isActive": False}).status_code == 409
    bad = admin.patch(f"/api/users/{vera_id}", json={"attributes": {"bad key": 1}})
    assert (
        bad.status_code == 422
        and bad.json()["error"]["details"][0]["path"] == "/attributes/bad key"
    )

    # Groups.
    group = ok(admin.post("/api/groups", json={"name": "Finance", "memberIds": [vera_id]}), 201)
    assert [m["email"] for m in group["members"]] == ["vera@example.test"]
    assert admin.post("/api/groups", json={"name": "finance"}).status_code == 409
    listed = {u["email"]: u for u in ok(admin.get("/api/users"))}
    assert listed["vera@example.test"]["groups"] == [{"id": group["id"], "name": "Finance"}]
    assert ok(vera.get("/api/me"))["groups"] == [group["id"]]

    # Deactivating someone ends their sessions and their tokens.
    vera_token = tokens.create(
        db,
        users.to_principal(db.get(User, uuid.UUID(vera_id)), "session"),
        name="t",
        scopes=["read"],
        days=1,  # type: ignore[arg-type]
    )[1]
    assert (
        client_for(**SIGN_IN, csrf=False)
        .get("/api/me", headers={"Authorization": f"Bearer {vera_token}"})
        .status_code
        == 200
    )
    changed = ok(admin.patch(f"/api/users/{vera_id}", json={"isActive": False, "role": "editor"}))
    assert changed["status"] == "deactivated" and changed["role"] == "editor"
    assert vera.get("/api/me").status_code == 401
    assert (
        client_for(**SIGN_IN, csrf=False)
        .get("/api/me", headers={"Authorization": f"Bearer {vera_token}"})
        .status_code
        == 401
    )
    assert ok(admin.patch(f"/api/users/{vera_id}", json={"isActive": True}))["status"] == "active"
    admin.delete(f"/api/groups/{group['id']}")
    assert ok(admin.get("/api/groups")) == []

    # Recovery from the command line when no admin can sign in.
    url = accounts.reset_link_for_email(db, make_settings(**SIGN_IN), ADMIN)
    assert (
        ok(client_for(**SIGN_IN).get(f"/api/auth/links/{url.rsplit('/', 1)[1]}"))["purpose"]
        == "reset"
    )


def test_builtin_admin_and_its_tokens_stop_when_sign_in_starts(
    client_for: ClientFor, db: Session
) -> None:
    local = client_for()
    token = ok(local.post("/api/tokens", json={"name": "old", "scopes": ["read"]}), 201)["token"]
    assert local.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    signed = client_for(**SIGN_IN, csrf=False)
    assert (
        signed.get("/api/dashboards", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )
    builtin = db.scalar(select(User).where(User.is_builtin))
    assert builtin is not None and builtin.is_active is False


# ---- access to dashboards and datasets -----------------------------------------------------------


def test_dashboard_access_lists(client_for: ClientFor, source: dict[str, Any], db: Session) -> None:  # noqa: F811
    admin = admin_client(client_for)
    load(admin, source)
    ok(admin.put("/api/models/gifts", json={"yaml": MODEL_YAML}))
    viewer, viewer_id = person(client_for, admin, "viewer@example.test", "viewer")
    editor, editor_id = person(
        client_for, admin, "editor@example.test", "editor", {"regions": ["VIC"]}
    )
    other, other_id = person(client_for, admin, "other@example.test", "editor")
    dashboard = ok(
        admin.post("/api/dashboards", json={"title": "Private", "spec": simple_spec("Private")}),
        201,
    )["id"]

    def share(*shares: tuple[str, str]) -> Any:
        return ok(admin.put(f"/api/dashboards/{dashboard}/access", json={
            "shares": [{"grantee": g, "role": r} for g, r in shares]}))  # fmt: skip

    # Not shared: invisible to everyone but the owner and admins.
    for client in (viewer, editor):
        assert ok(client.get("/api/dashboards")) == []
        assert client.get(f"/api/dashboards/{dashboard}").status_code == 404
    # A viewer shared with an unpublished dashboard still sees nothing.
    share((f"user:{viewer_id}", "viewer"))
    assert ok(viewer.get("/api/dashboards")) == []
    ok(admin.post(f"/api/dashboards/{dashboard}/publish"))
    seen = ok(viewer.get(f"/api/dashboards/{dashboard}"))
    assert (seen["source"], seen["canEdit"], seen["canShare"]) == ("published", False, False)
    assert (
        viewer.put(f"/api/dashboards/{dashboard}", json={"spec": simple_spec()}).status_code == 403
    )
    assert (
        ok(viewer.post(f"/api/dashboards/{dashboard}/data", json={"itemId": "t"}))["rows"] == []
    )  # RLS

    # An editor shared as a viewer views the published version only.
    share((f"user:{viewer_id}", "viewer"), (f"user:{editor_id}", "viewer"))
    as_viewer = ok(editor.get(f"/api/dashboards/{dashboard}"))
    assert as_viewer["source"] == "published" and as_viewer["canEdit"] is False
    refused = editor.put(f"/api/dashboards/{dashboard}", json={"spec": simple_spec("Mine now")})
    assert refused.status_code == 403 and "Ask its owner" in refused.json()["error"]["message"]
    assert editor.get(f"/api/dashboards/{dashboard}/versions").status_code == 403
    assert (
        editor.post(
            f"/api/dashboards/{dashboard}/data",
            json={"itemId": "t", "source": "inline", "spec": simple_spec()},
        ).status_code
        == 403
    )

    # Through a group, as an editor: the draft, saving, publishing, sharing; not deleting.
    group = ok(admin.post("/api/groups", json={"name": "Builders", "memberIds": [editor_id]}), 201)
    share((f"user:{viewer_id}", "viewer"), (f"group:{group['id']}", "editor"))
    assert ok(editor.get(f"/api/dashboards/{dashboard}"))["canEdit"] is True
    saved = ok(
        editor.put(
            f"/api/dashboards/{dashboard}", json={"spec": simple_spec("Edited"), "baseVersion": 1}
        )
    )
    assert saved["version"] == 2
    # Viewers see the published version, title included: the draft does not leak.
    seen_now = ok(viewer.get(f"/api/dashboards/{dashboard}"))
    assert (seen_now["title"], seen_now["spec"]["title"]) == ("Private", "Private")
    assert [d["title"] for d in ok(viewer.get("/api/dashboards"))] == ["Private"]
    assert [d["title"] for d in ok(editor.get("/api/dashboards"))] == ["Edited"]
    assert editor.delete(f"/api/dashboards/{dashboard}").status_code == 403
    rows = ok(editor.post(f"/api/dashboards/{dashboard}/data", json={"itemId": "t"}))["rows"]
    assert {r[0] for r in rows} == {"VIC"}

    # Everyone, as a viewer; and a viewer-role person shared as an editor is still a viewer.
    share(("everyone", "viewer"), (f"user:{viewer_id}", "editor"))
    assert [d["id"] for d in ok(other.get("/api/dashboards"))] == [dashboard]
    assert ok(viewer.get(f"/api/dashboards/{dashboard}"))["canEdit"] is False
    assert viewer.get(f"/api/dashboards/{dashboard}/events").status_code == 403
    assert other.get(f"/api/dashboards/{dashboard}/access").status_code == 403
    assert (
        admin.put(
            f"/api/dashboards/{dashboard}/access",
            json={"shares": [{"grantee": "user:nobody", "role": "viewer"}]},
        ).status_code
        == 422
    )
    assert {s["kind"] for s in ok(admin.get(f"/api/dashboards/{dashboard}/access"))["shares"]} == {
        "everyone",
        "user",
    }
    directory = ok(editor.get("/api/directory"))
    assert {u["email"] for u in directory["users"]} >= {ADMIN, "viewer@example.test"} and directory[
        "groups"
    ][0]["name"] == "Builders"
    assert viewer.get("/api/directory").status_code == 403
    assert other_id


def test_restricted_datasets(client_for: ClientFor, source: dict[str, Any], db: Session) -> None:  # noqa: F811
    admin = admin_client(client_for)
    ids = load(admin, source)
    ok(admin.put("/api/models/gifts", json={"yaml": MODEL_YAML}))
    editor, editor_id = person(client_for, admin, "editor@example.test", "editor")
    before = ok(admin.get(f"/api/datasets/{ids['donors']}/access"))
    assert (before["restricted"], before["grants"], before["canManage"]) == (False, [], True)
    ok(admin.put(f"/api/datasets/{ids['donors']}/access", json={"restricted": True, "grants": []}))
    assert (
        editor.put(f"/api/datasets/{ids['donors']}/access", json={"restricted": False}).status_code
        == 403
    )

    assert [d["name"] for d in ok(editor.get("/api/datasets"))] == ["gifts"]
    assert [m["dataset"] for m in ok(editor.get("/api/models"))] == ["gifts"]
    for path in (
        f"/api/datasets/{ids['donors']}",
        f"/api/datasets/{ids['donors']}/preview",
        "/api/models/donors",
    ):
        refused = editor.get(path)
        assert (
            refused.status_code == 403
            and "do not have access to the dataset 'donors'" in refused.json()["error"]["message"]
        )
    query = {"dataset": "donors", "measures": ["donors.row_count"]}
    assert (
        editor.post("/api/query", json={"query": query}).json()["error"]["code"]
        == "dataset_forbidden"
    )
    spec = simple_spec(more=[{"id": "d", "visual": "kpi", "position": {"x": 3, "y": 0, "w": 3, "h": 3},
                              "dataset": "donors", "bindings": {"value": ["donors.row_count"]}}])  # fmt: skip
    problems = ok(editor.post("/api/dashboards/validate", json={"spec": spec}))["problems"]
    assert [p["path"] for p in problems] == ["/items/2/dataset"]
    assert editor.post("/api/dashboards", json={"title": "x", "spec": spec}).status_code == 422

    # A dashboard someone else built on it can still be viewed: it grants its own questions.
    built = ok(admin.post("/api/dashboards", json={"title": "With donors", "spec": spec}), 201)[
        "id"
    ]
    ok(admin.post(f"/api/dashboards/{built}/publish"))
    ok(
        admin.put(
            f"/api/dashboards/{built}/access",
            json={"shares": [{"grantee": f"user:{editor_id}", "role": "viewer"}]},
        )
    )
    assert ok(
        editor.post(f"/api/dashboards/{built}/data", json={"itemId": "d", "source": "published"})
    )["rows"] == [[40]]

    # A grant (here through a group) gives access.
    group = ok(
        admin.post("/api/groups", json={"name": "Fundraising", "memberIds": [editor_id]}), 201
    )
    ok(
        admin.put(
            f"/api/datasets/{ids['donors']}/access",
            json={"restricted": True, "grants": [f"group:{group['id']}"]},
        )
    )
    assert ok(editor.post("/api/query", json={"query": query}))["rows"] == [[40]]
    assert ok(editor.post("/api/dashboards/validate", json={"spec": spec}))["valid"] is True


# ---- access tokens on the REST API -----------------------------------------------------------


def test_access_tokens_on_the_rest_api(
    client_for: ClientFor, source: dict[str, Any], db: Session
) -> None:  # noqa: F811
    admin = admin_client(client_for)
    load(admin, source)
    ok(admin.put("/api/models/gifts", json={"yaml": MODEL_YAML}))

    def token(*scopes: str) -> dict[str, str]:
        made = ok(admin.post("/api/tokens", json={"name": "script", "scopes": list(scopes)}), 201)
        return {"Authorization": f"Bearer {made['token']}"}

    script = client_for(**SIGN_IN, csrf=False)  # a script: no cookies, no CSRF token
    read, edit, publish, full = (
        token("read"),
        token("read", "edit"),
        token("read", "edit", "publish"),
        token("read", "edit", "publish", "admin"),
    )
    query = {"query": {"dataset": "gifts", "measures": ["gifts.total_amount"]}}
    assert script.get("/api/dashboards", headers=read).status_code == 200
    assert (
        script.post("/api/query", json=query, headers=read).status_code == 200
    )  # a POST that only reads
    refused = script.post("/api/dashboards", json={"title": "x"}, headers=read)
    assert refused.status_code == 403 and "`edit` scope" in refused.json()["error"]["message"]
    created = ok(
        script.post(
            "/api/dashboards", json={"title": "From a script", "spec": simple_spec()}, headers=edit
        ),
        201,
    )
    assert script.post(f"/api/dashboards/{created['id']}/publish", headers=edit).status_code == 403
    assert (
        script.post(f"/api/dashboards/{created['id']}/publish", headers=publish).status_code == 200
    )
    assert script.get("/api/users", headers=publish).status_code == 403
    assert script.get("/api/users", headers=full).status_code == 200
    assert (
        script.post(
            "/api/connections",
            json={"name": "x", "connectorId": "file", "config": {}},
            headers=edit,
        ).status_code
        == 403
    )  # admin work
    # Tokens cannot make tokens or change passwords; a wrong token never falls back to a cookie.
    assert (
        script.post("/api/tokens", json={"name": "y", "scopes": ["read"]}, headers=full).status_code
        == 403
    )
    assert (
        script.post(
            "/api/auth/password",
            json={"currentPassword": CHANGED, "newPassword": "x" * 20},
            headers=full,
        ).status_code
        == 403
    )
    assert (
        admin.get("/api/me", headers={"Authorization": "Bearer opsis_pat_wrong"}).status_code == 401
    )
    assert admin.get("/api/me", headers={"Authorization": "Basic abc"}).status_code == 401


# ---- share links ------------------------------------------------------------------------------


def test_share_links(client_for: ClientFor, source: dict[str, Any], db: Session) -> None:  # noqa: F811
    admin = admin_client(client_for, **SERVER_SIGN_IN)
    load(admin, source)
    ok(admin.put("/api/models/gifts", json={"yaml": MODEL_YAML}))
    dashboard = ok(
        admin.post("/api/dashboards", json={"title": "Linked", "spec": simple_spec("Linked")}), 201
    )["id"]
    unpublished = admin.post(f"/api/dashboards/{dashboard}/links", json={})
    assert (
        unpublished.status_code == 409
        and "Publish the dashboard first" in unpublished.json()["error"]["message"]
    )
    ok(admin.post(f"/api/dashboards/{dashboard}/publish"))

    made = ok(
        admin.post(
            f"/api/dashboards/{dashboard}/links", json={"name": "Board", "expiresInDays": 30}
        ),
        201,
    )
    assert made["url"].startswith("https://bi.test/opsis/s/") and made["attributes"] == {}
    token = made["url"].rsplit("/", 1)[1]
    assert len(base64.urlsafe_b64decode(token + "==")) == 32
    stored = db.scalar(select(ShareLink))
    assert stored is not None and stored.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert "url" not in ok(admin.get(f"/api/dashboards/{dashboard}/links"))[0]

    anonymous = client_for(**SERVER_SIGN_IN)
    opened = ok(anonymous.get(f"/api/public/links/{token}"))
    assert opened["needsPassword"] is False and opened["spec"]["title"] == "Linked"
    # Row-level security: a link has no attributes by default, so the rule shows nothing.
    kpi = ok(anonymous.post(f"/api/public/links/{token}/data", json={"itemId": "k"}))
    assert kpi["rows"] == [[None]] and "query" not in kpi and "snapshots" not in kpi
    # Only the published version: a saved draft does not leak.
    ok(admin.put(f"/api/dashboards/{dashboard}", json={"spec": simple_spec("Draft only")}))
    assert ok(anonymous.get(f"/api/public/links/{token}"))["spec"]["title"] == "Linked"
    db.expire_all()
    assert db.scalar(select(ShareLink.view_count)) == 2

    # Attributes: an admin may choose them; others may give none or their own.
    vic = ok(
        admin.post(f"/api/dashboards/{dashboard}/links", json={"attributes": {"regions": ["VIC"]}}),
        201,
    )
    rows = ok(
        anonymous.post(
            f"/api/public/links/{vic['url'].rsplit('/', 1)[1]}/data", json={"itemId": "t"}
        )
    )["rows"]
    assert {r[0] for r in rows} == {"VIC"}
    editor, editor_id = person(
        client_for, admin, "editor@example.test", "editor", {"regions": ["NSW"]}, **SERVER_SIGN_IN
    )
    ok(
        admin.put(
            f"/api/dashboards/{dashboard}/access",
            json={"shares": [{"grantee": f"user:{editor_id}", "role": "editor"}]},
        )
    )
    # Since the M6 review, an editor who does not own a dashboard cannot make links for it...
    not_owner = editor.post(f"/api/dashboards/{dashboard}/links", json={})
    assert (
        not_owner.status_code == 403 and "owner or an admin" in not_owner.json()["error"]["message"]
    )
    # ...so the attribute rules are checked on a dashboard of the editor's own.
    hers = ok(
        editor.post("/api/dashboards", json={"title": "Hers", "spec": simple_spec("Hers")}), 201
    )["id"]
    ok(editor.post(f"/api/dashboards/{hers}/publish"))
    widened = editor.post(
        f"/api/dashboards/{hers}/links", json={"attributes": {"regions": ["VIC", "NSW"]}}
    )
    assert (
        widened.status_code == 403
        and "Only an admin can choose" in widened.json()["error"]["message"]
    )
    mine = ok(editor.post(f"/api/dashboards/{hers}/links", json={"attributes": "mine"}), 201)
    assert mine["attributes"] == {"regions": ["NSW"]}

    # A link never inherits an exemption: a rule for editors only still applies to it.
    ok(
        admin.put(
            "/api/models/gifts",
            json={
                "yaml": MODEL_YAML.replace(
                    "applies_to_roles: [viewer, editor]", "applies_to_roles: [editor]"
                )
            },
        )
    )
    assert ok(anonymous.post(f"/api/public/links/{token}/data", json={"itemId": "k"}))["rows"] == [
        [None]
    ]

    # A password, with lockouts.
    locked = ok(
        admin.post(f"/api/dashboards/{dashboard}/links", json={"password": "Board-Meeting-2026"}),
        201,
    )
    secret = locked["url"].rsplit("/", 1)[1]
    assert ok(anonymous.get(f"/api/public/links/{secret}")) == {"needsPassword": True}
    assert (
        anonymous.post(f"/api/public/links/{secret}/data", json={"itemId": "k"}).status_code == 401
    )
    for _ in range(5):
        assert (
            anonymous.post(
                f"/api/public/links/{secret}/unlock", json={"password": "guess-guess"}
            ).status_code
            == 401
        )
    assert (
        anonymous.post(
            f"/api/public/links/{secret}/unlock", json={"password": "Board-Meeting-2026"}
        ).status_code
        == 429
    )
    db.execute(update(LoginAttempt).values(locked_until=None, failures=0))
    db.commit()
    unlocked = anonymous.post(
        f"/api/public/links/{secret}/unlock", json={"password": "Board-Meeting-2026"}
    )
    cookie = next(c for c in cookies_set(unlocked) if c.startswith("opsis_link_"))
    assert "HttpOnly" in cookie and "Secure" in cookie and "Path=/opsis" in cookie
    assert ok(anonymous.get(f"/api/public/links/{secret}"))["spec"]["title"] == "Linked"

    # Revoked, expired, or the dashboard unpublished: the link stops working.
    ok(admin.post(f"/api/links/{made['id']}/revoke"))
    gone = anonymous.get(f"/api/public/links/{token}")
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "link_not_available"
    db.execute(
        update(ShareLink)
        .where(ShareLink.id == uuid.UUID(vic["id"]))
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    db.commit()
    assert anonymous.get(f"/api/public/links/{vic['url'].rsplit('/', 1)[1]}").status_code == 404
    ok(admin.post(f"/api/dashboards/{dashboard}/unpublish"))
    assert anonymous.get(f"/api/public/links/{secret}").status_code == 404
    statuses = {
        link["id"]: link["status"] for link in ok(admin.get(f"/api/dashboards/{dashboard}/links"))
    }
    assert statuses[made["id"]] == "revoked" and statuses[vic["id"]] == "expired"
    actions = {a.action for a in db.scalars(select(AuditLog)).all()}
    assert {"link.created", "link.revoked", "dashboard.shared"} <= actions


# ---- View as -----------------------------------------------------------------------------------


def test_view_as(client_for: ClientFor, source: dict[str, Any], db: Session) -> None:  # noqa: F811
    admin = admin_client(client_for)
    load(admin, source)
    ok(admin.put("/api/models/gifts", json={"yaml": MODEL_YAML}))
    editor, editor_id = person(
        client_for, admin, "editor@example.test", "editor", {"regions": ["VIC"]}
    )
    dashboard = ok(admin.post("/api/dashboards", json={"title": "V", "spec": simple_spec()}), 201)[
        "id"
    ]

    def regions(client: TestClient, view_as: dict[str, Any] | None) -> tuple[set[str], bool]:
        body = ok(
            client.post(
                f"/api/dashboards/{dashboard}/data", json={"itemId": "t", "viewAs": view_as}
            )
        )
        return {row[0] for row in body["rows"]}, body["rlsBypassed"]

    assert regions(admin, None) == ({"VIC", "NSW", "QLD"}, True)  # the admin is exempt, and told so
    assert regions(admin, {"userId": editor_id}) == ({"VIC"}, False)
    assert regions(admin, {"role": "viewer", "attributes": {"regions": ["NSW", "QLD"]}}) == (
        {"NSW", "QLD"},
        False,
    )
    ok(
        admin.put(
            f"/api/dashboards/{dashboard}/access",
            json={"shares": [{"grantee": "everyone", "role": "editor"}]},
        )
    )
    refused = editor.post(
        f"/api/dashboards/{dashboard}/data", json={"itemId": "t", "viewAs": {"userId": editor_id}}
    )
    assert refused.status_code == 403 and "Only an admin" in refused.json()["error"]["message"]


# ---- Sign in with Google ------------------------------------------------------------------------


class FakeGoogle:
    """An OpenID Connect provider that issues real RS256 tokens and checks PKCE."""

    issuer = "https://idp.test"

    def __init__(self) -> None:
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
        self.codes: dict[str, tuple[dict[str, Any], str]] = {}

    def get_json(self, url: str) -> dict[str, Any]:
        if url.endswith("/.well-known/openid-configuration"):
            return {
                "issuer": self.issuer,
                "authorization_endpoint": f"{self.issuer}/authorize",
                "token_endpoint": f"{self.issuer}/token",
                "jwks_uri": f"{self.issuer}/jwks",
            }
        assert url == f"{self.issuer}/jwks"
        return {"keys": [self.key.as_dict(private=False)]}

    def authorize(self, location: str, email: str, **claims: Any) -> tuple[str, str]:
        """The user picks an account. Returns (code, state) for the callback."""
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))
        assert query["code_challenge_method"] == "S256" and query["scope"] == "openid email profile"
        code = f"code-{len(self.codes)}"
        self.codes[code] = (
            {
                "iss": self.issuer, "aud": query["client_id"], "sub": f"sub-{email}", "email": email,
                "email_verified": True, "name": email.split("@")[0].title(), "nonce": query["nonce"],
                "iat": int(datetime.now(UTC).timestamp()), "exp": int(datetime.now(UTC).timestamp()) + 300,
                **claims,
            },
            query["code_challenge"],
        )  # fmt: skip
        return code, query["state"]

    def post_form(self, url: str, form: dict[str, str]) -> dict[str, Any]:
        assert url == f"{self.issuer}/token" and form["grant_type"] == "authorization_code"
        claims, challenge = self.codes.pop(form["code"])
        digest = hashlib.sha256(form["code_verifier"].encode()).digest()
        assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == challenge  # PKCE
        assert form["client_secret"] == "test-client-key-0099"
        return {"id_token": jwt.encode({"alg": "RS256", "kid": "k1"}, claims, self.key)}


def test_sign_in_with_google(
    client_for: ClientFor, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeGoogle()
    monkeypatch.setattr(google, "_cache", {})
    monkeypatch.setattr(google, "_get_json", idp.get_json)
    monkeypatch.setattr(google, "_post_form", idp.post_form)
    full = {
        "auth_mode": "full", "google_client_id": "client-123", "google_client_secret": "test-client-key-0099",
        "google_allowed_domains": "allowed.test", "google_discovery_url": f"{idp.issuer}/.well-known/openid-configuration",
    }  # fmt: skip
    admin = admin_client(client_for, **full)
    ok(admin.post("/api/users", json={"email": "pat@allowed.test", "role": "editor"}), 201)
    ok(admin.post("/api/users", json={"email": "eve@other.test", "role": "editor"}), 201)

    def attempt(
        email: str, client: TestClient | None = None, **claims: Any
    ) -> tuple[TestClient, str]:
        browser = client or client_for(**{**SIGN_IN, **full})
        started = browser.get("/api/auth/google/start?next=/dashboards", follow_redirects=False)
        assert started.status_code == 303 and any(
            c.startswith("opsis_oauth=") for c in cookies_set(started)
        )
        location = started.headers["location"]
        assert location.startswith(f"{idp.issuer}/authorize?") and "hd=allowed.test" in location
        code, state = idp.authorize(location, email, **claims)
        done = browser.get(
            f"/api/auth/google/callback?code={code}&state={state}", follow_redirects=False
        )
        assert done.status_code == 303
        return browser, done.headers["location"]

    browser, location = attempt("pat@allowed.test")
    assert location == "/dashboards" and ok(browser.get("/api/me"))["email"] == "pat@allowed.test"
    linked = db.scalar(select(User).where(User.email == "pat@allowed.test"))
    assert linked is not None and linked.google_sub == "sub-pat@allowed.test"

    # Refusals, each with its reason on the sign-in page.
    assert (
        attempt("eve@other.test")[1] == "/login?error=google_not_allowed"
    )  # invited, wrong domain
    assert attempt("nobody@allowed.test")[1] == "/login?error=google_not_invited"
    assert attempt("pat@allowed.test", email_verified=False)[1] == "/login?error=google_unverified"
    assert attempt("pat@allowed.test", nonce="replayed")[1] == "/login?error=google_failed"
    assert attempt("pat@allowed.test", aud="another-app")[1] == "/login?error=google_failed"
    stranger = client_for(**{**SIGN_IN, **full})
    started = stranger.get("/api/auth/google/start", follow_redirects=False)
    code, _ = idp.authorize(started.headers["location"], "pat@allowed.test")
    forged = stranger.get(
        f"/api/auth/google/callback?code={code}&state=forged", follow_redirects=False
    )
    assert forged.headers["location"] == "/login?error=google_expired"
    elsewhere = client_for(
        **{**SIGN_IN, **full}
    )  # a callback in a browser that did not start the flow
    assert (
        elsewhere.get(
            f"/api/auth/google/callback?code=x&state={'s' * 40}", follow_redirects=False
        ).headers["location"]
        == "/login?error=google_expired"
    )
    reasons = [
        a.detail.get("reason")
        for a in db.scalars(select(AuditLog).where(AuditLog.action == "auth.login_failed"))
    ]
    assert {"google_not_allowed", "google_not_invited", "google_unverified"} <= set(reasons)

    # Auto-provisioning creates a viewer, for allowed addresses only.
    auto = {**full, "google_auto_provision": True}
    browser, location = attempt("new@allowed.test", client_for(**{**SIGN_IN, **auto}))
    assert location == "/dashboards" and ok(browser.get("/api/me"))["role"] == "viewer"
    # Google sign-in is not offered in AUTH_MODE=local.
    assert (
        client_for(**SIGN_IN).get("/api/auth/google/start", follow_redirects=False).status_code
        == 404
    )


# ---- the audit log, and local mode ------------------------------------------------------------------


def test_audit_log(client_for: ClientFor, db: Session) -> None:
    admin = admin_client(client_for)
    viewer, _ = person(client_for, admin, "viewer@example.test")
    viewer.post("/api/auth/login", json={"email": "viewer@example.test", "password": "wrong-wrong"})
    page = ok(admin.get("/api/audit?limit=500"))
    actions = [e["action"] for e in page["entries"]]
    assert {
        "user.first_admin",
        "auth.login",
        "auth.login_failed",
        "user.invited",
        "user.password_set_by_invite",
    } <= set(actions)
    assert page["entries"][0]["id"] > page["entries"][-1]["id"]  # newest first
    only_auth = ok(admin.get("/api/audit?action=auth."))["entries"]
    assert only_auth and all(e["action"].startswith("auth.") for e in only_auth)
    by_viewer = ok(admin.get("/api/audit?actor=viewer@"))["entries"]
    assert by_viewer and all(e["actorEmail"] == "viewer@example.test" for e in by_viewer)
    first = ok(admin.get("/api/audit?limit=2"))
    assert len(first["entries"]) == 2 and first["next"] == first["entries"][-1]["id"]
    assert (
        ok(admin.get(f"/api/audit?limit=2&before={first['next']}"))["entries"][0]["id"]
        < first["next"]
    )
    assert viewer.get("/api/audit").status_code == 403


def test_local_mode_exposes_none_of_it(client_for: ClientFor) -> None:
    local = client_for()
    me = ok(local.get("/api/me"))
    assert me["authMethod"] == "none" and me["mustChangePassword"] is False
    dashboard = ok(local.post("/api/dashboards", json={"title": "x"}), 201)["id"]
    for method, path in (
        ("post", "/api/auth/login"),
        ("get", "/api/auth/google/start"),
        ("get", "/api/users"),
        ("get", "/api/groups"),
        ("get", "/api/directory"),
        ("get", f"/api/dashboards/{dashboard}/access"),
        ("get", f"/api/dashboards/{dashboard}/links"),
        ("get", "/api/public/links/anything"),
        ("get", f"/api/datasets/{uuid.uuid4()}/access"),
    ):
        response = getattr(local, method)(path)
        assert response.status_code in (404, 405), (path, response.status_code)
    assert local.get("/api/audit").status_code == 200  # the audit log is read-only and admin-only
    # Share links stay off in a sign-in mode too, unless server mode turns them on.
    assert admin_client(client_for).get(f"/api/dashboards/{dashboard}/links").status_code == 404
