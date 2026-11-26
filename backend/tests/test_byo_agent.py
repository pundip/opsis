"""Bring your own agent (v2 V1, ADR-036): people's own Anthropic and OpenAI keys,
the OpenAI provider, the workspace key only for people an admin allows (and a
revoke that takes effect at once), and the promise that no key ever appears in an
API response, a log line or an audit row.

The fakes answer over HTTP as the real APIs do (tests/test_assistant.py), through
the providers' own SDK clients.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from opsis.assistant.providers import chat_model, claude_options, scrub
from opsis.db.models import AssistantConversation, AssistantUsage, AuditLog, User, UserApiKey
from opsis.services import crypto
from tests.conftest import make_settings
from tests.test_assistant import (
    FakeModel,
    ask,
    builder_script,
    entries,
    history_of,
    new_dashboard,
    use,
)
from tests.test_auth import SERVER_SIGN_IN, admin_client, person
from tests.test_ingestion import ok
from tests.test_query_api import load
from tests.test_query_api import source as source  # noqa: F401  (fixture)

ClientFor = Callable[..., TestClient]
KEY_A = "sk-ant-api03-own-alpha-" + "A" * 40 + "Q7xz"  # a person's own Anthropic key
KEY_O = "sk-proj-own-omega-" + "B" * 48 + "Zk9w"  # a person's own OpenAI key
WORKSPACE = "sk-test-0123"  # the workspace's key, as tests.test_assistant.use sets it


def add_key(
    client: TestClient, provider: str, key: str, label: str = "Work", model: str | None = None
) -> dict[str, Any]:
    body = {"provider": provider, "label": label, "key": key, "defaultModel": model}
    made = ok(client.post("/api/account/api-keys", json=body), 201)
    assert key not in json.dumps(made) and made["last4"] == key[-4:]
    return made  # type: ignore[no-any-return]


def choose(client: TestClient, option: str, model: str | None = None) -> dict[str, Any]:
    return ok(client.put("/api/assistant/choice", json={"option": option, "model": model}))  # type: ignore[no-any-return]


def state(client: TestClient, dashboard: str) -> dict[str, Any]:
    return ok(client.get(f"/api/dashboards/{dashboard}/assistant"))  # type: ignore[no-any-return]


def said(fake: FakeModel, index: int = -1) -> str:
    """The key a model call carried: x-api-key for Anthropic, a bearer token for OpenAI."""
    headers = fake.headers[index]
    return headers.get("x-api-key") or headers.get("authorization", "").removeprefix("Bearer ")


# ---- OpenAI ------------------------------------------------------------------------------


def test_openai_builds_a_dashboard_with_a_persons_own_key(
    client_for: ClientFor,
    source: dict[str, Any],
    db: Session,  # noqa: F811
) -> None:
    client = client_for()
    load(client, source)
    dashboard = new_dashboard(client)
    fake = FakeModel("openai", builder_script())
    use(client, fake)
    key = add_key(client, "openai", KEY_O, model="gpt-5.5")
    choose(client, f"key:{key['id']}")

    shown = entries(ask(client, dashboard, "Build a dashboard of gifts"))
    # The same build as with Claude: the commentary is a progress note.
    assert [kind for kind, _ in shown] == ["update", "text", "step", "step", "step", "text", "done"]
    assert ok(client.get(f"/api/dashboards/{dashboard}"))["spec"]["items"][0]["id"] == "kpi_total"
    done = shown[-1][1]
    assert (done["provider"], done["source"], done["keyLabel"], done["model"]) == (
        "openai",
        "own",
        "Work",
        "gpt-5.5",
    )
    assert (done["inputTokens"], done["cachedTokens"], done["outputTokens"]) == (4000, 1600, 200)

    # What OpenAI was sent: the Responses API, nothing stored there, the key in the header.
    assert set(fake.paths) == {"/v1/responses"} and said(fake) == KEY_O
    first = fake.requests[0]
    assert first["model"] == "gpt-5.5" and first["stream"] is True and first["store"] is False
    assert first["include"] == ["reasoning.encrypted_content"]
    assert "dashboard editor" in first["instructions"]
    tool = first["tools"][0]
    assert tool["type"] == "function" and tool["strict"] is False and "parameters" in tool
    names = [t["name"] for t in first["tools"]]
    assert names == sorted(names) and "patch_dashboard" in names
    assert first["input"][0]["role"] == "user" and first["input"][0]["content"].startswith(
        "<context>"
    )
    # The reply's items go back as they came, then each call's output by its call id.
    second = history_of(fake.requests[1])
    assert [item.get("type", "message") for item in second[1:]] == [
        "message",
        "message",
        "function_call",
        "function_call_output",
    ]
    assert second[1]["phase"] == "commentary"
    assert (
        second[4]["call_id"] == second[3]["call_id"]
        and json.loads(second[4]["output"])["dataset"] == "gifts"
    )
    for before, after in zip(fake.requests, fake.requests[1:], strict=False):
        assert after["input"][: len(before["input"])] == before["input"]  # append-only

    # Usage is kept per key, for the person's Account page, and audited without the key.
    row = db.scalar(select(AssistantUsage))
    assert row is not None and (row.source, row.provider, str(row.key_id), row.key_label) == (
        "own",
        "openai",
        key["id"],
        "Work",
    )
    usage = ok(client.get("/api/account/api-keys"))["usage"]
    assert (
        usage[0]["keyLabel"] == "Work"
        and usage[0]["requests"] == 1
        and usage[0]["outputTokens"] == 200
    )
    audit = db.scalar(select(AuditLog.detail).where(AuditLog.action == "assistant.request"))
    assert audit is not None and audit["keyId"] == key["id"] and audit["source"] == "own"
    assert ok(client.get("/api/account/api-keys"))["keys"][0]["lastUsedAt"] is not None


def test_openai_reasoning_goes_back_unchanged_and_refusals_are_not_kept(
    client_for: ClientFor,
    source: dict[str, Any],  # noqa: F811
) -> None:
    client = client_for()
    load(client, source)
    dashboard = new_dashboard(client)
    fake = FakeModel(
        "openai",
        [
            lambda body: ([("reasoning",), ("call", "list_datasets", {})], "completed"),
            lambda body: ([("text", "Two datasets.")], "completed"),
            lambda body: ([("refusal", "I can't help with that.")], "completed"),
            lambda body: ([("call", "list_datasets", {})], "max_output_tokens"),
            lambda body: ([("text", "Smaller steps.")], "completed"),
        ],
    )
    use(client, fake)
    choose(client, f"key:{add_key(client, 'openai', KEY_O, model='gpt-5.5')['id']}")
    ask(client, dashboard, "What is there?")
    sent = history_of(fake.requests[1])
    reasoning = next(item for item in sent if item.get("type") == "reasoning")
    assert reasoning["encrypted_content"] == "gAAAAB-encrypted-reasoning"

    shown = entries(ask(client, dashboard, "Something odd"))
    notice = next(data for kind, data in shown if kind == "notice")
    assert "declined" in notice["text"] and "can't help" in notice["text"]
    assert shown[-1][1]["outcome"] == "refused"
    # A cut-off call is answered, not run, and the declined reply is not in the history.
    shown = entries(ask(client, dashboard, "Again"))
    assert not any(kind == "step" for kind, _ in shown)
    later = history_of(fake.requests[-1])
    assert not any(
        item.get("type") == "message" and "can't help" in json.dumps(item) for item in later
    )
    assert any(
        item.get("type") == "function_call_output" and "Not run" in item["output"] for item in later
    )


def test_a_model_without_reasoning_items_is_asked_again_without_them(
    client_for: ClientFor,
    source: dict[str, Any],  # noqa: F811
) -> None:
    import httpx2

    client = client_for()
    load(client, source)
    dashboard = new_dashboard(client)
    fake = FakeModel("openai", [lambda body: ([("text", "Hello.")], "completed")])
    handler = fake.handler

    def picky(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST" and "include" in json.loads(request.content):
            fake.requests.append(json.loads(request.content))
            message = "Encrypted content is not supported with this model."
            return httpx2.Response(
                400,
                json={
                    "error": {
                        "message": message,
                        "type": "invalid_request_error",
                        "param": "include",
                        "code": None,
                    }
                },
            )
        return handler(request)

    use(client, fake)
    client.app.state.assistant_transport = httpx2.MockTransport(picky)  # type: ignore[attr-defined]
    choose(client, f"key:{add_key(client, 'openai', KEY_O, model='gpt-4.1')['id']}")
    shown = entries(ask(client, dashboard, "Hi"))
    assert shown[0] == ("text", {"text": "Hello."})
    assert "include" in fake.requests[0] and "include" not in fake.requests[1]


# ---- Claude with a person's own key --------------------------------------------------------


def test_claude_with_a_persons_own_key_gets_the_options_its_model_takes(
    client_for: ClientFor,
    source: dict[str, Any],
    db: Session,  # noqa: F811
) -> None:
    client = client_for()
    load(client, source)
    dashboard = new_dashboard(client)
    fake = FakeModel("anthropic", [lambda body: ([("text", "Hi.")], "end_turn")] * 3)
    use(client, fake)  # the workspace key exists too
    key = add_key(client, "anthropic", KEY_A, model="claude-haiku-5-5")
    choose(client, f"key:{key['id']}")
    ask(client, dashboard, "Hello")
    first = fake.requests[0]
    assert said(fake) == KEY_A and first["model"] == "claude-haiku-5-5"
    # Adaptive thinking and effort, but neither progress updates nor fallbacks.
    assert first["thinking"] == {"type": "adaptive"} and first["output_config"] == {
        "effort": "medium"
    }
    assert "fallbacks" not in first and "anthropic-beta" not in fake.headers[-1]

    choose(client, f"key:{key['id']}", "claude-haiku-4-5")  # an older model: plain tool use
    ask(client, dashboard, "Hello")
    older = fake.requests[1]
    assert older["model"] == "claude-haiku-4-5"
    assert not {"thinking", "output_config", "fallbacks", "cache_control"} & set(older)

    choose(client, "workspace")
    ask(client, dashboard, "Hello")
    assert said(fake) == WORKSPACE and fake.requests[2]["model"] == "claude-opus-5-5"
    assert fake.requests[2]["thinking"] == {"type": "adaptive", "display": "updates"}
    # A conversation stays with one key and model: three here.
    assert len(db.scalars(select(AssistantConversation)).all()) == 3


def test_claude_options_by_model() -> None:
    assert claude_options("claude-opus-5-5") == (True, True, True)
    assert claude_options("claude-haiku-5-5") == (True, False, False)
    assert claude_options("claude-opus-5") == (True, False, True)
    assert claude_options("claude-haiku-4-5-20251001") == (False, False, False)  # dated snapshot
    assert claude_options("claude-sonnet-4-20250514") == (False, False, False)
    assert claude_options("claude-opus-6") == (True, True, True)  # newer than the table
    assert [
        m
        for m in [
            "gpt-5.5",
            "o4-mini",
            "text-embedding-3-large",
            "whisper-1",
            "gpt-image-1",
            "dall-e-3",
        ]
        if chat_model(m)
    ] == ["gpt-5.5", "o4-mini"]
    assert scrub(f"bad key {KEY_O} given", KEY_O) == f"bad key ****{KEY_O[-4:]} given"
    assert scrub("nothing here", KEY_O) == "nothing here"


# ---- the Account page's keys -----------------------------------------------------------


def test_keys_are_added_tested_renamed_and_deleted_but_never_shown(
    client_for: ClientFor,
    db: Session,  # noqa: F811
) -> None:
    settings = make_settings()
    client = client_for()
    fake = FakeModel("openai", [])
    use(client, fake)
    made = add_key(client, "anthropic", KEY_A)
    listed = client.get("/api/account/api-keys")
    assert KEY_A not in listed.text and listed.json()["keys"][0]["label"] == "Work"
    stored = db.scalar(select(UserApiKey))
    assert stored is not None and KEY_A not in stored.secret
    assert crypto.decrypt(settings, stored.secret) == {"key": KEY_A}

    # What is refused, and why.
    assert (
        client.post(
            "/api/account/api-keys",
            json={"provider": "anthropic", "label": "Home", "key": "sk-ant has spaces in it"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/account/api-keys", json={"provider": "google", "label": "x", "key": KEY_A}
        ).status_code
        == 422
    )
    duplicate = client.post(
        "/api/account/api-keys", json={"provider": "anthropic", "label": " Work ", "key": KEY_A}
    )
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "duplicate_label"

    # Test: the provider's models endpoint, free. The default model is checked too.
    tested = ok(client.post(f"/api/account/api-keys/{made['id']}/test"))
    assert tested["ok"] is True and tested["message"] == "The key works: it may use 2 models."
    assert [m["id"] for m in tested["models"]] == ["claude-opus-5-5", "claude-haiku-5-5"]
    assert said(fake) == KEY_A and fake.headers[-1]["anthropic-version"]
    ok(client.patch(f"/api/account/api-keys/{made['id']}", json={"defaultModel": "claude-unknown"}))
    assert (
        "not one of them" in ok(client.post(f"/api/account/api-keys/{made['id']}/test"))["message"]
    )
    bad = add_key(client, "anthropic", "sk-ant-api03-bad-" + "C" * 40, label="Old")
    failed = ok(client.post(f"/api/account/api-keys/{bad['id']}/test"))
    assert (
        failed["ok"] is False
        and failed["message"] == "Anthropic did not accept the key: invalid x-api-key"
    )
    assert failed["key"]["lastTestOk"] is False

    # OpenAI lists every model a key may use; only chat models are offered.
    openai = add_key(client, "openai", KEY_O, label="Lab")
    assert [
        m["id"] for m in ok(client.get(f"/api/account/api-keys/{openai['id']}/models"))["models"]
    ] == ["gpt-5.5", "gpt-5.5-mini"]
    wrong = add_key(client, "openai", "sk-proj-bad-" + "D" * 40 + "1234", label="Wrong")
    failed = ok(client.post(f"/api/account/api-keys/{wrong['id']}/test"))
    assert failed["ok"] is False and "OpenAI did not accept the key" in failed["message"]
    refused = client.get(f"/api/account/api-keys/{wrong['id']}/models")
    assert refused.status_code == 502 and "did not accept" in refused.json()["error"]["message"]

    # Rename, clear the default model, delete.
    changed = ok(
        client.patch(
            f"/api/account/api-keys/{made['id']}", json={"label": "Personal", "defaultModel": None}
        )
    )
    assert (changed["label"], changed["defaultModel"]) == ("Personal", None)
    assert client.delete(f"/api/account/api-keys/{made['id']}").status_code == 204
    assert client.delete(f"/api/account/api-keys/{made['id']}").status_code == 404
    actions = db.scalars(
        select(AuditLog.action).where(AuditLog.action.startswith("assistant.key"))
    ).all()
    assert {"assistant.key_added", "assistant.key_changed", "assistant.key_deleted"} <= set(actions)


# ---- the workspace key, grants, and personal keys switched off --------------------------


def test_the_workspace_key_needs_a_grant_and_a_revoke_takes_effect_at_once(
    client_for: ClientFor,
    source: dict[str, Any],
    db: Session,  # noqa: F811
) -> None:
    admin = admin_client(client_for, **SERVER_SIGN_IN)
    load(admin, source)
    editor, editor_id = person(client_for, admin, "ed@example.test", "editor", **SERVER_SIGN_IN)
    dashboard = new_dashboard(admin)
    ok(
        admin.put(
            f"/api/dashboards/{dashboard}/access",
            json={"shares": [{"grantee": "everyone", "role": "editor"}]},
        )
    )
    fake = FakeModel("anthropic", [])
    for client in (admin, editor):
        use(client, fake, ollama_enabled=False)

    # No keys and no grant: the panel says how to get one, and nothing runs. That goes
    # for admins too: the workspace key is off for everyone until someone is allowed.
    for client in (editor, admin):
        nothing = state(client, dashboard)
        assert nothing["options"] == [] and nothing["choice"] is None
        assert "Account page" in nothing["unavailable"] and "admin" in nothing["unavailable"]
    refused = editor.post(f"/api/dashboards/{dashboard}/assistant/messages", json={"text": "Hi"})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "no_assistant_option"

    # Granted: the workspace key is offered and works.
    grant = {"grantee": f"user:{editor_id}", "allowed": True}
    ok(admin.put("/api/assistant-access/grants", json=grant))
    assert [o["id"] for o in state(editor, dashboard)["options"]] == ["workspace"]
    fake.turns = [lambda body: ([("text", "Hello.")], "end_turn")]
    assert entries(ask(editor, dashboard, "Hi"))[0] == ("text", {"text": "Hello."})
    assert said(fake) == WORKSPACE

    # Revoked while a request runs: it stops before its next model call.
    def revoke_then_call(body: dict[str, Any]) -> Any:
        ok(admin.put("/api/assistant-access/grants", json={**grant, "allowed": None}))
        return [("call", "list_datasets", {})], "tool_use"

    fake.turns = [revoke_then_call, lambda body: ([("text", "Never sent.")], "end_turn")]
    calls = len(fake.requests)
    shown = entries(ask(editor, dashboard, "Look around"))
    assert len(fake.requests) == calls + 1 and len(fake.turns) == 1
    assert (
        "notice",
        {
            "level": "error",
            "text": "You may no longer use the workspace's key, so the assistant stopped.",
        },
    ) in shown
    assert shown[-1][1]["outcome"] == "error"
    assert state(editor, dashboard)["options"] == []
    assert (
        editor.post(
            f"/api/dashboards/{dashboard}/assistant/messages", json={"text": "Hi"}
        ).status_code
        == 409
    )

    # Groups: a yes through a group, a no on the person wins, a no on a group wins.
    team = ok(admin.post("/api/groups", json={"name": "Team", "memberIds": [editor_id]}), 201)
    others = ok(admin.post("/api/groups", json={"name": "Others", "memberIds": [editor_id]}), 201)
    ok(
        admin.put(
            "/api/assistant-access/grants", json={"grantee": f"group:{team['id']}", "allowed": True}
        )
    )
    assert [o["id"] for o in state(editor, dashboard)["options"]] == ["workspace"]
    ok(admin.put("/api/assistant-access/grants", json={**grant, "allowed": False}))
    assert state(editor, dashboard)["options"] == []
    ok(admin.put("/api/assistant-access/grants", json={**grant, "allowed": None}))
    ok(
        admin.put(
            "/api/assistant-access/grants",
            json={"grantee": f"group:{others['id']}", "allowed": False},
        )
    )
    assert state(editor, dashboard)["options"] == []
    access = ok(admin.get("/api/assistant-access"))
    row = next(p for p in access["people"] if p["id"] == editor_id)
    assert row["grant"] is None and row["workspaceKey"] is False
    assert {g["name"]: g["grant"] for g in access["groups"]} == {"Others": False, "Team": True}
    # Deleting a group takes its setting with it.
    assert admin.delete(f"/api/groups/{others['id']}").status_code == 204
    assert [o["id"] for o in state(editor, dashboard)["options"]] == ["workspace"]
    assert (
        admin.put(
            "/api/assistant-access/grants", json={"grantee": "everyone", "allowed": True}
        ).status_code
        == 404
    )
    assert editor.get("/api/assistant-access").status_code == 403
    assert {"grant.changed"} == set(
        db.scalars(select(AuditLog.action).where(AuditLog.action == "grant.changed")).all()
    )

    # Personal keys switched off: kept, but not offered, and no new ones.
    add_key(editor, "anthropic", KEY_A)
    assert [o["source"] for o in state(editor, dashboard)["options"]] == ["own", "workspace"]
    ok(admin.put("/api/assistant-access/settings", json={"byoKeys": False}))
    assert [o["source"] for o in state(editor, dashboard)["options"]] == ["workspace"]
    off = editor.post(
        "/api/account/api-keys", json={"provider": "openai", "label": "x", "key": KEY_O}
    )
    assert off.status_code == 409 and off.json()["error"]["code"] == "byo_keys_off"
    assert ok(editor.get("/api/account/api-keys"))["enabled"] is False
    ok(admin.put("/api/assistant-access/settings", json={"byoKeys": True}))
    # Admins see who has keys of which provider, never the keys.
    listing = admin.get("/api/assistant-access")
    assert KEY_A not in listing.text
    assert next(p for p in listing.json()["people"] if p["id"] == editor_id)["keys"] == {
        "anthropic": 1
    }
    assert "settings.changed" in db.scalars(select(AuditLog.action)).all()


def test_a_choice_that_is_gone_is_never_swapped_for_a_paid_key(
    client_for: ClientFor,
    source: dict[str, Any],  # noqa: F811
) -> None:
    admin = admin_client(client_for, **SERVER_SIGN_IN)
    load(admin, source)
    editor, editor_id = person(client_for, admin, "ed@example.test", "editor", **SERVER_SIGN_IN)
    dashboard = new_dashboard(admin)
    everyone = {"shares": [{"grantee": "everyone", "role": "editor"}]}
    ok(admin.put(f"/api/dashboards/{dashboard}/access", json=everyone))

    def workspace_key(allowed: bool | None) -> None:
        grant = {"grantee": f"user:{editor_id}", "allowed": allowed}
        ok(admin.put("/api/assistant-access/grants", json=grant))

    fake = FakeModel("anthropic", [lambda body: ([("text", "Mine.")], "end_turn")])
    use(editor, fake, ollama_enabled=False)
    own = add_key(editor, "anthropic", KEY_A)
    workspace_key(True)
    choose(editor, "workspace")
    workspace_key(None)

    # The person's own key is still there, but it is not used behind their back.
    now = state(editor, dashboard)
    assert [o["id"] for o in now["options"]] == [f"key:{own['id']}"]
    assert now["choice"] is None and now["provider"] is None
    gone = "You may no longer use the workspace's key. Choose what the assistant should use."
    assert now["notice"] == gone
    refused = editor.post(f"/api/dashboards/{dashboard}/assistant/messages", json={"text": "Hi"})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "choice_unavailable"
    assert fake.requests == []
    # Once they choose, it runs on their key.
    choose(editor, f"key:{own['id']}")
    assert entries(ask(editor, dashboard, "Hi"))[0] == ("text", {"text": "Mine."})
    assert said(fake) == KEY_A and state(editor, dashboard)["notice"] is None
    # Deleting the key they chose themselves clears the choice: no notice, the default.
    workspace_key(True)
    assert editor.delete(f"/api/account/api-keys/{own['id']}").status_code == 204
    after = state(editor, dashboard)
    assert after["notice"] is None
    assert after["choice"] == {"option": "workspace", "model": "claude-opus-5-5"}


def test_only_people_who_build_have_keys(client_for: ClientFor, db: Session) -> None:  # noqa: F811
    admin = admin_client(client_for, **SERVER_SIGN_IN)
    viewer, _ = person(client_for, admin, "vi@example.test", "viewer", **SERVER_SIGN_IN)
    assert viewer.get("/api/account/api-keys").status_code == 403
    # Someone else's key is not there for you.
    editor, _ = person(client_for, admin, "ed@example.test", "editor", **SERVER_SIGN_IN)
    theirs = add_key(editor, "anthropic", KEY_A)
    assert admin.delete(f"/api/account/api-keys/{theirs['id']}").status_code == 404
    assert admin.post(f"/api/account/api-keys/{theirs['id']}/test").status_code == 404
    # Access tokens are for scripts and MCP, not for keys.
    token = ok(admin.post("/api/tokens", json={"name": "t", "scopes": ["read", "edit"]}), 201)[
        "token"
    ]
    scripted = client_for(csrf=False, **SERVER_SIGN_IN)
    denied = scripted.get("/api/account/api-keys", headers={"Authorization": f"Bearer {token}"})
    assert denied.status_code == 403
    # Without sign-in there is nobody to grant, so the admin routes do not exist.
    assert client_for().get("/api/assistant-access").status_code == 404
    assert client_for(feature_assistant=False).get("/api/account/api-keys").status_code == 404


def test_deactivating_someone_deletes_their_keys(client_for: ClientFor, db: Session) -> None:  # noqa: F811
    admin = admin_client(client_for, **SERVER_SIGN_IN)
    editor, editor_id = person(client_for, admin, "ed@example.test", "editor", **SERVER_SIGN_IN)
    add_key(editor, "anthropic", KEY_A)
    add_key(editor, "openai", KEY_O, label="Lab")
    ok(admin.patch(f"/api/users/{editor_id}", json={"isActive": False}))
    assert db.scalars(select(UserApiKey)).all() == []
    detail = db.scalar(
        select(AuditLog.detail)
        .where(AuditLog.action == "user.updated")
        .order_by(AuditLog.id.desc())
    )
    assert detail is not None and detail["api_keys_deleted"] == 2
    # And deleting a person's row takes their keys with it.
    other, other_id = person(client_for, admin, "ot@example.test", "editor", **SERVER_SIGN_IN)
    add_key(other, "anthropic", KEY_A)
    db.execute(text("DELETE FROM users WHERE id = :id"), {"id": other_id})
    db.commit()
    assert db.scalars(select(UserApiKey)).all() == []
    assert db.get(User, uuid.UUID(other_id)) is None


# ---- no key anywhere -----------------------------------------------------------------------


def test_no_key_ever_appears_in_a_response_a_log_or_an_audit_row(
    client_for: ClientFor,
    source: dict[str, Any],  # noqa: F811
    db: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)  # every logger, the SDKs' and httpx2's included
    marker = "PROMPT-MARKER-7Q"  # the person's words are not logged either
    admin = admin_client(client_for, **SERVER_SIGN_IN)
    load(admin, source)
    dashboard = new_dashboard(admin)
    fake = FakeModel("anthropic", [])
    use(admin, fake)
    answers: list[str] = []

    def call(method: str, url: str, **kwargs: Any) -> Any:
        response = admin.request(method, url, **kwargs)
        assert response.status_code < 500, response.text
        answers.append(response.text)
        return (
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else None
        )

    anthropic = call(
        "POST", "/api/account/api-keys", json={"provider": "anthropic", "label": "A", "key": KEY_A}
    )
    openai = call(
        "POST",
        "/api/account/api-keys",
        json={"provider": "openai", "label": "O", "key": KEY_O, "defaultModel": "gpt-5.5"},
    )
    for key in (anthropic, openai):
        call("POST", f"/api/account/api-keys/{key['id']}/test")
        call("GET", f"/api/account/api-keys/{key['id']}/models")
        call("PATCH", f"/api/account/api-keys/{key['id']}", json={"label": key["label"] + "2"})
    for key, kind in ((anthropic, "/v1/messages"), (openai, "/responses")):
        call("PUT", "/api/assistant/choice", json={"option": f"key:{key['id']}"})
        fake.turns = [
            lambda body: ([("call", "list_datasets", {})], "tool_use"),
            lambda body: ([("text", "Done.")], "end_turn"),
        ]
        streamed = admin.post(
            f"/api/dashboards/{dashboard}/assistant/messages",
            json={"text": f"Look around {marker}"},
        )
        assert streamed.status_code == 200 and "Done." in streamed.text
        answers.append(streamed.text)
        assert fake.paths[-1].endswith(kind)
        call("GET", f"/api/dashboards/{dashboard}/assistant")
    call("GET", "/api/account/api-keys")
    call("GET", "/api/assistant-access")
    call("GET", "/api/audit?limit=500")
    call("GET", "/api/users")
    call("GET", "/api/me")
    call("GET", "/api/config")
    # The keys were used, each for its own requests ...
    assert {said(fake, i) for i in range(len(fake.headers))} >= {KEY_A, KEY_O}

    # ... and appear nowhere else.
    tables = [
        "audit_log",
        "assistant_conversations",
        "assistant_messages",
        "assistant_entries",
        "assistant_usage",
        "jobs",
        "users",
        "workspace_settings",
        "capability_grants",
        "assistant_choices",
    ]
    stored = {
        table: json.dumps(
            [dict(row._mapping) for row in db.execute(text(f"SELECT * FROM {table}"))], default=str
        )
        for table in tables
    }
    secrets = json.dumps([row.secret for row in db.scalars(select(UserApiKey))])
    logs = caplog.text + "".join(str(record.args) for record in caplog.records)
    for key in (KEY_A, KEY_O):
        assert all(key not in answer for answer in answers), "a key in an API response"
        assert key not in logs, "a key in a log line"
        for table, rows in stored.items():
            assert key not in rows, f"a key in {table}"
        assert key not in secrets, "a key stored unencrypted"
    assert marker not in logs, "the person's words in a log line"
    assert marker not in stored["audit_log"], "the person's words in the audit log"
    assert (
        "assistant.request" in stored["audit_log"] and "assistant.key_added" in stored["audit_log"]
    )
