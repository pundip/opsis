"""Model providers: one streamed model call each, to Claude, OpenAI or Ollama.

Each provider keeps the conversation in its own wire format. The tool loop never
looks inside those messages: it stores what `complete` returns and sends it back
unchanged, which Claude needs (its thinking blocks are only valid in the exact
conversation that produced them, and an unchanged history keeps the prompt cache
warm), and OpenAI's reasoning models too (their reasoning items go back as they came).

A provider is made for one request with the key it may use: the person's own key,
the workspace's, or none for Ollama (see assistant/choices.py). The key lives in
the SDK client for that request only and is never logged.

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
import openai

from opsis.mcp.tools import Tool
from opsis.settings import Settings

log = logging.getLogger("opsis.assistant")
# The SDKs log whole requests at debug level, people's words included. Opsis never
# logs those (nor keys), whatever LOG_LEVEL says.
for _name in ("anthropic", "openai"):
    logging.getLogger(_name).setLevel(logging.WARNING)

Emit = Callable[[str, dict[str, Any]], None]

# Claude: progress notes between tool calls come back as text (they are thinking
# blocks, empty by default), and a request the safety checks decline is re-run
# on the model Anthropic recommends for it.
UPDATES_BETA = "thinking-display-updates-2026-08-18"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# A tool input the SDK cannot parse at all is asked for again, this many times.
JSON_RETRIES = 2
# Chat-template tokens a local model sometimes leaks into its text, such as
# "<channel|>" or "<|tool_response>" (seen with gemma4).
CONTROL_TOKEN = re.compile(r"<\|[a-z_]{2,30}\|?>|<[a-z_]{2,30}\|>")

# What a Claude model accepts beyond plain tool use: (progress updates, refusal
# fallbacks), from Anthropic's model documentation (November 2026). Adaptive thinking
# and effort go to every model except the older ones below. A model not listed is
# newer than this table and gets everything.
CLAUDE_OPTIONS: dict[str, tuple[bool, bool]] = {
    "claude-fable-5-2": (True, True),
    "claude-mythos-5-2": (True, True),
    "claude-fable-5-1": (True, True),
    "claude-mythos-5-1": (True, False),
    "claude-fable-5": (True, False),
    "claude-mythos-5": (False, False),
    "claude-opus-5-5": (True, True),
    "claude-opus-5": (False, True),
    "claude-sonnet-5-5": (True, True),
    "claude-sonnet-5": (False, False),
    "claude-haiku-5-5": (False, False),
    "claude-opus-4-8": (False, False),
    "claude-opus-4-7": (False, False),
    "claude-opus-4-6": (False, False),
    "claude-sonnet-4-6": (False, False),
}
CLAUDE_WITHOUT_ADAPTIVE = (
    "claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-5", "claude-opus-4-1",
    "claude-opus-4", "claude-sonnet-4", "claude-3", "claude-2",
)  # fmt: skip
# OpenAI lists every model a key may use; these are not chat models.
NOT_CHAT = re.compile(
    r"embedding|tts|whisper|dall-e|image|audio|realtime|transcribe|moderation|search|"
    r"davinci|babbage|computer-use|sora",
    re.IGNORECASE,
)
MAX_MODELS = 200


def claude_options(model: str) -> tuple[bool, bool, bool]:
    """(adaptive thinking and effort, progress updates, refusal fallbacks) for a model."""
    name = re.sub(r"-\d{8}$", "", model.strip())  # a dated snapshot is its model
    if name.startswith(CLAUDE_WITHOUT_ADAPTIVE):
        return False, False, False
    updates, fallbacks = CLAUDE_OPTIONS.get(name, (True, True))
    return True, updates, fallbacks


def chat_model(name: str) -> bool:
    return name.startswith(("gpt-", "o1", "o3", "o4", "o5", "chatgpt-")) and not NOT_CHAT.search(
        name
    )


class ProviderError(Exception):
    """A model call that failed. `message` is written for the person using the panel."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class KeySource:
    """Whose key a provider calls with, for the messages a person reads."""

    who: str  # 'your Anthropic key "Work"', "the workspace's Anthropic key"
    fix: str  # what to do when the provider refuses it


WORKSPACE_KEY = KeySource(
    "the workspace's Anthropic key", "Ask an admin to check ANTHROPIC_API_KEY."
)


def scrub(text: str, secret: str | None) -> str:
    """A provider's message with the key taken out, should it ever echo one."""
    if secret and len(secret) >= 8 and secret in text:
        return text.replace(secret, f"****{secret[-4:]}")
    return text


def said_by(error: Exception, secret: str | None) -> str:
    """The provider's own words for an error (its body's message), key taken out.
    Both SDKs otherwise write "Error code: 401 - {the whole body}"."""
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        nested = body.get("error")
        inner: dict[str, Any] = nested if isinstance(nested, dict) else body
        message = inner.get("message")
        if isinstance(message, str) and message.strip():
            return scrub(message.strip(), secret)[:500]
    return scrub(str(getattr(error, "message", "") or error), secret)[:500]


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
    # What to append to the history, in order: one message (Claude, Ollama) or the
    # reply's items (OpenAI). Empty to append nothing (a declined reply is never kept:
    # its partial output may not be used).
    messages: list[dict[str, Any]]
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

    def __init__(
        self,
        settings: Settings,
        transport: httpx2.AsyncBaseTransport | None = None,
        *,
        api_key: str | None = None,
        model: str | None = None,
        source: KeySource = WORKSPACE_KEY,
    ) -> None:
        if api_key is None:  # the workspace's key
            key = settings.anthropic_api_key
            api_key = key.get_secret_value() if key is not None else None
        self.model = (model or settings.anthropic_model).strip()
        self.source = source
        self.effort = settings.anthropic_effort
        self.adaptive, self.updates, fallbacks = claude_options(self.model)
        self.fallbacks = settings.anthropic_fallbacks and fallbacks
        self._secret = api_key
        self.client = anthropic.AsyncAnthropic(
            api_key=api_key,
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
        }
        betas: list[str] = []
        if self.adaptive:
            params["cache_control"] = {"type": "ephemeral"}
            # Thinking is always on for current models; effort sets how much.
            thinking: dict[str, Any] = {"type": "adaptive"}
            if self.updates:
                thinking["display"] = "updates"
                betas.append(UPDATES_BETA)
            params["thinking"] = thinking
            params["output_config"] = {"effort": self.effort}
        if self.fallbacks:
            betas.append(FALLBACK_BETA)
            params["fallbacks"] = "default"
        if betas:
            params["betas"] = betas
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
            messages=[{"role": "assistant", "content": content}],
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
            reply.messages = []
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
            reply.messages = []  # the API refuses an empty assistant message
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
        detail = said_by(error, self._secret)
        who = self.source.who
        if isinstance(error, anthropic.AuthenticationError):
            return f"{who[:1].upper()}{who[1:]} was not accepted. {self.source.fix}"
        if isinstance(error, anthropic.PermissionDeniedError):
            return f"{who[:1].upper()}{who[1:]} may not do this: {detail}"
        if isinstance(error, anthropic.NotFoundError):
            return (
                f"Anthropic does not know the model {self.model!r}, or {who} cannot use it. "
                "Choose another model."
            )
        if isinstance(error, anthropic.RateLimitError):
            return f"Claude is rate limited for {who} right now. Wait a minute and try again."
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


# ---- OpenAI ---------------------------------------------------------------------------


class OpenAIProvider(Provider):
    """OpenAI's Responses API, streamed, through the official SDK (v2, ADR-036).

    Nothing is stored at OpenAI (`store: false`): the whole history goes with each
    call, as for Claude. Reasoning models' reasoning comes back encrypted and goes
    back unchanged, so they keep their train of thought between tool calls. Newer
    models mark text between tool calls as commentary, shown as progress notes.
    """

    name = "openai"

    def __init__(
        self,
        settings: Settings,
        transport: httpx2.AsyncBaseTransport | None = None,
        *,
        api_key: str,
        model: str,
        source: KeySource,
    ) -> None:
        self.model = model.strip()
        self.source = source
        self._secret = api_key
        # Encrypted reasoning, asked for until a model says it has none.
        self.include_reasoning = True
        self.client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=settings.openai_base_url or None,
            max_retries=2,
            timeout=openai.Timeout(600.0, connect=10.0),
            http_client=(
                openai.DefaultAsyncHttpx2Client(transport=transport) if transport else None
            ),
        )

    def user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": text}

    def tool_results(self, results: Sequence[ToolResult]) -> list[dict[str, Any]]:
        return [
            {"type": "function_call_output", "call_id": r.call_id, "output": r.text}
            for r in results
        ]

    def _params(self, call: Call) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "instructions": call.system,
            "input": call.history,
            "tools": [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                    # The loop checks every input against the tool's schema itself.
                    "strict": False,
                }
                for tool in sorted(call.tools, key=lambda t: t.name)
            ],
            "max_output_tokens": call.max_tokens,
            "store": False,
            "stream": True,
        }
        if self.include_reasoning:
            params["include"] = ["reasoning.encrypted_content"]
        return params

    async def complete(self, call: Call, emit: Emit) -> Reply:
        while True:
            try:
                final = await self._stream(self._params(call), emit)
                break
            except openai.BadRequestError as error:
                text = str(getattr(error, "message", "") or error).lower()
                if self.include_reasoning and ("include" in text or "encrypted" in text):
                    self.include_reasoning = False  # a model without reasoning items
                    emit("retry", {})
                    continue
                raise ProviderError(self._explain(error)) from None
            except openai.APIError as error:
                raise ProviderError(self._explain(error)) from None
        return self._reply(final)

    async def _stream(self, params: dict[str, Any], emit: Emit) -> Any:
        stream = await self.client.responses.create(**params)
        final: Any = None
        kinds: dict[int, str] = {}
        async for event in stream:
            kind = getattr(event, "type", "")
            if kind == "response.output_item.added":
                item, index = event.item, event.output_index
                if item.type == "reasoning":
                    emit("thinking", {"block": index})
                elif item.type == "function_call":
                    emit("call", {"block": index, "id": item.call_id, "tool": item.name})
                elif item.type == "message":
                    commentary = getattr(item, "phase", None) == "commentary"
                    kinds[index] = "update" if commentary else "text"
            elif kind == "response.output_text.delta" and event.delta:
                index = event.output_index
                emit(
                    "delta", {"block": index, "kind": kinds.get(index, "text"), "text": event.delta}
                )
            elif kind in ("response.completed", "response.incomplete"):
                final = event.response
            elif kind == "response.failed":
                problem = getattr(event.response, "error", None)
                message = getattr(problem, "message", None) or "the response failed"
                raise ProviderError(f"OpenAI could not answer: {scrub(message, self._secret)}")
            elif kind == "error":
                raise ProviderError(
                    f"OpenAI reported an error: {scrub(event.message, self._secret)}"
                )
        if final is None:
            raise ProviderError("OpenAI's reply ended before it was complete. Try again.")
        return final

    def _reply(self, final: Any) -> Reply:
        parts: list[Part] = []
        refused: str | None = None
        for item in final.output:
            if item.type == "message":
                kind = "update" if getattr(item, "phase", None) == "commentary" else "text"
                for content in item.content:
                    if content.type == "output_text" and content.text.strip():
                        parts.append(Part(kind, content.text.strip()))
                    elif content.type == "refusal":
                        refused = content.refusal
            elif item.type == "function_call":
                try:
                    arguments = json.loads(item.arguments or "{}")
                except ValueError:
                    arguments = None
                call = ToolCall(
                    item.call_id,
                    item.name,
                    arguments if isinstance(arguments, dict) else None,
                    raw=(item.arguments or "")[:2000],
                )
                parts.append(Part("call", call=call))
        usage = final.usage
        details = getattr(usage, "input_tokens_details", None)
        reply = Reply(
            # By alias: some fields have Python names (`async_` is sent as `async`).
            messages=[
                item.model_dump(mode="json", exclude_none=True, by_alias=True)
                for item in final.output
            ],
            parts=parts,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cached_tokens=int(getattr(details, "cached_tokens", 0) or 0),
            model=str(getattr(final, "model", "") or self.model),
        )
        incomplete = getattr(final, "incomplete_details", None)
        reason = getattr(incomplete, "reason", None)
        if reply.calls:
            reply.stop = "max_tokens" if reason == "max_output_tokens" else "tools"
        elif reason == "max_output_tokens":
            reply.stop = "max_tokens"
        elif refused is not None or reason == "content_filter":
            reply.stop = "refusal"
            reply.messages = []
            reply.notices.append(
                (
                    "warning",
                    "The OpenAI model declined this request"
                    + (f": {refused.strip()}" if refused and refused.strip() else ".")
                    + " Nothing more was done.",
                )
            )
        if not parts and reply.stop == "end":
            reply.messages = []  # nothing to keep
        return reply

    def _explain(self, error: openai.APIError) -> str:
        detail = said_by(error, self._secret)
        code = getattr(error, "code", None)
        who = self.source.who
        if isinstance(error, openai.AuthenticationError):
            return f"{who[:1].upper()}{who[1:]} was not accepted by OpenAI. {self.source.fix}"
        if isinstance(error, openai.PermissionDeniedError):
            return f"{who[:1].upper()}{who[1:]} may not do this: {detail}"
        if isinstance(error, openai.NotFoundError):
            return (
                f"OpenAI does not know the model {self.model!r}, or {who} cannot use it. "
                "Choose another model."
            )
        if isinstance(error, openai.RateLimitError):
            if code == "insufficient_quota":
                return (
                    f"The OpenAI account behind {who} has no credit left. Add credit at "
                    "OpenAI, or choose another key."
                )
            return f"OpenAI is rate limited for {who} right now. Wait a minute and try again."
        if isinstance(error, openai.BadRequestError):
            if code == "context_length_exceeded":
                return "The conversation is too long for the model. Start a new conversation."
            return f"The OpenAI API did not accept the request: {detail}"
        if isinstance(error, openai.APIStatusError):
            return (
                f"The OpenAI API is overloaded or unavailable right now ({error.status_code}). "
                "Try again in a moment."
            )
        if isinstance(error, openai.APITimeoutError):
            return "OpenAI took too long to answer. Try again."
        if isinstance(error, openai.APIConnectionError):
            return f"Could not reach the OpenAI API: {detail}"
        return f"The OpenAI API call failed: {detail}"

    async def aclose(self) -> None:
        await self.client.close()


# ---- Ollama -----------------------------------------------------------------------------


class OllamaProvider(Provider):
    """Ollama through its own chat API (/api/chat, streamed as JSON lines).

    Its own API rather than the OpenAI-compatible one, because only this one can set
    the context size per request: Ollama's default (4,096 tokens on most machines)
    cuts off the assistant's instructions and tools, so a default install would not
    work (decided at the M7 review).
    """

    name = "ollama"
    one_item_per_call = True
    may_drop_calls = True  # Ollama logs "tool call parsing failed" and sends the rest

    def __init__(self, settings: Settings, transport: httpx2.AsyncBaseTransport | None = None):
        self.url = settings.ollama_url.rstrip("/")
        self.model = settings.ollama_model.strip()
        self.num_ctx = settings.ollama_num_ctx
        self.http = httpx2.AsyncClient(
            base_url=self.url,
            timeout=httpx2.Timeout(600.0, connect=10.0),
            transport=transport,
        )

    def user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": text}

    def tool_results(self, results: Sequence[ToolResult]) -> list[dict[str, Any]]:
        return [
            {"role": "tool", "tool_name": r.name, "tool_call_id": r.call_id, "content": r.text}
            for r in results
        ]

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
            "options": {"num_ctx": self.num_ctx, "num_predict": call.max_tokens},
        }
        text: list[str] = []
        calls: list[tuple[str, str, Any]] = []
        last: dict[str, Any] = {}
        thinking = False
        try:
            async with self.http.stream("POST", "/api/chat", json=body) as response:
                if response.status_code != 200:
                    raw = (await response.aread()).decode("utf-8", errors="replace")
                    raise ProviderError(self._explain(response.status_code, raw))
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise ProviderError(self._explain(500, line))
                    message = chunk.get("message") or {}
                    if message.get("thinking") and not (thinking or text):
                        thinking = True
                        emit("thinking", {"block": 0})
                    if message.get("content"):
                        text.append(message["content"])
                        emit("delta", {"block": 0, "kind": "text", "text": message["content"]})
                    for part in message.get("tool_calls") or []:
                        function = part.get("function") or {}
                        call_id = str(part.get("id") or f"call_{uuid.uuid4().hex[:16]}")
                        name = str(function.get("name") or "")
                        calls.append((call_id, name, function.get("arguments")))
                        emit("call", {"block": len(calls), "id": call_id, "tool": name})
                    if chunk.get("done"):
                        last = chunk
                        break
        except httpx2.HTTPError as error:
            raise ProviderError(
                f"Could not reach Ollama at {self.url} ({type(error).__name__}). Check that it is "
                "running and that OLLAMA_URL is right."
            ) from None
        except ValueError:
            raise ProviderError("Ollama sent a reply that could not be read.") from None
        return self._reply(CONTROL_TOKEN.sub("", "".join(text)), calls, last)

    def _reply(self, text: str, found: list[tuple[str, str, Any]], last: dict[str, Any]) -> Reply:
        calls: list[ToolCall] = []
        for call_id, name, arguments in found:
            raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments or "{}")
                except ValueError:
                    arguments = None
            calls.append(
                ToolCall(
                    call_id,
                    name,
                    arguments if isinstance(arguments, dict) else None,
                    raw=(raw or "")[:2000],
                )
            )
        message: dict[str, Any] = {"role": "assistant", "content": text}
        if calls:
            # Unreadable arguments go back as {}: the tool result says what went wrong.
            message["tool_calls"] = [
                {"id": c.id, "function": {"name": c.name, "arguments": c.arguments or {}}}
                for c in calls
            ]
        stop = "tools" if calls else "end"
        if last.get("done_reason") == "length":
            stop = "max_tokens"
        parts = [Part("text", text.strip())] if text.strip() else []
        parts += [Part("call", call=call) for call in calls]
        return Reply(
            messages=[message] if parts else [],  # an empty reply adds nothing to the history
            parts=parts,
            stop=stop,
            input_tokens=int(last.get("prompt_eval_count") or 0),
            output_tokens=int(last.get("eval_count") or 0),
            cached_tokens=int(last.get("prompt_eval_cached_count") or 0),
            model=self.model,
        )

    def _explain(self, status: int, raw: str) -> str:
        try:
            error = json.loads(raw).get("error")
            message = str(error.get("message") if isinstance(error, dict) else error or raw)
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
        """Ollama drops the oldest messages of a conversation that no longer fits its
        context, so a reply that used nearly all of it may have missed the request."""
        if first_reply is not None and first_reply.input_tokens >= self.num_ctx * 0.95:
            raise ProviderError(
                f"This conversation no longer fits in the model's context ({self.num_ctx:,} "
                "tokens). Start a new conversation, or raise OLLAMA_NUM_CTX."
            )

    async def aclose(self) -> None:
        await self.http.aclose()


# ---- keys: the Test button and the model lists -----------------------------------------


@dataclass
class KeyCheck:
    ok: bool
    message: str
    models: list[dict[str, str]] = field(default_factory=list)  # {"id", "name"}


def _anthropic_models(
    settings: Settings, api_key: str, transport: httpx2.BaseTransport | None
) -> list[dict[str, str]]:
    client = anthropic.Anthropic(
        api_key=api_key,
        base_url=settings.anthropic_base_url or None,
        max_retries=1,
        timeout=30.0,
        http_client=anthropic.DefaultHttpxClient(transport=transport) if transport else None,
    )
    try:
        found: list[dict[str, str]] = []
        for model in client.models.list(limit=100):  # the page object pages on by itself
            found.append({"id": model.id, "name": model.display_name or model.id})
            if len(found) >= MAX_MODELS:
                break
        return found
    finally:
        client.close()


def _openai_models(
    settings: Settings, api_key: str, transport: httpx2.BaseTransport | None
) -> list[dict[str, str]]:
    client = openai.OpenAI(
        api_key=api_key,
        base_url=settings.openai_base_url or None,
        max_retries=1,
        timeout=30.0,
        http_client=openai.DefaultHttpx2Client(transport=transport) if transport else None,
    )
    try:
        names = sorted({model.id for model in client.models.list() if chat_model(model.id)})
        return [{"id": name, "name": name} for name in names[:MAX_MODELS]]
    finally:
        client.close()


def _key_error(provider: str, error: Exception, api_key: str) -> str:
    company = "Anthropic" if provider == "anthropic" else "OpenAI"
    detail = said_by(error, api_key)
    status = getattr(error, "status_code", None)
    if status == 401:
        return f"{company} did not accept the key: {detail}"
    if status == 403:
        return f"{company} accepted the key but refused this: {detail}"
    if status == 429:
        return f"{company} is rate limiting this key, or its account has no credit: {detail}"
    if status is not None:
        return f"{company} answered with an error ({status}): {detail}"
    return f"Could not reach {company}: {detail}"


def list_models(
    settings: Settings,
    provider: str,
    api_key: str,
    transport: httpx2.BaseTransport | None = None,
) -> list[dict[str, str]]:
    """The models a key may use. Raises ProviderError with the provider's own words."""
    try:
        if provider == "anthropic":
            return _anthropic_models(settings, api_key, transport)
        return _openai_models(settings, api_key, transport)
    except (anthropic.APIError, openai.APIError) as error:
        raise ProviderError(_key_error(provider, error, api_key)) from None


def check_key(
    settings: Settings,
    provider: str,
    api_key: str,
    model: str | None,
    transport: httpx2.BaseTransport | None = None,
) -> KeyCheck:
    """The Test button: ask the provider which models the key may use. It costs
    nothing, and it proves the key is accepted."""
    try:
        models = list_models(settings, provider, api_key, transport)
    except ProviderError as error:
        return KeyCheck(False, error.message)
    message = f"The key works: it may use {len(models)} model{'s' if len(models) != 1 else ''}."
    if model and model not in {m["id"] for m in models}:
        message += f" {model} is not one of them, so choose another default model."
    return KeyCheck(True, message, models)


# The optional "Send a test message" button (V1 review): one short request, which the
# provider bills, so it shows whether the key's account can pay. The free Test above
# proves only that the key is accepted. OpenAI's Responses API takes no limit below 16
# output tokens; Claude takes 1.
TEST_MESSAGE = "Reply with OK."
TEST_MAX_TOKENS = {"anthropic": 1, "openai": 16}


@dataclass
class MessageCheck:
    ok: bool
    message: str
    input_tokens: int = 0
    output_tokens: int = 0


def _claude_test_message(
    settings: Settings, api_key: str, model: str, transport: httpx2.BaseTransport | None
) -> tuple[int, int]:
    """Tokens in and out of one short request to Claude (not streamed, no tools)."""
    client = anthropic.Anthropic(
        api_key=api_key,
        base_url=settings.anthropic_base_url or None,
        max_retries=1,
        timeout=60.0,
        http_client=anthropic.DefaultHttpxClient(transport=transport) if transport else None,
    )
    try:
        reply = client.messages.create(
            model=model,
            max_tokens=TEST_MAX_TOKENS["anthropic"],
            messages=[{"role": "user", "content": TEST_MESSAGE}],
        )
    finally:
        client.close()
    return reply.usage.input_tokens, reply.usage.output_tokens


def _openai_test_message(
    settings: Settings, api_key: str, model: str, transport: httpx2.BaseTransport | None
) -> tuple[int, int]:
    """Tokens in and out of one short request to OpenAI (not streamed, nothing stored)."""
    client = openai.OpenAI(
        api_key=api_key,
        base_url=settings.openai_base_url or None,
        max_retries=1,
        timeout=60.0,
        http_client=openai.DefaultHttpx2Client(transport=transport) if transport else None,
    )
    try:
        response = client.responses.create(
            model=model,
            input=TEST_MESSAGE,
            max_output_tokens=TEST_MAX_TOKENS["openai"],
            store=False,
        )
    finally:
        client.close()
    usage = response.usage
    return (usage.input_tokens, usage.output_tokens) if usage else (0, 0)


def send_test_message(
    settings: Settings,
    provider: str,
    api_key: str,
    model: str,
    transport: httpx2.BaseTransport | None = None,
) -> MessageCheck:
    """One short billed request to a model. Any answer proves the account can pay, even
    one cut off at its limit (a model that thinks first spends its one token on that);
    otherwise the provider's own words."""
    try:
        if provider == "anthropic":
            used = _claude_test_message(settings, api_key, model, transport)
        else:
            used = _openai_test_message(settings, api_key, model, transport)
    except (anthropic.APIError, openai.APIError) as error:
        return MessageCheck(False, _key_error(provider, error, api_key))
    return MessageCheck(
        True,
        f"The key works, and its account paid for a test message: {model} answered "
        f"({used[0]} tokens in, {used[1]} out).",
        *used,
    )
