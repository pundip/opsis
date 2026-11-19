"""Sign in with Google: OpenID Connect, authorization code flow with PKCE.

The flow's secrets (state, nonce, PKCE verifier and where to return) travel in
a short-lived encrypted cookie bound to the browser that started the flow, so
a callback started elsewhere is refused. The ID token is checked against
Google's published keys: signature, issuer, audience, expiry and nonce. Only a
Google-verified email is used, and only if the allowlists accept it.

Authlib provides the PKCE and token helpers; joserfc (Authlib's JOSE library)
verifies the ID token. The two HTTP calls use the standard library.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from authlib.common.security import generate_token
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from opsis.services import crypto
from opsis.settings import Settings

FLOW_COOKIE = "opsis_oauth"
FLOW_MAX_AGE = 600  # seconds to complete the Google screen
CACHE_SECONDS = 3600
SCOPES = "openid email profile"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


class GoogleSignInError(Exception):
    """A refused or failed Google sign-in. `code` goes back to the sign-in page."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310  (configured URL)
            data: dict[str, Any] = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise GoogleSignInError(
            "google_unavailable", f"Google could not be reached: {exc}"
        ) from exc
    return data


def _post_form(url: str, form: dict[str, str]) -> dict[str, Any]:
    body = urllib.parse.urlencode(form).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310  (configured URL)
            data: dict[str, Any] = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode(errors="replace")
        raise GoogleSignInError("google_failed", f"Google refused the sign-in: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise GoogleSignInError(
            "google_unavailable", f"Google could not be reached: {exc}"
        ) from exc
    return data


def _cached(key: str, load: Any, *, refresh: bool = False) -> Any:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and not refresh and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    value = load()
    with _lock:
        _cache[key] = (now, value)
    return value


def provider(settings: Settings) -> dict[str, Any]:
    """The provider's OpenID configuration (Google's, unless overridden for tests)."""
    config: dict[str, Any] = _cached(
        f"discovery:{settings.google_discovery_url}",
        lambda: _get_json(settings.google_discovery_url),
    )
    for key in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not isinstance(config.get(key), str):
            raise GoogleSignInError("google_unavailable", f"The OpenID configuration has no {key}.")
    return config


def _keys(uri: str, *, refresh: bool = False) -> KeySet:
    def load() -> KeySet:
        data: Any = _get_json(uri)
        return KeySet.import_key_set(data)

    keys: KeySet = _cached(f"jwks:{uri}", load, refresh=refresh)
    return keys


def redirect_uri(settings: Settings) -> str:
    return f"{settings.public_base_url}/api/auth/google/callback"


def start(settings: Settings, next_path: str) -> tuple[str, str]:
    """The Google URL to send the browser to, and the flow cookie to set."""
    config = provider(settings)
    state = generate_token(32)
    nonce = generate_token(32)
    verifier = generate_token(64)
    params = {
        "response_type": "code",
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri(settings),
        "scope": SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": create_s256_code_challenge(verifier),
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    domains = settings.google_domain_list
    if len(domains) == 1:
        params["hd"] = domains[0]  # a hint for Google's account picker; checked again below
    url = f"{config['authorization_endpoint']}?{urllib.parse.urlencode(params)}"
    cookie = crypto.seal(
        settings,
        "google-flow",
        {"state": state, "nonce": nonce, "verifier": verifier, "next": next_path},
    )
    return url, cookie


def verify_id_token(settings: Settings, token: str, nonce: str) -> dict[str, Any]:
    config = provider(settings)
    issuers = [config["issuer"], *(GOOGLE_ISSUERS if config["issuer"] in GOOGLE_ISSUERS else ())]
    try:
        try:
            decoded = jwt.decode(token, _keys(config["jwks_uri"]), algorithms=["RS256"])
        except JoseError:
            # Google rotates its keys: fetch them again once before giving up.
            decoded = jwt.decode(
                token, _keys(config["jwks_uri"], refresh=True), algorithms=["RS256"]
            )
        jwt.JWTClaimsRegistry(
            leeway=60,
            iss={"essential": True, "values": issuers},
            aud={"essential": True, "value": settings.google_client_id},
            exp={"essential": True},
            sub={"essential": True},
            nonce={"essential": True, "value": nonce},
        ).validate(decoded.claims)
    except (JoseError, ValueError) as exc:
        raise GoogleSignInError(
            "google_failed", f"The sign-in token from Google is not valid ({exc})."
        ) from exc
    claims: dict[str, Any] = decoded.claims
    return claims


def finish(
    settings: Settings, *, code: str, state: str, flow_cookies: list[str]
) -> tuple[dict[str, Any], str]:
    """Exchange the code and verify the result. Returns the ID token claims and
    where to go next."""
    flow = None
    for value in flow_cookies:
        sealed = crypto.unseal(settings, "google-flow", value, FLOW_MAX_AGE)
        if sealed and secrets.compare_digest(str(sealed.get("state", "")), state):
            flow = sealed
            break
    if flow is None:
        raise GoogleSignInError(
            "google_expired",
            "The sign-in took too long or was started in another browser. Try again.",
        )
    config = provider(settings)
    assert settings.google_client_secret is not None
    tokens = _post_form(
        config["token_endpoint"],
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(settings),
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret.get_secret_value(),
            "code_verifier": flow["verifier"],
        },
    )
    if not isinstance(tokens.get("id_token"), str):
        raise GoogleSignInError("google_failed", "Google did not return an ID token.")
    claims = verify_id_token(settings, tokens["id_token"], flow["nonce"])
    return claims, str(flow.get("next") or "/")


def allowed(settings: Settings, email: str) -> bool:
    """The allowlists: an empty pair allows any address (an invitation is then
    still needed unless auto-provisioning is on, which requires a list)."""
    domains, emails = settings.google_domain_list, settings.google_email_list
    if not domains and not emails:
        return True
    return email in emails or email.rpartition("@")[2] in domains
