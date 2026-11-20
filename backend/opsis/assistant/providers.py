"""Model providers: one streamed model call each, to Claude or to Ollama.

Each provider keeps the conversation in its own wire format. The tool loop never
looks inside those messages: it stores what `complete` returns and sends it back
unchanged, which Claude needs (its thinking blocks are only valid in the exact
conversation that produced them, and an unchanged history keeps the prompt cache
warm).

While a call runs the provider reports what the model writes through `emit`:

- ("thinking", {"block"}): the model is thinking; nothing to show yet.
- ("delta", {"block", "kind", "text"}): text for the person ("text") or a short
  progress note between tool calls ("update").
- ("call", {"block", "id", "tool"}): the model has started writing a tool call.
- ("retry", {}): the call is being made again; drop what was shown for it.
"""

from __future__ import annotations

import abc
import json
import logging
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import anthropic
import httpx2

from opsis.mcp.tools import Tool
from opsis.settings import Settings

log = logging.getLogger("opsis.assistant")

Emit = Callable[[str, dict[str, Any]], None]

# Claude: progress notes between tool calls come back as text (they are thinking
# blocks, empty by default), and a request the safety checks decline is re-run
# on the model Anthropic recommends for it.
UPDATES_BETA = "thinking-display-updates-2026-08-18"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# A tool input the SDK cannot parse at all is asked for again, this many times.
JSON_RETRIES = 2
# Ollama: the instructions and tools alone need about 10,000 tokens.
OLLAMA_MIN_CONTEXT = 16_384
OLLAMA_GOOD_CONTEXT = 32_768
# Chat-template tokens a local model sometimes leaks into its text, such as
# "<channel|>" or "<|tool_response>" (seen with gemma4).
CONTROL_TOKEN = re.compile(r"<\|[a-z_]{2,30}\|?>|<[a-z_]{2,30}\|>")


class ProviderError(Exception):
    """A model call that failed. `message` is written for the person using the panel."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass
class ToolCall:
    id: str
    name: str
    # None when the model's JSON could not be read; `raw` then holds what it sent.
    arguments: dict[str, Any] | None
    raw: str = ""


@dataclass
class ToolResult:
    call_id: str
    name: str
    text: str
    is_error: bool = False


@dataclass
class Call:
    system: str
    tools: Sequence[Tool]
    history: list[dict[str, Any]]
    max_tokens: int


@dataclass
class Part:
    """A piece of a reply, in the order the model wrote it."""

    kind: str  # text | update | call
    text: str = ""
    call: ToolCall | None = None


@dataclass
class Reply:
    # The assistant message to append to the history, or None to append nothing
    # (a declined reply is never kept: its partial output may not be used).
    message: dict[str, Any] | None
    parts: list[Part] = field(default_factory=list)
    stop: str = "end"  # end | tools | max_tokens | refusal | context
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    model: str = ""  # who answered: with fallbacks this may not be the model asked
    notices: list[tuple[str, str]] = field(default_factory=list)  # (level, text)

    @property
    def calls(self) -> list[ToolCall]:
        return [part.call for part in self.parts if part.call is not None]

    @property
    def text(self) -> str:
        return "\n\n".join(part.text for part in self.parts if part.kind == "text")


class Provider(abc.ABC):
    name: str
    model: str
    # Ask for one dashboard item per tool call (small models write long calls badly).
    one_item_per_call = False
    # The server may drop a tool call it cannot read, leaving only the text before it.
    may_drop_calls = False

    @abc.abstractmethod
    def user_message(self, text: str) -> dict[str, Any]: ...

    @abc.abstractmethod
    def tool_results(self, results: Sequence[ToolResult]) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    async def complete(self, call: Call, emit: Emit) -> Reply: ...

    async def check(self, first_reply: Reply | None) -> None:  # noqa: B027
        """Raise ProviderError if the model cannot work as configured. Called
        before the first call of a request, and again after it."""

    async def aclose(self) -> None:  # noqa: B027
        pass


# ---- Claude ---------------------------------------------------------------------------


def _echo(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The content to send back. After a fallback in mid-reply, the declined
    model's thinking and tool calls before the last `fallback` block are left
    out; its text and the fallback blocks stay where they were."""
    marks = [i for i, block in enumerate(blocks) if block.get("type") == "fallback"]
    if not marks:
        return blocks
    last = marks[-1]
    return [b for i, b in enumerate(blocks) if i >= last or b.get("type") in ("text", "fallback")]


def _tokens(usage: Any) -> tuple[int, int, int]:
    """(input, output, cache reads) over every attempt, fallbacks included."""
    rows = list(getattr(usage, "iterations", None) or []) or [usage]

    def count(row: Any, name: str) -> int:
        return int(getattr(row, name, 0) or 0)

    read = sum(count(r, "cache_read_input_tokens") for r in rows)
    written = sum(count(r, "cache_creation_input_tokens") for r in rows)
    uncached = sum(count(r, "input_tokens") for r in rows)
    return uncached + written + read, sum(count(r, "output_tokens") for r in rows), read


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, settings: Settings, transport: httpx2.AsyncBaseTransport | None = None):
        key = settings.anthropic_api_key
        self.model = settings.anthropic_model.strip()
        self.effort = settings.anthropic_effort
        self.fallbacks = settings.anthropic_fallbacks
        self.client = anthropic.AsyncAnthropic(
            api_key=key.get_secret_value() if key is not None else None,
            base_url=settings.anthropic_base_url or None,
            max_retries=2,
            timeout=anthropic.Timeout(600.0, connect=10.0),
            http_client=(
                anthropic.DefaultAsyncHttpxClient(transport=transport) if transport else None
            ),
        )

    def user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": text}

    def tool_results(self, results: Sequence[ToolResult]) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.call_id,
                        "content": r.text,
                        **({"is_error": True} if r.is_error else {}),
                    }
                    for r in results
                ],
            }
        ]

    def _params(self, call: Call) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": call.max_tokens,
            # A breakpoint on the system prompt caches the tools and the instructions,
            # the top-level one caches the conversation so far.
            "system": [
                {"type": "text", "text": call.system, "cache_control": {"type": "ephemeral"}}
            ],
            "cache_control": {"type": "ephemeral"},
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    # Inputs stream as they are written; the loop checks every input
                    # against the tool's schema before running it.
                    "eager_input_streaming": True,
                }
                for tool in sorted(call.tools, key=lambda t: t.name)
            ],
            "messages": call.history,
            # Thinking is always on for current models; effort sets how much.
            "thinking": {"type": "adaptive", "display": "updates"},
            "output_config": {"effort": self.effort},
            "betas": [UPDATES_BETA],
        }
        if self.fallbacks:
            params["betas"].append(FALLBACK_BETA)
            params["fallbacks"] = "default"
        return params

    async def complete(self, call: Call, emit: Emit) -> Reply:
        params = self._params(call)
        attempt = 0
        while True:
            try:
                async with self.client.beta.messages.stream(**params) as stream:
                    async for event in stream:
                        _forward(event, emit)
                    final = await stream.get_final_message()
                break
            except anthropic.APIError as error:
                raise ProviderError(self._explain(error)) from None
            except ValueError:
                # Tool input JSON the SDK could not parse at all. The tool call never
                # completed, so there is nothing to answer: ask again (a few times).
                attempt += 1
                if attempt > JSON_RETRIES:
                    raise ProviderError(
                        "Claude's reply could not be read (a tool call was not valid JSON). "
                        "Try again."
                    ) from None
                log.warning("assistant: unreadable tool input from Claude, asking again")
                emit("retry", {})
        return self._reply(final)

    def _reply(self, final: Any) -> Reply:
        content = _echo([block.to_dict(mode="json") for block in final.content])
        input_tokens, output_tokens, cached = _tokens(final.usage)
        parts: list[Part] = []
        for block in content:
            kind = block.get("type")
            if kind == "text" and block["text"].strip():
                parts.append(Part("text", block["text"].strip()))
            elif kind == "thinking" and (block.get("thinking") or "").strip():
                # With display "updates", a thinking block with text is a progress note.
                parts.append(Part("update", block["thinking"].strip()))
            elif kind == "tool_use":
                arguments = block.get("input")
                call = ToolCall(
                    block["id"],
                    block["name"],
                    arguments if isinstance(arguments, dict) else None,
                    raw=json.dumps(arguments)[:2000],
                )
                parts.append(Part("call", call=call))
        reply = Reply(
            message={"role": "assistant", "content": content},
            parts=parts,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached,
            model=str(getattr(final, "model", "") or self.model),
        )
        reason = final.stop_reason
        iterations = getattr(final.usage, "iterations", None) or []
        fell_back = any(getattr(i, "type", "") == "fallback_message" for i in iterations)
        if reason == "refusal":
            details = getattr(final, "stop_details", None)
            category = getattr(details, "category", None)
            about = f" (it was flagged as {category})" if category else ""
            reply.message = None
            reply.stop = "refusal"
            reply.notices.append(
                (
                    "warning",
                    f"Claude declined this request{about}. Nothing more was done. If it was "
                    "an ordinary request, try wording it differently.",
                )
            )
        elif reason == "tool_use" or reply.calls:
            reply.stop = "max_tokens" if reason == "max_tokens" else "tools"
        elif reason == "max_tokens":
            reply.stop = "max_tokens"
        elif reason == "model_context_window_exceeded":
            reply.stop = "context"
        if not content:
            reply.message = None  # the API refuses an empty assistant message
        if fell_back and reason != "refusal":
            reply.notices.append(
                (
                    "info",
                    f"Claude's safety checks declined part of this request, so {reply.model} "
                    "answered instead.",
                )
            )
        return reply

    def _explain(self, error: anthropic.APIError) -> str:
        detail = getattr(error, "message", "") or str(error)
        if isinstance(error, anthropic.AuthenticationError):
            return "The Anthropic API key was not accepted. Check ANTHROPIC_API_KEY."
        if isinstance(error, anthropic.PermissionDeniedError):
            return f"The Anthropic API key may not do this: {detail}"
        if isinstance(error, anthropic.NotFoundError):
            return f"Anthropic does not know the model {self.model!r}. Check ANTHROPIC_MODEL."
        if isinstance(error, anthropic.RateLimitError):
            return "Claude is rate limited for this API key right now. Wait a minute and try again."
        if isinstance(error, anthropic.BadRequestError):
            return f"The Claude API did not accept the request: {detail}"
        if isinstance(error, anthropic.APIStatusError):
            return (
                f"The Claude API is overloaded or unavailable right now ({error.status_code}). "
                "Try again in a moment."
            )
        if isinstance(error, anthropic.APIConnectionError):
            return f"Could not reach the Claude API: {detail}"
        return f"The Claude API call failed: {detail}"

    async def aclose(self) -> None:
        await self.client.close()


def _forward(event: Any, emit: Emit) -> None:
    kind = getattr(event, "type", "")
    if kind == "content_block_start":
        block = event.content_block
        if block.type == "tool_use":
            emit("call", {"block": event.index, "id": block.id, "tool": block.name})
        elif block.type in ("thinking", "redacted_thinking"):
            emit("thinking", {"block": event.index})
    elif kind == "content_block_delta":
        delta = event.delta
        if delta.type == "text_delta" and delta.text:
            emit("delta", {"block": event.index, "kind": "text", "text": delta.text})
        elif delta.type == "thinking_delta" and delta.thinking:
            emit("delta", {"block": event.index, "kind": "update", "text": delta.thinking})


# ---- Ollama -----------------------------------------------------------------------------


class OllamaProvider(Provider):
    """Ollama through its OpenAI-compatible Chat Completions endpoint."""

    name = "ollama"
    one_item_per_call = True
    may_drop_calls = True  # Ollama logs "tool call parsing failed" and sends the rest

    def __init__(self, settings: Settings, transport: httpx2.AsyncBaseTransport | None = None):
        self.url = settings.ollama_url.rstrip("/")
        self.model = settings.ollama_model.strip()
        self.http = httpx2.AsyncClient(
            base_url=self.url,
            timeout=httpx2.Timeout(600.0, connect=10.0),
            transport=transport,
        )

    def user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": text}

    def tool_results(self, results: Sequence[ToolResult]) -> list[dict[str, Any]]:
        return [{"role": "tool", "tool_call_id": r.call_id, "content": r.text} for r in results]

    async def complete(self, call: Call, emit: Emit) -> Reply:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": call.system}, *call.history],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in sorted(call.tools, key=lambda t: t.name)
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": call.max_tokens,
        }
        text: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish = ""
        usage: dict[str, Any] = {}
        thinking = False
        try:
            async with self.http.stream("POST", "/v1/chat/completions", json=body) as response:
                if response.status_code != 200:
                    raw = (await response.aread()).decode("utf-8", errors="replace")
                    raise ProviderError(self._explain(response.status_code, raw))
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if chunk.get("error"):
                        raise ProviderError(self._explain(500, json.dumps(chunk)))
                    usage = chunk.get("usage") or usage
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if (delta.get("reasoning") or delta.get("reasoning_content")) and not (
                            thinking or text
                        ):
                            thinking = True
                            emit("thinking", {"block": 0})
                        if delta.get("content"):
                            text.append(delta["content"])
                            emit("delta", {"block": 0, "kind": "text", "text": delta["content"]})
                        for position, part in enumerate(delta.get("tool_calls") or []):
                            index = int(part.get("index", position))
                            entry = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                            function = part.get("function") or {}
                            if part.get("id"):
                                entry["id"] = part["id"]
                            if function.get("name") and not entry["name"]:
                                entry["name"] = function["name"]
                                emit(
                                    "call",
                                    {"block": index + 1, "id": entry["id"], "tool": entry["name"]},
                                )
                            arguments = function.get("arguments")
                            if isinstance(arguments, dict):  # some servers send an object
                                arguments = json.dumps(arguments)
                            if arguments:
                                entry["arguments"] += arguments
                        finish = choice.get("finish_reason") or finish
        except httpx2.HTTPError as error:
            raise ProviderError(
                f"Could not reach Ollama at {self.url} ({type(error).__name__}). Check that it is "
                "running and that OLLAMA_URL is right."
            ) from None
        except ValueError:
            raise ProviderError("Ollama sent a reply that could not be read.") from None
        return self._reply(CONTROL_TOKEN.sub("", "".join(text)), calls, finish, usage)

    def _reply(
        self, text: str, pieces: dict[int, dict[str, str]], finish: str, usage: dict[str, Any]
    ) -> Reply:
        calls: list[ToolCall] = []
        for index in sorted(pieces):
            part = pieces[index]
            try:
                arguments = json.loads(part["arguments"] or "{}")
            except ValueError:
                arguments = None
            calls.append(
                ToolCall(
                    part["id"] or f"call_{uuid.uuid4().hex[:16]}",
                    part["name"],
                    arguments if isinstance(arguments, dict) else None,
                    raw=part["arguments"][:2000],
                )
            )
        message: dict[str, Any] = {"role": "assistant", "content": text}
        if calls:
            message["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    # Unreadable arguments go back as {}: Ollama would refuse the whole
                    # history otherwise. The tool result says what went wrong.
                    "function": {
                        "name": c.name,
                        "arguments": (pieces[i]["arguments"] or "{}")
                        if c.arguments is not None
                        else "{}",
                    },
                }
                for i, c in zip(sorted(pieces), calls, strict=True)
            ]
        stop = "tools" if calls else "end"
        if finish == "length":
            stop = "max_tokens"
        parts = [Part("text", text.strip())] if text.strip() else []
        parts += [Part("call", call=call) for call in calls]
        return Reply(
            message=message if parts else None,  # an empty reply adds nothing to the history
            parts=parts,
            stop=stop,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            cached_tokens=int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
            model=self.model,
        )

    def _explain(self, status: int, raw: str) -> str:
        try:
            message = str((json.loads(raw).get("error") or {}).get("message") or raw)
        except (ValueError, AttributeError):
            message = raw
        message = message.strip()[:500]
        lowered = message.lower()
        if status == 404 or "not found" in lowered:
            return f"Ollama does not have the model {self.model!r}. Run: ollama pull {self.model}"
        if "does not support tools" in lowered:
            return (
                f"The Ollama model {self.model!r} cannot call tools. Set OLLAMA_MODEL to a model "
                "that can, such as gemma4 or qwen3."
            )
        return f"Ollama returned an error ({status}): {message}"

    async def check(self, first_reply: Reply | None) -> None:
        """Ollama's OpenAI-compatible endpoint cannot set the context size per request,
        and its default (4,096 tokens on most machines) cuts off the instructions. Ask
        Ollama what the loaded model runs with, and say how to fix it."""
        try:
            response = await self.http.get("/api/ps", timeout=5.0)
            loaded = response.json().get("models") or [] if response.status_code == 200 else []
        except (httpx2.HTTPError, ValueError):
            return  # not Ollama, or an old one: nothing to check
        wanted = {self.model, f"{self.model}:latest"}
        for entry in loaded:
            if entry.get("name") not in wanted and entry.get("model") not in wanted:
                continue
            context = int(entry.get("context_length") or 0)
            if 0 < context < OLLAMA_MIN_CONTEXT:
                raise ProviderError(
                    f"Ollama runs {self.model} with room for {context:,} tokens, but the "
                    f"assistant's instructions and tools need about 10,000. Set "
                    f"OLLAMA_CONTEXT_LENGTH={OLLAMA_GOOD_CONTEXT} where Ollama runs and restart "
                    "it (on Windows: set it as a user environment variable, then quit and "
                    "reopen the Ollama app)."
                )
            if first_reply is not None and first_reply.input_tokens >= context * 0.95:
                raise ProviderError(
                    "This conversation no longer fits in the model's context. Start a new "
                    "conversation."
                )

    async def aclose(self) -> None:
        await self.http.aclose()


def build_provider(
    settings: Settings, transport: httpx2.AsyncBaseTransport | None = None
) -> Provider:
    if settings.assistant_provider == "anthropic":
        return AnthropicProvider(settings, transport)
    return OllamaProvider(settings, transport)
