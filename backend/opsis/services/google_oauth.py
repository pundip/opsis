"""Google Sheets through the person's own OAuth client, installed-app style.

The connection holds the client (credentials.json, "installed" or "web") as an
encrypted secret. "Connect Google account" runs Google's consent flow with
offline access, prompt=consent, PKCE (S256) and two read-only scopes:
spreadsheets.readonly, and drive.metadata.readonly to list spreadsheets and to
see which account it is. Where Google sends the browser back to:

- **direct**: Opsis's own root. An installed (Desktop) client may use a
  loopback address on any port, so in local mode, where Opsis is at
  http://localhost:PORT with no sub-path, Google redirects straight back. A Web
  client does the same when Opsis's root is one of its authorised redirect URIs.
  The app's root page sees the answer and finishes the flow.
- **paste**: anywhere else (a server, a sub-path). Google sends the browser to a
  loopback address that does not load; the person copies the whole address from
  the address bar into Opsis, which reads the code from it.

Either way the flow is finished by `finish`, from that address. The pending flow
(state, PKCE verifier, redirect URI, who started it) is kept with the
connection's encrypted secrets for 15 minutes; the state carries the
connection's id. The refresh token is stored there too, encrypted, and is never
returned. `auth_state` (not secret) records the account, the scopes granted and
whether it needs reconnecting. When Google answers invalid_grant, the connection
is marked, its scheduled refreshes pause, and its owner is emailed.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets as tokens
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

from sqlalchemy.orm import Session

from opsis.auth.principal import Principal
from opsis.connectors import gsheets
from opsis.connectors.base import ConnectorError, ReconnectNeeded
from opsis.db.models import Connection, User
from opsis.errors import ApiError
from opsis.services import audit, crypto, mail
from opsis.settings import Settings

log = logging.getLogger("opsis.google_oauth")

TOKEN = "__oauth__"
PENDING = "__oauth_pending__"
STATE_PREFIX = "opsis-gs"
FLOW_MINUTES = 15
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
TESTING_NOTE = (
    "If your OAuth app's publishing status in Google Cloud is Testing, Google ends its "
    "refresh tokens after 7 days and the connection has to be reconnected each week. "
    "Setting the app to In production stops that."
)


def _now() -> datetime:
    return datetime.now(UTC)


def _secrets(settings: Settings, connection: Connection) -> dict[str, Any]:
    try:
        return dict(crypto.decrypt(settings, connection.secrets))
    except crypto.SecretsError as exc:
        raise ApiError(409, "secrets_unreadable", str(exc)) from None


def _parts(
    settings: Settings, connection: Connection
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """The connection's OAuth client and its secrets, or a 409/422 saying why not."""
    if connection.connector_id != gsheets.GoogleSheetsConnector.id:
        raise ApiError(409, "not_google", "Only Google Sheets connections sign in to Google.")
    config = connection.config or {}
    secrets = _secrets(settings, connection)
    method = config.get("auth") or (
        "service_account" if "service_account_key" in secrets else "oauth"
    )
    if method != "oauth":
        raise ApiError(
            409,
            "not_oauth",
            "This connection signs in with a service account. Edit it and choose My Google "
            "account (OAuth) to connect an account instead.",
        )
    try:
        kind, client = gsheets.client_config(secrets.get("client_secrets"))
    except ConnectorError as exc:
        raise ApiError(422, "invalid_client_file", str(exc)) from None
    return kind, client, secrets


def redirect(settings: Settings, kind: str, client: dict[str, Any]) -> tuple[str, str]:
    """Where Google sends the browser back to, and whether that is Opsis ("direct")
    or a page the person copies the address from ("paste")."""
    root = settings.public_base_url.rstrip("/") + "/"
    registered = [str(u) for u in client.get("redirect_uris") or []]
    if kind == "installed":
        base = urlsplit(settings.public_base_url)
        if base.scheme == "http" and base.hostname in LOOPBACK_HOSTS and not settings.base_path:
            return root, "direct"  # for example http://localhost:8080/
        loopback = next((u for u in registered if urlsplit(u).hostname in LOOPBACK_HOSTS), None)
        return loopback or "http://localhost", "paste"
    for uri in registered:
        if uri.rstrip("/") == root.rstrip("/"):
            return uri, "direct"
    if registered:
        return registered[0], "paste"
    raise ApiError(
        422,
        "no_redirect_uri",
        f"This Web application client has no authorised redirect URIs. In Google Cloud, add "
        f"{root} to them (or use a Desktop app client), download the JSON again and upload it.",
    )


def _flow(kind: str, client: dict[str, Any], redirect_uri: str, verifier: str | None = None) -> Any:
    from google_auth_oauthlib.flow import Flow

    return Flow.from_client_config(
        {kind: client},
        scopes=list(gsheets.OAUTH_SCOPES),
        redirect_uri=redirect_uri,
        code_verifier=verifier,
        autogenerate_code_verifier=verifier is None,
    )


def start(
    db: Session, settings: Settings, actor: Principal, connection: Connection
) -> dict[str, Any]:
    """Begin "Connect Google account": the address to send the person to."""
    kind, client, secrets = _parts(settings, connection)
    redirect_uri, mode = redirect(settings, kind, client)
    state = f"{STATE_PREFIX}.{connection.id}.{tokens.token_urlsafe(32)}"
    flow = _flow(kind, client, redirect_uri)
    hint = (secrets.get(TOKEN) or {}).get("account")
    extra = {"login_hint": hint} if hint else {}
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", state=state, **extra)
    secrets[PENDING] = {
        "state": state,
        "verifier": flow.code_verifier,
        "redirect_uri": redirect_uri,
        "kind": kind,
        "user": str(actor.user_id) if actor.user_id else None,
        "expires": (_now() + timedelta(minutes=FLOW_MINUTES)).isoformat(),
    }
    connection.secrets = crypto.encrypt(settings, secrets)
    audit.record(
        db,
        action="connection.google_started",
        actor=actor,
        target_type="connection",
        target_id=str(connection.id),
        detail={"name": connection.name, "mode": mode},
    )
    db.commit()
    return {"authUrl": url, "mode": mode, "redirectUri": redirect_uri}


def _first(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name) or [""]
    return values[0].strip()


def finish(db: Session, settings: Settings, actor: Principal, address: str) -> dict[str, Any]:
    """Finish the flow from the address Google sent the browser back to."""
    text = address.strip()
    query = parse_qs(urlsplit(text).query if "?" in text else text.lstrip("?"))
    state, code, error = _first(query, "state"), _first(query, "code"), _first(query, "error")
    not_it = ApiError(
        422,
        "not_a_google_address",
        "That is not the address Google sent you back to. After you allow access, copy the "
        "whole address from the browser's address bar: it has state= and code= in it.",
    )
    parts = state.split(".")
    if len(parts) != 3 or parts[0] != STATE_PREFIX:
        raise not_it
    try:
        connection_id = uuid.UUID(parts[1])
    except ValueError:
        raise not_it from None
    connection = db.get(Connection, connection_id)
    if connection is None:
        raise ApiError(404, "not_found", "The connection this sign-in was for no longer exists.")
    kind, client, secrets = _parts(settings, connection)
    pending = secrets.get(PENDING)
    if not isinstance(pending, dict) or not hmac.compare_digest(str(pending.get("state")), state):
        raise ApiError(
            409,
            "flow_not_found",
            "This sign-in has been replaced by a newer one, or was already finished. Choose "
            "Connect Google account again.",
        )
    if datetime.fromisoformat(str(pending["expires"])) < _now():
        raise ApiError(
            410, "flow_expired", "This sign-in took longer than 15 minutes. Start again."
        )
    if pending.get("user") and actor.user_id and pending["user"] != str(actor.user_id):
        raise ApiError(403, "forbidden", "Someone else started this sign-in.")
    if error or not code:
        secrets.pop(PENDING, None)
        connection.secrets = crypto.encrypt(settings, secrets)
        db.commit()
        if error == "access_denied":
            raise ApiError(400, "access_denied", "You did not allow access, so nothing changed.")
        if error:
            raise ApiError(400, "google_refused", f"Google returned an error: {error}.")
        raise not_it
    from oauthlib.oauth2.rfc6749.errors import OAuth2Error
    from requests import RequestException

    flow = _flow(kind, client, str(pending["redirect_uri"]), str(pending["verifier"]))
    try:
        token = dict(flow.fetch_token(code=code))
    except Warning as changed:  # oauthlib's "Scope has changed": an unticked scope; checked below
        token = dict(getattr(changed, "token", None) or {})
    except OAuth2Error as exc:
        described = f": {exc.description}" if exc.description else ""
        raise ApiError(
            400,
            "google_token_refused",
            f"Google did not accept the sign-in ({exc.error}{described}). Start again.",
        ) from None
    except RequestException as exc:
        raise ApiError(502, "google_unreachable", f"Could not reach Google: {exc}") from None
    return _store(db, settings, actor, connection, secrets, client, token, source="consent")


def use_token_json(
    db: Session, settings: Settings, actor: Principal, connection: Connection, text: str
) -> dict[str, Any]:
    """Use an existing token.json (from a Python script) instead of consenting again."""
    from google.auth.exceptions import RefreshError, TransportError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    kind, client, secrets = _parts(settings, connection)
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    if not isinstance(document, dict) or not document.get("refresh_token"):
        raise ApiError(
            422,
            "invalid_token_file",
            "This is not a token.json with a refresh_token. Connect the Google account instead.",
        )
    if document.get("client_id") and document["client_id"] != client["client_id"]:
        raise ApiError(
            422,
            "token_other_client",
            "This token.json was made with another OAuth client (its client_id differs from "
            "the credentials.json saved here). Upload that client's credentials.json, or "
            "connect again.",
        )
    stated = gsheets.granted(document.get("scopes") or document.get("scope"))
    credentials = Credentials(  # type: ignore[no-untyped-call]
        None,
        refresh_token=str(document["refresh_token"]),
        token_uri=str(client["token_uri"]),
        client_id=str(client["client_id"]),
        client_secret=str(client["client_secret"]),
        scopes=sorted(stated) or None,
    )
    try:
        credentials.refresh(Request())  # type: ignore[no-untyped-call]
    except RefreshError as exc:
        if "invalid_grant" in str(exc):
            raise ApiError(
                400,
                "token_revoked",
                "Google no longer accepts this token.json (invalid_grant). Connect the Google "
                "account instead.",
            ) from None
        raise ApiError(
            400, "google_token_refused", f"Google refused it: {str(exc)[:200]}"
        ) from None
    except (TransportError, OSError) as exc:
        raise ApiError(502, "google_unreachable", f"Could not reach Google: {exc}") from None
    scopes = gsheets.granted(credentials.granted_scopes) or stated
    if not stated:  # a token.json without its scopes: ask Google what the token may do
        scopes = _token_scopes(str(client["token_uri"]), str(credentials.token)) or scopes
    token = {
        "refresh_token": document["refresh_token"],
        "access_token": credentials.token,
        "scope": sorted(scopes),
    }
    return _store(db, settings, actor, connection, secrets, client, token, source="token.json")


def _token_scopes(token_uri: str, access_token: str) -> set[str]:
    """The scopes of an access token, from Google's tokeninfo endpoint (next to the
    token endpoint: https://oauth2.googleapis.com/tokeninfo)."""
    import requests

    url = urlsplit(token_uri)._replace(path="/tokeninfo", query="").geturl()
    try:
        answer = requests.get(url, params={"access_token": access_token}, timeout=20)
        found = answer.json().get("scope") if answer.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return set()
    return gsheets.granted(found)


def _account(settings: Settings, connection: Connection, access_token: str) -> str | None:
    """The account's address, from Drive (any Drive scope may ask)."""
    config = {**(connection.config or {}), "auth": "oauth", "__oauth__": {}}
    if settings.google_api_base_url:
        config["_google_api_base"] = settings.google_api_base_url
    reader = gsheets.GoogleSheetsConnector(config, token=lambda: access_token)
    try:
        return reader.about()["email"] or None
    except ConnectorError as exc:
        log.warning("Could not read the Google account of %s: %s", connection.name, exc)
        return None
    finally:
        reader.close()


def _store(
    db: Session,
    settings: Settings,
    actor: Principal,
    connection: Connection,
    secrets: dict[str, Any],
    client: dict[str, Any],
    token: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    scopes = gsheets.granted(token.get("scope"))
    if not scopes & gsheets.READ_SCOPES:
        raise ApiError(
            400,
            "scope_missing",
            "Google did not allow Opsis to read your spreadsheets. Connect again and leave "
            '"See all your Google Sheets spreadsheets" allowed.',
        )
    refresh_token = token.get("refresh_token")
    if not refresh_token:
        raise ApiError(
            400,
            "no_refresh_token",
            "Google sent no refresh token, so the connection could not be kept. Remove the "
            "app's access at https://myaccount.google.com/permissions, then connect again.",
        )
    can_list = bool(scopes & gsheets.LIST_SCOPES)
    account = _account(settings, connection, str(token.get("access_token"))) if can_list else None
    now = _now().isoformat()
    secrets.pop(PENDING, None)
    secrets[TOKEN] = {
        "refresh_token": str(refresh_token),
        "scopes": sorted(scopes),
        "account": account,
        "client_id": client["client_id"],
        "connected_at": now,
    }
    connection.secrets = crypto.encrypt(settings, secrets)
    connection.auth_state = {
        "method": "oauth",
        "status": "connected",
        "account": account,
        "scopes": sorted(scopes),
        "canList": can_list,
        "connectedAt": now,
        "source": source,
    }
    connection.last_test_ok = connection.last_test_message = connection.last_tested_at = None
    audit.record(
        db,
        action="connection.google_connected",
        actor=actor,
        target_type="connection",
        target_id=str(connection.id),
        detail={
            "name": connection.name,
            "account": account,
            "scopes": sorted(scopes),
            "source": source,
        },
    )
    db.commit()
    return {"connectionId": str(connection.id), "auth": connection.auth_state}


def forget_if_client_changed(
    connection: Connection, before: dict[str, Any], after: dict[str, Any]
) -> None:
    """After an edit: a new OAuth client, or a change to the service account
    method, ends the saved Google sign-in (its refresh token was for the old
    client). Changes `after` in place."""
    if connection.connector_id != gsheets.GoogleSheetsConnector.id or TOKEN not in after:
        return
    method = (connection.config or {}).get("auth") or "oauth"

    def client_id(secrets: dict[str, Any]) -> str | None:
        try:
            return str(gsheets.client_config(secrets.get("client_secrets"))[1]["client_id"])
        except ConnectorError:
            return None

    if method != "oauth" or client_id(before) != client_id(after):
        after.pop(TOKEN, None)
        after.pop(PENDING, None)
        connection.auth_state = {"method": method, "status": "not_connected"}


def mark_reconnect(db: Session, settings: Settings, connection: Connection, message: str) -> None:
    """Google refused the saved sign-in: say so on the connection (which pauses
    its scheduled refreshes) and, once per time it happens, email its owner."""
    state = dict(connection.auth_state or {})
    first = state.get("status") != "needs_reconnect"
    state.update({"method": "oauth", "status": "needs_reconnect", "error": message})
    if first:
        state["failedAt"] = _now().isoformat()
        state.pop("notifiedAt", None)
        audit.record(
            db,
            action="connection.needs_reconnect",
            target_type="connection",
            target_id=str(connection.id),
            detail={"name": connection.name},
        )
    connection.auth_state = state
    db.commit()
    if first and _email_owner(db, settings, connection):
        connection.auth_state = {**state, "notifiedAt": _now().isoformat()}
        db.commit()


def _email_owner(db: Session, settings: Settings, connection: Connection) -> bool:
    if not settings.feature_email or connection.created_by is None:
        return False
    owner = db.get(User, connection.created_by)
    if owner is None or not owner.is_active or owner.is_builtin or "@" not in owner.email:
        return False
    message = mail.reconnect_needed(
        settings, owner.display_name, connection.name, f"{settings.public_base_url}/connections"
    )
    try:
        mail.send(settings, owner.email, message)
    except mail.MailError as exc:
        log.warning("Could not email %s about %s: %s", owner.email, connection.name, exc.message)
        return False
    return True


def needs_reconnect(connection: Connection | None) -> bool:
    return bool(connection and (connection.auth_state or {}).get("status") == "needs_reconnect")


def list_spreadsheets(
    db: Session, settings: Settings, connection: Connection, search: str | None
) -> list[dict[str, Any]]:
    """Spreadsheets the connected account can open, to pick one from."""
    from opsis.services import connections

    _parts(settings, connection)
    state = connection.auth_state or {}
    if state.get("status") == "needs_reconnect":
        raise ApiError(409, "needs_reconnect", str(state.get("error") or gsheets.RECONNECT))
    if state.get("status") != "connected":
        raise ApiError(409, "not_connected", "Connect a Google account first.")
    if not state.get("canList"):
        raise ApiError(
            409,
            "cannot_list",
            "This sign-in may not list your spreadsheets (the Drive metadata permission was "
            "not granted). Paste the spreadsheet's URL instead, or reconnect and allow it.",
        )
    reader = connections.instantiate(settings, connection)
    try:
        found: list[dict[str, Any]] = reader.spreadsheets(search)
    except ReconnectNeeded as exc:
        mark_reconnect(db, settings, connection, str(exc))
        raise ApiError(409, "needs_reconnect", str(exc)) from None
    except ConnectorError as exc:
        raise ApiError(400, "connector_error", str(exc)) from None
    finally:
        reader.close()
    return found
