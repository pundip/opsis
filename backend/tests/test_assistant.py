"""The in-app assistant: both providers against scripted fake models, the tool
loop and its budgets, what is stored and streamed, and who may use it.

The fakes answer over HTTP exactly as the real APIs stream (Anthropic's
Messages events, and OpenAI-style chunks for Ollama), through the providers'
own clients, so the SDK's parsing is part of what is tested.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from opsis.assistant import loop, prompt
from opsis.assistant.providers import (
    AnthropicProvider,
    OllamaProvider,
    _echo,
)
from opsis.db.models import AuditLog, DashboardVersion
from opsis.settings import Settings
from tests.conftest import make_settings
from tests.test_auth import SERVER_SIGN_IN, admin_client, person
from tests.test_ingestion import ok
from tests.test_query_api import load
from tests.test_query_api import source as source  # noqa: F401  (fixture)

ClientFor = Callable[..., TestClient]
# A turn of a scripted model: given the request body, what it writes and why it stops.
Turn = Callable[[dict[str, Any]], tuple[list[tuple[Any, ...]], str]]

# ---- fake models ----------------------------------------------------------------------

_ids = iter(range(1, 1_000_000))


def claude_sse(
    parts: list[tuple[Any, ...]],
    stop: str = "end_turn",
    *,
    model: str = "claude-opus-5-5",
    iterations: list[dict[str, Any]] | None = None,
    stop_details: dict[str, Any] | None = None,
) -> bytes:
    """A streamed Messages API response, event by event."""
    out: list[str] = []

    def event(name: str, data: dict[str, Any]) -> None:
        out.append(f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n")

    usage = {"input_tokens": 100, "output_tokens": 1, "cache_read_input_tokens": 400}
    event(
        "message_start",
        {
            "message": {"id": "msg_test", "type": "message", "role": "assistant", "model": model,
                        "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}
        },
    )  # fmt: skip
    for index, part in enumerate(parts):
        kind = part[0]
        if kind == "text":
            event(
                "content_block_start",
                {"index": index, "content_block": {"type": "text", "text": ""}},
            )
            event(
                "content_block_delta",
                {"index": index, "delta": {"type": "text_delta", "text": part[1]}},
            )
        elif kind == "update":
            block = {"type": "thinking", "thinking": "", "signature": ""}
            event("content_block_start", {"index": index, "content_block": block})
            event(
                "content_block_delta",
                {"index": index, "delta": {"type": "thinking_delta", "thinking": part[1]}},
            )
            event(
                "content_block_delta",
                {"index": index, "delta": {"type": "signature_delta", "signature": f"sig{index}"}},
            )
        elif kind == "call":
            block = {"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": part[1], "input": {}}
            event("content_block_start", {"index": index, "content_block": block})
            event(
                "content_block_delta",
                {
                    "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": json.dumps(part[2])},
                },
            )
        elif kind == "fallback":
            block = {"type": "fallback", "from": {"model": model}, "to": {"model": "claude-opus-5"}}
            event("content_block_start", {"index": index, "content_block": block})
        event("content_block_stop", {"index": index})
    delta: dict[str, Any] = {"stop_reason": stop, "stop_sequence": None}
    if stop_details:
        delta["stop_details"] = stop_details
    tail: dict[str, Any] = {"output_tokens": 50}
    if iterations:
        tail["iterations"] = iterations
    event("message_delta", {"delta": delta, "usage": tail})
    event("message_stop", {})
    return "".join(out).encode()


def ollama_lines(
    parts: list[tuple[Any, ...]], stop: str = "stop", *, prompt_tokens: int = 900
) -> bytes:
    """A streamed /api/chat response, as Ollama sends it: one JSON object per line."""
    lines: list[dict[str, Any]] = []
    for index, part in enumerate(parts):
        if part[0] == "text":
            lines.append({"message": {"role": "assistant", "content": part[1]}, "done": False})
        elif part[0] == "call":
            # Arguments are an object; a string here stands for something unreadable.
            call = {"id": f"call_{next(_ids)}",
                    "function": {"index": index, "name": part[1], "arguments": part[2]}}  # fmt: skip
            message = {"role": "assistant", "content": "", "tool_calls": [call]}
            lines.append({"message": message, "done": False})
    lines.append(
        {
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": "length" if stop == "length" else "stop",
            "prompt_eval_count": prompt_tokens,
            "prompt_eval_cached_count": 600,
            "eval_count": 40,
        }
    )
    return "".join(json.dumps({"model": "gemma4:12b", **line}) + "\n" for line in lines).encode()


class FakeModel:
    """Answers each model call with the next turn of a script, and keeps the requests."""

    def __init__(self, kind: str, turns: list[Turn], *, prompt_tokens: int = 900) -> None:
        self.kind = kind
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.prompt_tokens = prompt_tokens
        self.fail: tuple[int, dict[str, Any]] | None = None

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.paths.append(request.url.path)
        self.requests.append(body)
        self.headers.append(dict(request.headers))
        if self.fail is not None:
            return httpx2.Response(self.fail[0], json=self.fail[1])
        assert self.turns, "the model was called more often than scripted"
        parts, stop = self.turns.pop(0)(body)
        if self.kind == "anthropic":
            extra = parts.pop() if parts and isinstance(parts[-1], dict) else {}
            content = claude_sse(parts, stop, **extra)
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, content=content
            )
        content = ollama_lines(parts, stop, prompt_tokens=self.prompt_tokens)
        return httpx2.Response(
            200, headers={"content-type": "application/x-ndjson"}, content=content
        )

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handler)


def use(client: TestClient, fake: FakeModel, **overrides: Any) -> Settings:
    """Point the client's app at a fake model, with settings changed as given."""
    state = client.app.state  # type: ignore[attr-defined]
    if not hasattr(state, "original_settings"):
        state.original_settings = state.settings
    if fake.kind == "anthropic":
        key = SecretStr("sk-test-0123")
        overrides = {"assistant_provider": "anthropic", "anthropic_api_key": key, **overrides}
    settings = state.original_settings.model_copy(update=overrides)
    provider_class = AnthropicProvider if fake.kind == "anthropic" else OllamaProvider
    client.app.state.assistant_provider = provider_class(settings, fake.transport())  # type: ignore[attr-defined]
    client.app.state.settings = settings  # type: ignore[attr-defined]
    return settings


def ask(
    client: TestClient, dashboard_id: str, text: str, selected: str | None = None
) -> list[tuple[str, Any]]:
    """Send a request and read the whole event stream."""
    response = client.post(
        f"/api/dashboards/{dashboard_id}/assistant/messages",
        json={"text": text, "selectedItemId": selected},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = []
    for chunk in response.text.split("\n\n"):
        lines = [line for line in chunk.split("\n") if line and not line.startswith(":")]
        if not lines:
            continue
        name = lines[0].removeprefix("event: ")
        events.append((name, json.loads(lines[1].removeprefix("data: "))))
    return events


def entries(events: list[tuple[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    return [(e["kind"], e["data"]) for name, e in events if name == "entry"]


def last_tool_result(body: dict[str, Any]) -> Any:
    """The newest tool result in a request, parsed, in either provider's format."""
    for message in reversed(body["messages"]):
        if message["role"] == "tool":
            return json.loads(message["content"])
        if message["role"] == "user" and isinstance(message["content"], list):
            return json.loads(message["content"][-1]["content"])
    raise AssertionError("no tool result")


def context_of(body: dict[str, Any]) -> str:
    first = next(m for m in body["messages"] if m["role"] == "user")
    return str(first["content"])


def dashboard_id_in(body: dict[str, Any]) -> str:
    match = re.search(r"id ([0-9a-f-]{36}), version (\d+)", context_of(body))
    assert match
    return match.group(1)


def builder_script() -> list[Turn]:
    """Build a dashboard: describe the data, look at the visuals, patch, summarise."""

    def describe(body: dict[str, Any]) -> Any:
        assert "Datasets you may use:" in context_of(body)
        return [("update", "Looking at the gifts data."), ("text", "I'll build it."),
                ("call", "describe_dataset", {"dataset": "gifts"})], "tool_use"  # fmt: skip

    def visuals(body: dict[str, Any]) -> Any:
        assert "gifts.total_amount" in json.dumps(last_tool_result(body))
        return [("call", "list_visuals", {"dataset": "gifts"})], "tool_use"

    def patch(body: dict[str, Any]) -> Any:
        found = {v["visual"]: v["exampleItem"] for v in last_tool_result(body)["visuals"]}
        kpi = {**found["kpi"], "id": "kpi_total", "title": "Total given"}
        ops = [{"op": "replace", "path": "/items", "value": [kpi]}]
        args = {"id": dashboard_id_in(body), "base_version": 1, "ops": ops, "message": "Add a KPI"}
        return [("call", "patch_dashboard", args)], "tool_use"

    def summary(body: dict[str, Any]) -> Any:
        assert last_tool_result(body)["saved"] is True
        return [("text", "Added a KPI card for the total given.")], "end_turn"

    return [describe, visuals, patch, summary]


def new_dashboard(client: TestClient, title: str = "Assistant test") -> str:
    return str(ok(client.post("/api/dashboards", json={"title": title}), 201)["id"])


# ---- Claude ---------------------------------------------------------------------------


def test_claude_builds_a_dashboard_step_by_step(
    client_for: ClientFor,
    source: dict[str, Any],
    db: Session,  # noqa: F811
) -> None:
    client = client_for()
    load(client, source)
    dashboard = new_dashboard(client)
    fake = FakeModel("anthropic", builder_script())
    use(client, fake)

    events = ask(client, dashboard, "Build a dashboard of gifts")
    names = [name for name, _ in events]
    assert names[0] == "start" and names[-1] == "done"
    assert events[0][1]["entry"]["kind"] == "request"
    assert {"delta", "step_started", "thinking", "call"} <= set(names)
    shown = entries(events)
    kinds = [kind for kind, _ in shown]
    assert kinds == ["update", "text", "step", "step", "step", "text", "done"]
    steps = [data for kind, data in shown if kind == "step"]
    assert [s["tool"] for s in steps] == ["describe_dataset", "list_visuals", "patch_dashboard"]
    assert all(s["ok"] for s in steps)
    assert steps[2]["writes"] and steps[2]["version"] == {"before": 1, "after": 2}
    done = shown[-1][1]
    assert done["outcome"] == "done" and done["steps"] == 3 and done["modelCalls"] == 4
    assert (done["versionBefore"], done["versionAfter"]) == (1, 2)
    assert (
        done["outputTokens"] == 200 and done["inputTokens"] == 2000 and done["cachedTokens"] == 1600
    )

    # The change is a new version by the assistant, with its note.
    saved = ok(client.get(f"/api/dashboards/{dashboard}"))
    assert [i["id"] for i in saved["spec"]["items"]] == ["kpi_total"]
    version = db.scalar(select(DashboardVersion).where(DashboardVersion.version == 2))
    assert version is not None and (version.author_type, version.message) == ("agent", "Add a KPI")

    # What Claude was sent: current features, cacheable, the same prefix every time.
    first = fake.requests[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["thinking"] == {"type": "adaptive", "display": "updates"}
    assert first["output_config"] == {"effort": "medium"}
    assert first["fallbacks"] == "default"
    assert "temperature" not in first and "tool_choice" not in first
    assert first["cache_control"] == {"type": "ephemeral"}
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert all(t["eager_input_streaming"] for t in first["tools"])
    tool_names = [t["name"] for t in first["tools"]]
    assert tool_names == sorted(tool_names) and "patch_dashboard" in tool_names
    assert not {"create_dashboard", "publish_dashboard", "share_dashboard"} & set(tool_names)
    betas = fake.headers[0]["anthropic-beta"]
    assert (
        "thinking-display-updates-2026-08-18" in betas
        and "server-side-fallback-2026-07-01" in betas
    )
    context = context_of(first)
    assert context.startswith("<context>") and dashboard in context and "gifts" in context
    assert context.endswith("Build a dashboard of gifts")
    for before, after in zip(fake.requests, fake.requests[1:], strict=False):
        assert after["system"] == before["system"] and after["tools"] == before["tools"]
        assert after["messages"][: len(before["messages"])] == before["messages"]  # append-only
    # Claude's own blocks go back exactly as they came, thinking signature included.
    reply = fake.requests[1]["messages"][1]
    assert reply["role"] == "assistant"
    assert reply["content"][0] == {
        "type": "thinking",
        "thinking": "Looking at the gifts data.",
        "signature": "sig0",
    }
    assert (
        reply["content"][2]["type"] == "tool_use"
        and reply["content"][2]["name"] == "describe_dataset"
    )
    result = fake.requests[1]["messages"][2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == reply["content"][2]["id"]

    # A follow-up in the same conversation sees everything before it.
    def rename(body: dict[str, Any]) -> Any:
        assert "Selected item: kpi_total (index 0)" in body["messages"][-1]["content"]
        ops = [{"op": "replace", "path": "/items/0/title", "value": "Total"}]
        return [
            (
                "call",
                "patch_dashboard",
                {"id": dashboard_id_in(body), "base_version": 2, "ops": ops},
            )
        ], "tool_use"

    fake.turns = [rename, lambda body: ([("text", "Renamed it.")], "end_turn")]
    ask(client, dashboard, "Call this one Total", selected="kpi_total")
    previous = fake.requests[3]["messages"]
    assert fake.requests[4]["messages"][: len(previous)] == previous
    assert fake.requests[4]["messages"][len(previous)]["role"] == "assistant"  # the summary
    assert ok(client.get(f"/api/dashboards/{dashboard}"))["spec"]["items"][0]["title"] == "Total"

    # The panel's view: everything stored, in order, and the conversation's totals.
    state = ok(client.get(f"/api/dashboards/{dashboard}/assistant"))
    assert state["provider"] == "anthropic" and state["model"] == "claude-opus-5-5"
    assert state["running"] is False and state["outdated"] is False
    assert [e["kind"] for e in state["entries"]].count("request") == 2
    assert state["conversation"]["outputTokens"] == 300
    # Every request is audited with its token usage; every write as an agent write.
    actions = db.scalars(select(AuditLog.action).order_by(AuditLog.id)).all()
    assert actions.count("assistant.request") == 2 and actions.count("agent.patch_dashboard") == 2
    usage = db.scalar(select(AuditLog.detail).where(AuditLog.action == "assistant.request"))
    assert usage is not None and usage["outputTokens"] == 200 and usage["provider"] == "anthropic"

    # A new conversation starts empty; the old one is kept.
    fresh = ok(client.post(f"/api/dashboards/{dashboard}/assistant/conversations"), 201)
    assert fresh["entries"] == [] and fresh["conversation"]["id"] != state["conversation"]["id"]


def test_claude_refusals_fallbacks_and_errors(
    client_for: ClientFor,
    source: dict[str, Any],  # noqa: F811
) -> None:
    client = client_for()
    load(client, source)
    dashboard = new_dashboard(client)

    # A declined request: nothing of it is kept in the history.
    refused = {"stop_details": {"type": "refusal", "category": "cyber", "explanation": None}}
    fake = FakeModel("anthropic", [lambda body: ([("text", "Partial"), refused], "refusal")])
    use(client, fake)
    shown = entries(ask(client, dashboard, "Something odd"))
    notice = next(data for kind, data in shown if kind == "notice")
    assert (
        notice["level"] == "warning" and "declined" in notice["text"] and "cyber" in notice["text"]
    )
    assert shown[-1][1]["outcome"] == "refused"

    # After a fallback in mid-reply, the declined part's thinking and tool calls are
    # not sent back, and the person is told who answered.
    iterations = [{"type": "message", "input_tokens": 50, "output_tokens": 5},
                  {"type": "fallback_message", "input_tokens": 60, "output_tokens": 30}]  # fmt: skip
    fake.turns = [
        lambda body: (
            [("update", "Declined part"), ("call", "list_datasets", {}), ("fallback",),
             ("text", "Here is the answer."), {"model": "claude-opus-5", "iterations": iterations}],
            "end_turn",
        ),
        lambda body: ([("text", "ok")], "end_turn"),
    ]  # fmt: skip
    shown = entries(ask(client, dashboard, "Try again"))
    assert any(
        kind == "notice" and "claude-opus-5 answered" in data["text"] for kind, data in shown
    )
    assert not any(kind == "step" for kind, _ in shown)
    ask(client, dashboard, "And again")
    sent = fake.requests[-1]["messages"]
    kept = next(m for m in reversed(sent) if m["role"] == "assistant")
    assert [b["type"] for b in kept["content"]] == ["fallback", "text"]
    # The refused request's partial reply is not there at all, and history stays valid.
    assert sum(1 for m in sent if m["role"] == "assistant") == 1

    # API errors become a plain message for the person.
    fake.fail = (
        401,
        {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}},
    )
    shown = entries(ask(client, dashboard, "Hello"))
    assert any(
        kind == "notice" and "API key was not accepted" in data["text"] for kind, data in shown
    )
    assert shown[-1][1]["outcome"] == "error"


def test_tool_calls_cut_off_are_answered_but_not_run(
    client_for: ClientFor,
    source: dict[str, Any],  # noqa: F811
) -> None:
    client = client_for()
    load(client, source)
    dashboard = new_dashboard(client)
    fake = FakeModel(
        "anthropic",
        [
            lambda body: (
                [("call", "patch_dashboard", {"id": dashboard_id_in(body)})],
                "max_tokens",
            ),
            lambda body: ([("text", "Done in smaller steps.")], "end_turn"),
        ],
    )
    use(client, fake)
    shown = entries(ask(client, dashboard, "Do it"))
    assert not any(kind == "step" for kind, _ in shown)
    assert any(kind == "notice" and "cut off" in data["text"] for kind, data in shown)
    result = fake.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "Not run" in result["content"]
    assert ok(client.get(f"/api/dashboards/{dashboard}"))["version"] == 1


# ---- Ollama ---------------------------------------------------------------------------


def test_ollama_builds_the_same_dashboard(
    client_for: ClientFor,
    source: dict[str, Any],  # noqa: F811
) -> None:
    client = client_for()
    load(client, source)
    dashboard = new_dashboard(client)
    fake = FakeModel("ollama", builder_script())
    use(client, fake)
    shown = entries(ask(client, dashboard, "Build a dashboard of gifts"))
    # Ollama has no progress notes: the update in the script is not part of its format.
    assert [kind for kind, _ in shown] == ["text", "step", "step", "step", "text", "done"]
    assert ok(client.get(f"/api/dashboards/{dashboard}"))["spec"]["items"][0]["id"] == "kpi_total"
    # Ollama's own chat API, with the context size set on every request.
    assert set(fake.paths) == {"/api/chat"}
    first = fake.requests[0]
    assert first["model"] == "gemma4:12b" and first["stream"] is True
    assert first["options"]["num_ctx"] == 32_768 and first["options"]["num_predict"] > 0
    assert (
        first["messages"][0]["role"] == "system"
        and "dashboard editor" in first["messages"][0]["content"]
    )
    assert first["tools"][0]["type"] == "function" and "parameters" in first["tools"][0]["function"]
    second = fake.requests[1]["messages"]
    call = second[-2]["tool_calls"][0]
    assert second[-2]["role"] == "assistant" and call["function"]["name"] == "describe_dataset"
    assert call["function"]["arguments"] == {"dataset": "gifts"}  # an object, not a string
    assert second[-1] == {
        "role": "tool",
        "tool_name": "describe_dataset",
        "tool_call_id": call["id"],
        "content": second[-1]["content"],
    }
    done = shown[-1][1]
    assert (done["provider"], done["inputTokens"], done["cachedTokens"], done["outputTokens"]) == (
        "ollama",
        3600,
        2400,
        160,
    )


def test_ollama_context_size_is_set_and_a_full_context_is_reported(
    client_for: ClientFor,
    source: dict[str, Any],  # noqa: F811
) -> None:
    client = client_for()
    load(client, source)
    dashboard = new_dashboard(client)
    # The context size goes with each request, so a default Ollama install works.
    fake = FakeModel("ollama", [lambda body: ([("text", "Hello.")], "stop")])
    use(client, fake, ollama_num_ctx=65_536)
    ask(client, dashboard, "Hi")
    assert fake.requests[0]["options"]["num_ctx"] == 65_536
    # A reply that used nearly all of it may have missed the start of the conversation.
    fake = FakeModel("ollama", [lambda body: ([("text", "Hm.")], "stop")], prompt_tokens=32_000)
    use(client, fake)
    shown = entries(ask(client, dashboard, "Hello"))
    notice = next(data for kind, data in shown if kind == "notice")
    assert notice["level"] == "error" and "no longer fits" in notice["text"]
    assert "OLLAMA_NUM_CTX" in notice["text"]


def test_budgets_and_guards(
    client_for: ClientFor,
    source: dict[str, Any],  # noqa: F811
) -> None:
    client = client_for()
    load(client, source)
    dashboard = new_dashboard(client)
    other = new_dashboard(client, "Another")
    fake = FakeModel(
        "ollama",
        [
            # Three calls in one reply with a budget of two: the third is not run.
            lambda body: ([("call", "list_datasets", {})] * 3, "stop"),
        ],
    )
    use(client, fake, assistant_max_steps=2)
    shown = entries(ask(client, dashboard, "Look around"))
    assert [kind for kind, _ in shown].count("step") == 2
    assert any(
        kind == "notice" and "budget of 2 tool calls" in data["text"] for kind, data in shown
    )
    assert shown[-1][1]["outcome"] == "budget"

    # Only the open dashboard can be changed; unreadable arguments are not run.
    fake.turns = [
        lambda body: (
            [("call", "patch_dashboard", {"id": other, "base_version": 1,
              "ops": [{"op": "replace", "path": "/title", "value": "Hijacked"}]}),
             ("call", "run_query", "{not json"),
             ("call", "no_such_tool", {})],
            "stop",
        ),
        lambda body: ([("text", "Sorry.")], "stop"),
    ]  # fmt: skip
    use(client, fake)
    shown = entries(ask(client, dashboard, "Change the other one"))
    steps = [data for kind, data in shown if kind == "step"]
    assert [s["ok"] for s in steps] == [False, False, False]
    assert "only change the open dashboard" in steps[0]["output"]
    assert "invalid_json" in steps[1]["output"] and "unknown_tool" in steps[2]["output"]
    assert ok(client.get(f"/api/dashboards/{other}"))["title"] == "Another"
    # Every call got an answer, so the history stays valid for the next request.
    answers = [m for m in fake.requests[-1]["messages"] if m["role"] == "tool"]
    assert len(answers) == 3 + 3


def test_stop_ends_a_request_and_keeps_what_was_written(
    client_for: ClientFor,
    source: dict[str, Any],  # noqa: F811
) -> None:
    client = client_for()
    load(client, source)
    dashboard = new_dashboard(client)
    started = threading.Event()

    async def slow(request: httpx2.Request) -> httpx2.Response:
        async def body() -> Any:
            yield b'{"message":{"role":"assistant","content":"Working on it"},"done":false}\n'
            started.set()
            await asyncio.sleep(30)
            yield b'{"message":{"role":"assistant","content":""},"done":true}\n'

        return httpx2.Response(
            200, headers={"content-type": "application/x-ndjson"}, content=body()
        )

    settings = make_settings()
    client.app.state.assistant_provider = OllamaProvider(settings, httpx2.MockTransport(slow))  # type: ignore[attr-defined]
    result: dict[str, Any] = {}
    sender = threading.Thread(target=lambda: result.update(events=ask(client, dashboard, "Go")))
    begun = time.monotonic()
    sender.start()
    assert started.wait(10)
    assert ok(client.get(f"/api/dashboards/{dashboard}/assistant"))["running"] is True
    busy = client.post(f"/api/dashboards/{dashboard}/assistant/messages", json={"text": "More"})
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "assistant_busy"
    assert ok(client.post(f"/api/dashboards/{dashboard}/assistant/stop"))["stopping"] is True
    sender.join(10)
    assert time.monotonic() - begun < 10
    shown = entries(result["events"])
    assert shown[0] == ("text", {"text": "Working on it", "partial": True})
    assert shown[1] == ("notice", {"level": "info", "text": "Stopped."})
    assert shown[-1][1]["outcome"] == "stopped"
    assert ok(client.get(f"/api/dashboards/{dashboard}/assistant"))["running"] is False


# ---- who may use it ---------------------------------------------------------------------


def test_who_may_use_the_assistant(
    client_for: ClientFor,
    source: dict[str, Any],
    db: Session,  # noqa: F811
) -> None:
    admin = admin_client(client_for, **SERVER_SIGN_IN)
    load(admin, source)
    editor, _ = person(client_for, admin, "ed@example.test", "editor", **SERVER_SIGN_IN)
    viewer, _ = person(client_for, admin, "vi@example.test", "viewer", **SERVER_SIGN_IN)
    mine = new_dashboard(admin, "Admin's")
    fake = FakeModel("ollama", [lambda body: ([("text", "Hello admin.")], "stop")])
    use(admin, fake)
    ask(admin, mine, "Hi")

    # An editor cannot reach a dashboard that is not shared with them, nor see the
    # admin's conversation on it; a viewer cannot use the assistant at all.
    assert editor.get(f"/api/dashboards/{mine}/assistant").status_code in (403, 404)
    assert viewer.get(f"/api/dashboards/{mine}/assistant").status_code == 403
    ok(admin.put(f"/api/dashboards/{mine}/access",
                 json={"shares": [{"grantee": "everyone", "role": "editor"}]}))  # fmt: skip
    state = ok(editor.get(f"/api/dashboards/{mine}/assistant"))
    assert state["conversation"] is None and state["entries"] == []

    # Access tokens are for scripts and MCP, not for the panel.
    token = ok(admin.post("/api/tokens", json={"name": "t", "scopes": ["read", "edit"]}), 201)[
        "token"
    ]
    scripted = client_for(csrf=False, **SERVER_SIGN_IN)
    denied = scripted.get(
        f"/api/dashboards/{mine}/assistant", headers={"Authorization": f"Bearer {token}"}
    )
    assert denied.status_code == 403 and "MCP" in denied.json()["error"]["message"]

    # With the feature off the routes do not exist.
    off = client_for(feature_assistant=False)
    assert off.get(f"/api/dashboards/{mine}/assistant").status_code == 404
    assert off.get("/api/config").json()["assistantModel"] is None


# ---- pieces -------------------------------------------------------------------------------


def test_results_are_capped_before_the_model_sees_them() -> None:
    rows = [[i, f"row {i}"] for i in range(500)]
    shown = json.loads(loop.render({"columns": ["n", "label"], "rows": rows, "rowCount": 500}, 100))
    assert (
        len(shown["rows"]) == 100
        and shown["rowsShown"] == 100
        and "first 100 of 500" in shown["note"]
    )
    long = loop.render({"text": "x" * 100_000}, 100)
    assert len(long) < 41_000 and long.endswith("Ask for less.]")


def test_the_context_names_the_dashboard_selection_and_datasets() -> None:
    spec = {"items": [{"id": "kpi_total", "visual": "kpi", "title": "Total", "dataset": "gifts"}],
            "filters": [{"id": "f_date", "type": "dateRange", "field": "gifts.gift_date"}]}  # fmt: skip
    text = prompt.context_block(
        today="2026-11-20",
        timezone="Australia/Melbourne",
        fiscal_year_start_month=7,
        dashboard={"id": "d-1", "title": "Gifts", "version": 4, "publishedVersion": None},
        spec=spec,
        selected="kpi_total",
        datasets=[
            {"dataset": "gifts", "label": "Gifts", "hasData": True, "rowCount": 2000},
            {"dataset": "late", "label": "Late", "hasData": False},
        ],  # fmt: skip
    )
    assert 'Open dashboard: "Gifts", id d-1, version 4 (not published).' in text
    assert '- index 0: kpi_total (kpi "Total", dataset gifts)' in text
    assert "Selected item: kpi_total (index 0). Its spec:" in text
    assert "- gifts (Gifts, 2000 rows)" in text and "refresh before use): late" in text
    # The instructions never change, so the prompt cache and Claude's thinking hold.
    assert prompt.instructions() == prompt.instructions() and "2026" not in prompt.instructions()


def test_after_a_fallback_only_text_is_sent_back_from_before_it() -> None:
    blocks = [{"type": "thinking", "thinking": "", "signature": "a"}, {"type": "text", "text": "Hi"},
              {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
              {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
              {"type": "thinking", "thinking": "", "signature": "b"}, {"type": "text", "text": "There"}]  # fmt: skip
    assert [b["type"] for b in _echo(blocks)] == ["text", "fallback", "thinking", "text"]
    assert _echo(blocks[:3]) == blocks[:3]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"assistant_provider": "anthropic"},
            "ASSISTANT_PROVIDER=anthropic needs ANTHROPIC_API_KEY",
        ),
        ({"assistant_provider": "anthropic", "anthropic_api_key": "  "}, "needs ANTHROPIC_API_KEY"),
        ({"ollama_url": "host.docker.internal:11434"}, "OLLAMA_URL"),
        ({"ollama_model": " "}, "OLLAMA_MODEL is empty"),
        ({"anthropic_effort": "extreme"}, "ANTHROPIC_EFFORT"),
        ({"assistant_max_steps": 0}, "ASSISTANT_MAX_STEPS"),
        ({"ollama_num_ctx": 4096}, "OLLAMA_NUM_CTX"),
    ],
)
def test_assistant_settings_fail_fast(overrides: dict[str, Any], expected: str) -> None:
    from opsis.settings import ConfigError, build_settings

    with pytest.raises(ConfigError) as info:
        build_settings(**overrides)
    assert expected in str(info.value)


def test_assistant_settings_that_work() -> None:
    from opsis.settings import build_settings

    assert build_settings().assistant_model == "gemma4:12b"
    claude = build_settings(assistant_provider="anthropic", anthropic_api_key="sk-ant-test")
    assert claude.assistant_model == "claude-opus-5-5" and "sk-ant" not in repr(claude)
    assert (
        build_settings(assistant_provider="anthropic", feature_assistant=False).feature_assistant
        is False
    )


def test_a_dropped_tool_call_is_asked_for_again(
    client_for: ClientFor,
    source: dict[str, Any],  # noqa: F811
) -> None:
    client = client_for()
    load(client, source)
    dashboard = new_dashboard(client)
    ASKED = (
        "notice",
        {"level": "info", "text": "The model's tool call did not arrive, so it was asked again."},
    )
    # Ollama drops a tool call it cannot read (gemma4 does this with long nested calls):
    # the reply arrives empty, or with only the sentence before the call.
    fake = FakeModel(
        "ollama",
        [
            lambda body: ([], "stop"),
            lambda body: ([("text", "I will add the KPI card now.")], "stop"),
            lambda body: ([("call", "list_datasets", {})], "stop"),
            lambda body: ([("text", "Added it.")], "stop"),
        ],
    )
    use(client, fake)
    shown = entries(ask(client, dashboard, "Build it"))
    assert [kind for kind, _ in shown] == ["notice", "text", "notice", "step", "text", "done"]
    assert shown[0] == ASKED and shown[1] == ("text", {"text": "I will add the KPI card now."})
    first, second = fake.requests[1]["messages"][-1], fake.requests[2]["messages"][-1]
    assert first["role"] == "user" and "arrived empty" in first["content"]
    assert second["role"] == "user" and "no tool call arrived" in second["content"]
    assert fake.requests[2]["messages"][-2] == {
        "role": "assistant",
        "content": "I will add the KPI card now.",
    }
    # The empty reply itself was not kept.
    assert [m["role"] for m in fake.requests[1]["messages"]][-2:] == ["user", "user"]
    # Small models are asked for one item per call.
    assert "one patch_dashboard call per item" in fake.requests[0]["messages"][0]["content"]

    # After two nudges in a request it stops asking, and says so.
    fake.turns = [lambda body: ([], "stop")] * 3
    shown = entries(ask(client, dashboard, "Again"))
    assert shown[:2] == [ASKED, ASKED]
    assert shown[2] == (
        "notice",
        {
            "level": "warning",
            "text": "The model stopped without replying. Try again, or ask in a different way.",
        },
    )
    assert shown[-1][1]["outcome"] == "empty"

    # Claude's API never drops a call, so a reply that only announces a step ends the turn.
    claude = FakeModel("anthropic", [lambda body: ([("text", "I will look into it.")], "end_turn")])
    use(client, claude)
    assert [kind for kind, _ in entries(ask(client, dashboard, "Hm"))] == ["text", "done"]
    assert "all the changes in one call" in claude.requests[0]["system"][0]["text"]


def test_template_tokens_a_local_model_leaks_are_removed() -> None:
    from opsis.assistant.providers import CONTROL_TOKEN

    leaked = "First, the KPI card.\n<channel|> Then <|tool_response> the chart."
    assert CONTROL_TOKEN.sub("", leaked) == "First, the KPI card.\n Then  the chart."
    assert CONTROL_TOKEN.sub("", "a < b and c > d, x|y") == "a < b and c > d, x|y"
