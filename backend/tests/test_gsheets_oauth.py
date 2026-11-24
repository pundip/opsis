"""Google Sheets through the person's own OAuth client (installed-app style),
against a fake Google: a real token endpoint on a local port (google-auth and
requests-oauthlib talk to it over HTTP), plus the Sheets and Drive APIs through
the connector's test transport."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from opsis.connectors import gsheets
from opsis.db.models import AuditLog, Connection, Job, Schedule
from opsis.errors import ApiError
from opsis.services import crypto, google_oauth, schedules
from opsis.services.datasets import REFRESH_JOB
from tests.test_auth import SERVER_SIGN_IN, admin_client
from tests.test_email import Inbox
from tests.test_email import smtp as smtp  # noqa: F401  (fixture)
from tests.test_gsheets import SHEET, FakeSheets
from tests.test_ingestion import make_dataset, ok, run_jobs, settings_of

ClientFor = Callable[..., TestClient]
CLIENT_ID = "1234-fake.apps.googleusercontent.com"
CLIENT_SECRET = "fake-client-secret"
ACCOUNT = "vera@gmail.test"
READ, LIST = gsheets.SCOPE, gsheets.LIST_SCOPE


class FakeGoogle:
    """Google's OAuth side: consent (in the test, standing in for the browser),
    the token endpoint, and the access tokens it has issued."""

    def __init__(self) -> None:
        self.codes: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: dict[str, dict[str, Any]] = {}
        self.access_tokens: dict[str, dict[str, Any]] = {}
        self.token_requests: list[dict[str, str]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def client_file(self, kind: str = "installed", redirect_uris: list[str] | None = None) -> str:
        return json.dumps(
            {
                kind: {
                    "client_id": CLIENT_ID,
                    "project_id": "opsis-test",
                    "auth_uri": f"{self.url}/auth",
                    "token_uri": f"{self.url}/token",
                    "client_secret": CLIENT_SECRET,
                    "redirect_uris": ["http://localhost"]
                    if redirect_uris is None
                    else redirect_uris,
                }
            }
        )

    def consent(
        self, auth_url: str, scopes: list[str] | None = None, account: str = ACCOUNT
    ) -> str:
        """What the person's browser does: allow, and come back with a code."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(auth_url).query).items()}
        assert auth_url.startswith(f"{self.url}/auth?")
        assert query["client_id"] == CLIENT_ID and query["response_type"] == "code"
        assert set(query["scope"].split()) == {READ, LIST}
        assert (query["access_type"], query["prompt"]) == ("offline", "consent")
        assert query["code_challenge_method"] == "S256" and len(query["code_challenge"]) == 43
        code = secrets.token_urlsafe(16)
        self.codes[code] = {
            "challenge": query["code_challenge"],
            "redirect_uri": query["redirect_uri"],
            "scopes": scopes or [READ, LIST],
            "account": account,
        }
        answer = {"state": query["state"], "code": code, "scope": " ".join(scopes or [READ, LIST])}
        return f"{query['redirect_uri']}?{urlencode(answer)}"

    def mint(self, scopes: list[str], account: str = ACCOUNT) -> str:
        """A refresh token as an earlier Python script got it (for token.json)."""
        token = "1//" + secrets.token_urlsafe(24)
        self.refresh_tokens[token] = {"scopes": scopes, "account": account, "revoked": False}
        return token

    def revoke(self) -> None:
        for token in self.refresh_tokens.values():
            token["revoked"] = True

    def _access(self, scopes: list[str], account: str) -> dict[str, Any]:
        access = "ya29." + secrets.token_urlsafe(24)
        self.access_tokens[access] = {"scopes": scopes, "account": account}
        return {"access_token": access, "expires_in": 3599, "scope": " ".join(scopes),
                "token_type": "Bearer"}  # fmt: skip

    def token(self, form: dict[str, str], basic: str | None) -> tuple[int, dict[str, Any]]:
        self.token_requests.append(form)
        client_id, client_secret = form.get("client_id"), form.get("client_secret")
        if basic:
            client_id, _, client_secret = base64.b64decode(basic).decode().partition(":")
        if (client_id, client_secret) != (CLIENT_ID, CLIENT_SECRET):
            return 401, {
                "error": "invalid_client",
                "error_description": "The OAuth client was not found.",
            }
        if form.get("grant_type") == "authorization_code":
            grant = self.codes.pop(form.get("code", ""), None)
            verifier = form.get("code_verifier", "")
            digest = hashlib.sha256(verifier.encode()).digest()
            challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            if (
                grant is None
                or grant["redirect_uri"] != form.get("redirect_uri")
                or grant["challenge"] != challenge
            ):
                return 400, {"error": "invalid_grant", "error_description": "Bad Request"}
            refresh = self.mint(grant["scopes"], grant["account"])
            return 200, {
                **self._access(grant["scopes"], grant["account"]),
                "refresh_token": refresh,
            }
        if form.get("grant_type") == "refresh_token":
            known = self.refresh_tokens.get(form.get("refresh_token", ""))
            if known is None or known["revoked"]:
                return 400, {
                    "error": "invalid_grant",
                    "error_description": "Token has been expired or revoked.",
                }
            return 200, self._access(known["scopes"], known["account"])
        return 400, {"error": "unsupported_grant_type"}

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        google = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
                grant = google.access_tokens.get(query.get("access_token", ""))
                found = urlsplit(self.path).path == "/tokeninfo" and grant is not None
                answer = (
                    {"aud": CLIENT_ID, "scope": " ".join(grant["scopes"])}
                    if found and grant
                    else {"error": "invalid_token"}
                )
                self._send(200 if found else 400, answer)

            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                form = {k: v[0] for k, v in parse_qs(body).items()}
                auth = self.headers.get("Authorization") or ""
                basic = auth[6:] if auth.startswith("Basic ") else None
                status, answer = google.token(form, basic) if self.path == "/token" else (404, {})
                self._send(status, answer)

            def _send(self, status: int, answer: dict[str, Any]) -> None:
                data = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        return Handler

    def api(self, request: httpx2.Request) -> httpx2.Response:
        """Sheets and Drive, for the access tokens this fake issued."""
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        grant = self.access_tokens.get(bearer)
        if grant is None:
            return httpx2.Response(
                401, json={"error": {"code": 401, "message": "Invalid Credentials"}}
            )
        path = request.url.path
        if path.startswith("/drive/v3/"):
            if not set(grant["scopes"]) & gsheets.LIST_SCOPES:
                return httpx2.Response(
                    403,
                    json={
                        "error": {
                            "code": 403,
                            "message": "Request had insufficient authentication scopes.",
                        }
                    },
                )
            if path == "/drive/v3/about":
                return httpx2.Response(
                    200, json={"user": {"emailAddress": grant["account"], "displayName": "Vera"}}
                )
            files = [
                {"id": SHEET, "name": "Giving", "modifiedTime": "2026-11-20T01:00:00Z",
                 "owners": [{"emailAddress": grant["account"]}]},
                {"id": "1" + "z" * 30, "name": "Budget 2027", "modifiedTime": "2026-11-01T01:00:00Z",
                 "owners": [{"emailAddress": "boss@gmail.test"}]},
            ]  # fmt: skip
            query = request.url.params.get("q", "")
            if "name contains 'Giv'" in query:
                files = files[:1]
            return httpx2.Response(200, json={"files": files})
        if not set(grant["scopes"]) & gsheets.READ_SCOPES:
            return httpx2.Response(
                403, json={"error": {"code": 403, "message": "insufficient scopes"}}
            )
        retold = httpx2.Request(
            request.method,
            request.url,
            headers={**request.headers, "authorization": "Bearer test-token"},
        )
        return FakeSheets().handler(retold)


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGoogle]:
    fake = FakeGoogle()
    # The fake token endpoint is plain http; Google's own is https.
    monkeypatch.setenv("OAUTHLIB_INSECURE_TRANSPORT", "1")
    monkeypatch.setattr(gsheets, "DEFAULT_TRANSPORT", httpx2.MockTransport(fake.api))
    try:
        yield fake
    finally:
        fake.server.shutdown()


def oauth_connection(client: TestClient, google: FakeGoogle, **config: Any) -> str:
    made = client.post(
        "/api/connections",
        json={
            "name": f"Sheets {uuid.uuid4().hex[:6]}",
            "connectorId": "gsheets",
            "config": {
                "auth": "oauth",
                "client_secrets": google.client_file(),
                "spreadsheet": SHEET,
                **config,
            },
        },
    )
    return str(ok(made, 201)["id"])


def connect(
    client: TestClient, google: FakeGoogle, connection: str, **consent: Any
) -> dict[str, Any]:
    started = ok(client.post(f"/api/connections/{connection}/google/start"))
    back = google.consent(started["authUrl"], **consent)
    return ok(client.post("/api/connections/google/finish", json={"url": back}))  # type: ignore[no-any-return]


def stored(db: Session, client: TestClient, connection: str) -> dict[str, Any]:
    row = db.get(Connection, uuid.UUID(connection))
    assert row is not None
    db.refresh(row)
    return dict(crypto.decrypt(settings_of(client), row.secrets))


# ---- the sign-in -----------------------------------------------------------------------


def test_connect_a_google_account_in_local_mode(
    client_for: ClientFor, google: FakeGoogle, db: Session
) -> None:
    client = client_for()
    connection = oauth_connection(client, google)
    seen = ok(client.get(f"/api/connections/{connection}"))
    assert seen["auth"] is None and seen["secretFields"] == ["client_secrets"]
    assert "client_secrets" not in seen["config"]
    not_yet = ok(client.post(f"/api/connections/{connection}/test"))
    assert not not_yet["ok"] and "not connected to a Google account yet" in not_yet["message"]

    # Local mode: Google may send a Desktop client back to Opsis on its own loopback address.
    started = ok(client.post(f"/api/connections/{connection}/google/start"))
    assert (started["mode"], started["redirectUri"]) == ("direct", "http://localhost:8080/")
    query = parse_qs(urlsplit(started["authUrl"]).query)
    assert query["redirect_uri"] == ["http://localhost:8080/"]
    assert query["state"][0].startswith(f"opsis-gs.{connection}.")
    back = google.consent(started["authUrl"])
    done = ok(client.post("/api/connections/google/finish", json={"url": back}))
    assert done["auth"]["status"] == "connected" and done["auth"]["account"] == ACCOUNT
    assert done["auth"]["canList"] is True and set(done["auth"]["scopes"]) == {READ, LIST}

    # PKCE: the verifier went to the token endpoint, and matched the challenge.
    exchange = next(r for r in google.token_requests if r["grant_type"] == "authorization_code")
    assert (
        len(exchange["code_verifier"]) >= 43
        and exchange["redirect_uri"] == "http://localhost:8080/"
    )
    # The refresh token is stored encrypted, and appears in no answer.
    secret = stored(db, client, connection)
    refresh = secret[google_oauth.TOKEN]["refresh_token"]
    assert refresh in google.refresh_tokens and google_oauth.PENDING not in secret
    row = db.get(Connection, uuid.UUID(connection))
    assert row is not None and refresh not in (row.secrets or "")
    for path in ("/api/connections", f"/api/connections/{connection}"):
        assert refresh not in client.get(path).text and CLIENT_SECRET not in client.get(path).text
    audit = db.scalars(
        select(AuditLog).where(AuditLog.action == "connection.google_connected")
    ).all()
    assert audit and refresh not in json.dumps([a.detail for a in audit])
    # The same address twice: the flow is finished.
    again = client.post("/api/connections/google/finish", json={"url": back})
    assert again.status_code == 409 and again.json()["error"]["code"] == "flow_not_found"

    # Reading, through a fresh access token each time.
    assert ok(client.post(f"/api/connections/{connection}/test"))["message"].endswith(
        f"reading as {ACCOUNT}."
    )
    assert [s["name"] for s in ok(client.get(f"/api/connections/{connection}/streams"))] == [
        "Gifts"
    ]
    dataset = make_dataset(client, connection, "Gifts", name="oauth_gifts")
    run_jobs(client)
    assert ok(client.get(f"/api/datasets/{dataset['id']}/preview"))["rowCount"] == 4

    # The spreadsheets the account can open, to pick from.
    listed = ok(client.get(f"/api/connections/{connection}/google/spreadsheets"))["spreadsheets"]
    assert [s["name"] for s in listed] == ["Giving", "Budget 2027"] and listed[0]["id"] == SHEET
    searched = ok(client.get(f"/api/connections/{connection}/google/spreadsheets?q=Giv"))[
        "spreadsheets"
    ]
    assert [s["name"] for s in searched] == ["Giving"]


def test_paste_the_address_on_a_server(
    client_for: ClientFor, google: FakeGoogle, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = admin_client(client_for, **SERVER_SIGN_IN)
    connection = oauth_connection(admin, google)
    started = ok(admin.post(f"/api/connections/{connection}/google/start"))
    # A browser on another computer cannot come back to the server's loopback address.
    assert (started["mode"], started["redirectUri"]) == ("paste", "http://localhost")
    back = google.consent(started["authUrl"])
    assert back.startswith("http://localhost?state=opsis-gs.")
    assert (
        ok(admin.post("/api/connections/google/finish", json={"url": f"  {back}  "}))["auth"][
            "status"
        ]
        == "connected"
    )

    def finish(url: str) -> Any:
        return admin.post("/api/connections/google/finish", json={"url": url})

    # Not allowed: Google comes back with an error instead of a code.
    started = ok(admin.post(f"/api/connections/{connection}/google/start"))
    denied = google.consent(started["authUrl"]).split("&code=")[0] + "&error=access_denied"
    refused = finish(denied)
    assert (
        refused.status_code == 400 and "did not allow access" in refused.json()["error"]["message"]
    )
    assert (
        finish("http://localhost/?nothing=here").json()["error"]["code"] == "not_a_google_address"
    )
    assert finish(f"http://localhost?state=opsis-gs.{uuid.uuid4()}.x&code=1").status_code == 404

    # Too late.
    started = ok(admin.post(f"/api/connections/{connection}/google/start"))
    late = google.consent(started["authUrl"])
    monkeypatch.setattr(google_oauth, "_now", lambda: datetime.now(UTC) + timedelta(minutes=16))
    assert finish(late).status_code == 410
    monkeypatch.undo()


def test_web_clients_and_scopes_left_out(client_for: ClientFor, google: FakeGoogle) -> None:
    settings = settings_of(client_for(**SERVER_SIGN_IN))
    _, web = gsheets.client_config(google.client_file("web", ["https://bi.test/opsis/"]))
    assert google_oauth.redirect(settings, "web", web) == ("https://bi.test/opsis/", "direct")
    _, other = gsheets.client_config(google.client_file("web", ["https://elsewhere.test/cb"]))
    assert google_oauth.redirect(settings, "web", other) == ("https://elsewhere.test/cb", "paste")
    _, none = gsheets.client_config(google.client_file("web", []))
    with pytest.raises(ApiError, match="no authorised redirect URIs"):
        google_oauth.redirect(settings, "web", none)

    client = client_for()
    # The person unticks the Drive permission: reading works, listing does not.
    connection = oauth_connection(client, google)
    partly = connect(client, google, connection, scopes=[READ])
    assert partly["auth"]["canList"] is False and partly["auth"]["account"] is None
    listed = client.get(f"/api/connections/{connection}/google/spreadsheets")
    assert listed.status_code == 409 and listed.json()["error"]["code"] == "cannot_list"
    assert [s["name"] for s in ok(client.get(f"/api/connections/{connection}/streams"))] == [
        "Gifts"
    ]
    # Without the Sheets permission there is nothing to keep.
    other = oauth_connection(client, google)
    started = ok(client.post(f"/api/connections/{other}/google/start"))
    missing = client.post(
        "/api/connections/google/finish",
        json={"url": google.consent(started["authUrl"], scopes=[LIST])},
    )
    assert missing.status_code == 400 and missing.json()["error"]["code"] == "scope_missing"

    # The wrong kind of file.
    for wrong, says in (
        ('{"type": "service_account", "client_email": "x@y.test"}', "service account key"),
        ('{"refresh_token": "1//x", "client_id": "x"}', "token.json"),
        ('{"hello": "world"}', "not an OAuth client file"),
    ):
        bad = oauth_connection(client, google, client_secrets=wrong)
        refused = client.post(f"/api/connections/{bad}/google/start")
        assert refused.status_code == 422 and says in refused.json()["error"]["message"]


def test_an_existing_token_json(client_for: ClientFor, google: FakeGoogle) -> None:
    client = client_for()
    connection = oauth_connection(client, google)

    def upload(document: dict[str, Any]) -> Any:
        return client.post(
            f"/api/connections/{connection}/google/token", json={"token": json.dumps(document)}
        )

    script = {  # what google-auth's Credentials.to_json() writes
        "token": "ya29.old", "refresh_token": google.mint([READ, LIST]), "token_uri": f"{google.url}/token",
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "scopes": [READ, LIST],
        "universe_domain": "googleapis.com", "account": "", "expiry": "2026-11-24T00:00:00Z",
    }  # fmt: skip
    used = ok(upload(script))
    assert used["auth"]["status"] == "connected" and used["auth"]["source"] == "token.json"
    assert used["auth"]["account"] == ACCOUNT
    # No "scopes" in it: the renewal says what was granted.
    bare = {k: v for k, v in script.items() if k != "scopes"} | {
        "refresh_token": google.mint([READ])
    }
    assert ok(upload(bare))["auth"]["scopes"] == [READ]
    assert (
        upload({**script, "client_id": "other.apps.googleusercontent.com"}).json()["error"]["code"]
        == "token_other_client"
    )
    assert upload({"token": "ya29.x"}).json()["error"]["code"] == "invalid_token_file"
    google.revoke()
    assert upload(script).json()["error"]["code"] == "token_revoked"


# ---- when Google stops accepting the sign-in ---------------------------------------------


def test_invalid_grant_pauses_schedules_and_emails_the_owner(
    client_for: ClientFor,
    google: FakeGoogle,
    smtp: tuple[Inbox, dict[str, Any]],  # noqa: F811
    db: Session,
) -> None:
    inbox, email = smtp
    admin = admin_client(client_for, **{**email, "feature_scheduled_refresh": True})
    connection = oauth_connection(admin, google)
    connect(admin, google, connection)
    dataset = make_dataset(admin, connection, "Gifts", name="paused_gifts")
    run_jobs(admin)
    ok(admin.put(f"/api/datasets/{dataset['id']}/schedule", json={"cron": "0 6 * * *"}))

    google.revoke()  # the person removed the app's access, or 7 days passed in Testing
    ok(admin.post(f"/api/datasets/{dataset['id']}/refresh", json={}), 202)
    run_jobs(admin)
    job = db.scalars(
        select(Job).where(Job.type == REFRESH_JOB).order_by(Job.created_at.desc())
    ).first()
    assert job is not None and job.status == "failed" and "invalid_grant" in (job.error or "")
    seen = ok(admin.get(f"/api/connections/{connection}"))["auth"]
    assert seen["status"] == "needs_reconnect" and "Testing" in seen["error"]
    [sent] = inbox.to("admin@example.test")
    assert sent["Subject"].startswith("Reconnect Sheets ")
    assert "Their scheduled refreshes are paused" in Inbox.text(sent)
    assert "/connections" in Inbox.text(sent)
    # Once per time it happens.
    ok(admin.post(f"/api/connections/{connection}/test"))
    assert len(inbox.to("admin@example.test")) == 1

    # The schedule is paused: due, but no refresh is queued.
    settings = settings_of(admin)
    db.execute(update(Schedule).values(next_run_at=datetime.now(UTC) - timedelta(minutes=1)))
    db.commit()
    before = len(db.scalars(select(Job).where(Job.type == REFRESH_JOB)).all())
    assert schedules.tick(db, settings) == 0
    assert len(db.scalars(select(Job).where(Job.type == REFRESH_JOB)).all()) == before
    paused = ok(admin.get(f"/api/datasets/{dataset['id']}"))["schedule"]["paused"]
    assert paused and "is reconnected" in paused
    [row] = [r for r in ok(admin.get("/api/schedules"))["schedules"] if r["kind"] == "refresh"]
    assert row["paused"] == paused
    blocked = admin.get(f"/api/connections/{connection}/streams")
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "needs_reconnect"

    # Reconnect: the schedule runs again.
    assert connect(admin, google, connection)["auth"]["status"] == "connected"
    assert ok(admin.get(f"/api/datasets/{dataset['id']}"))["schedule"]["paused"] is None
    db.execute(update(Schedule).values(next_run_at=datetime.now(UTC) - timedelta(minutes=1)))
    db.commit()
    assert schedules.tick(db, settings) == 1
    run_jobs(admin)
    assert ok(admin.get(f"/api/datasets/{dataset['id']}"))["lastJob"]["status"] == "succeeded"


def test_a_new_client_ends_the_saved_sign_in(
    client_for: ClientFor, google: FakeGoogle, db: Session
) -> None:
    client = client_for()
    connection = oauth_connection(client, google)
    connect(client, google, connection)
    name = ok(client.get(f"/api/connections/{connection}"))["name"]
    # Editing other settings keeps it.
    ok(
        client.patch(
            f"/api/connections/{connection}",
            json={
                "name": name,
                "config": {"auth": "oauth", "spreadsheet": SHEET, "has_header": True},
            },
        )
    )
    assert google_oauth.TOKEN in stored(db, client, connection)
    assert ok(client.get(f"/api/connections/{connection}"))["auth"]["status"] == "connected"
    # Another OAuth client: its refresh token was for the old one.
    other = json.loads(google.client_file())
    other["installed"]["client_id"] = "5678-other.apps.googleusercontent.com"
    ok(
        client.patch(
            f"/api/connections/{connection}",
            json={
                "config": {
                    "auth": "oauth",
                    "spreadsheet": SHEET,
                    "client_secrets": json.dumps(other),
                }
            },
        )
    )
    assert google_oauth.TOKEN not in stored(db, client, connection)
    assert ok(client.get(f"/api/connections/{connection}"))["auth"]["status"] == "not_connected"
