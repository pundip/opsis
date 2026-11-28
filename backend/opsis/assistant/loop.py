"""The tool loop: one request to the assistant, from the person's words to "done".

Model call, then the tool calls it asked for, then another model call with the
results, until the model answers without tools or a budget runs out. Budgets
per request (brief 10.2): tool calls (ASSISTANT_MAX_STEPS), tokens the model
writes (ASSISTANT_MAX_OUTPUT_TOKENS), wall-clock time (ASSISTANT_TIMEOUT_SECONDS)
and rows of a tool result shown to the model (ASSISTANT_MAX_ROWS). Token usage
is logged and audited per request.

Tools are the MCP tools, called directly through `opsis.mcp.tools.call`, with
the person's own permissions. Changes are saved as new dashboard versions by
those tools, so each one can be undone from the panel.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

import anyio.to_thread
from sqlalchemy.orm import Session

from opsis.assistant import choices, prompt
from opsis.assistant.providers import (
    Call,
    Part,
    Provider,
    ProviderError,
    Reply,
    ToolCall,
    ToolResult,
)
from opsis.assistant.runs import Run
from opsis.db.models import AssistantUsage, Dashboard, UserApiKey
from opsis.mcp import tools
from opsis.services import assistant as store
from opsis.services import audit, dashboards

log = logging.getLogger("opsis.assistant")
T = TypeVar("T")

# The MCP tools the assistant may use, in this order. Left out: creating,
# publishing and sharing dashboards (the person does those with the editor's
# buttons), tools that are no use from inside one dashboard, and changes to the
# workspace model's relationships (people make those in the Model view: they change
# every dashboard and where row-level security reaches). It reads the model, and can
# suggest relationships for the user to add there.
TOOL_NAMES = (
    "list_datasets",
    "describe_dataset",
    "list_visuals",
    "get_spec_schema",
    "run_query",
    "preview_dataset",
    "profile_column",
    "get_semantic_model",
    "patch_semantic_model",
    "get_workspace_model",
    "suggest_relationships",
    "list_dashboards",
    "get_dashboard",
    "validate_spec",
    "patch_dashboard",
    "list_versions",
    "restore_version",
    "trigger_refresh",
    "get_job_status",
)
# Tools that change a dashboard may only change the open one.
ON_THIS_DASHBOARD = ("patch_dashboard", "restore_version")
PER_CALL_MAX_TOKENS = 32_000
MIN_CALL_TOKENS = 4_000
# A tool result longer than this is cut before the model sees it.
MAX_RESULT_CHARS = 40_000
# Tool inputs and results are kept for the panel up to this length.
SHOWN_CHARS = 8_000
# Sent when a reply comes back with nothing in it, or (from a server that drops tool
# calls it cannot read) announces a step without the call. At most twice a request.
EMPTY_REPLY = (
    "(From Opsis: your last reply arrived empty. If you made a tool call, it could not be "
    "read. Carry on with the next step: make the tool call again with valid JSON arguments, "
    "or answer in a sentence.)"
)
MISSING_CALL = (
    "(From Opsis: no tool call arrived with your last reply. If you meant to make one, it "
    "could not be read: make it again, smaller if you can, with valid JSON arguments. If you "
    "have finished, say so in a sentence.)"
)
MAX_NUDGES = 2
# A reply that says what it is about to do, rather than what it did.
ANNOUNCES = re.compile(
    r"\b(I will|I'll|I am going to|I'm going to|let me|next,? I|now I)\b", re.IGNORECASE
)


def tools_for(settings: Any, principal: Any) -> list[tools.Tool]:
    offered = {tool.name: tool for tool in tools.available(settings, principal)}
    return [offered[name] for name in TOOL_NAMES if name in offered]


def render(result: Any, max_rows: int) -> str:
    """A tool result as the model reads it: compact JSON, rows capped."""
    if isinstance(result, dict) and isinstance(result.get("rows"), list):
        rows = result["rows"]
        if len(rows) > max_rows:
            result = {
                **result,
                "rows": rows[:max_rows],
                "rowsShown": max_rows,
                "note": (
                    f"Only the first {max_rows} of {len(rows)} rows are shown to you. Add "
                    "filters, a sort or a smaller limit to get the rows that matter."
                ),
            }
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = (
            text[:MAX_RESULT_CHARS]
            + f" ... [cut off here: the result had {len(text):,} characters. Ask for less.]"
        )
    return text


def _shorten(text: str) -> str:
    return text if len(text) <= SHOWN_CHARS else text[:SHOWN_CHARS] + " ..."


class Stopped(Exception):
    pass


class TimedOut(Exception):
    pass


@dataclass
class Totals:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    model_calls: int = 0
    steps: int = 0
    model: str = ""

    def add(self, reply: Reply) -> None:
        self.input_tokens += reply.input_tokens
        self.output_tokens += reply.output_tokens
        self.cached_tokens += reply.cached_tokens
        self.model_calls += 1
        self.model = reply.model or self.model


class Request:
    """One request: runs as a task, reports through `run.emit`, stores as it goes."""

    def __init__(
        self,
        *,
        run: Run,
        ctx: tools.ToolContext,
        provider: Provider,
        conversation_id: uuid.UUID,
        dashboard_id: uuid.UUID,
        text: str,
        selected: str | None,
        option: choices.Option | None = None,
    ) -> None:
        self.run = run
        self.ctx = ctx
        self.settings = ctx.settings
        self.provider = provider
        # Whose key this request runs on: checked again before every model call.
        self.option = option
        self.conversation_id = conversation_id
        self.dashboard_id = dashboard_id
        self.text = text
        self.selected = selected
        self.tools = tools_for(ctx.settings, ctx.principal)
        self.by_name = {tool.name: tool for tool in self.tools}
        self.totals = Totals()
        self.started = time.monotonic()
        self.deadline = self.started + self.settings.assistant_timeout_seconds
        # Text streamed for the model call in progress, by block, so that a reply
        # cut short by Stop is still kept as far as it got.
        self.partial: dict[int, dict[str, str]] = {}
        self.outcome = "done"

    # ---- plumbing ------------------------------------------------------------------

    async def _db(self, work: Callable[[Session], T]) -> T:
        def run() -> T:
            with self.ctx.session_factory() as db:
                return work(db)

        return await anyio.to_thread.run_sync(run)

    async def _entry(self, kind: str, data: dict[str, Any]) -> None:
        entry = await self._db(lambda db: store.add_entry(db, self.conversation_id, kind, data))
        self.run.emit("entry", entry)

    async def _append(self, messages: list[dict[str, Any]], history: list[dict[str, Any]]) -> None:
        await self._db(lambda db: store.append_messages(db, self.conversation_id, messages))
        history.extend(messages)

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if event == "delta":
            part = self.partial.setdefault(int(data["block"]), {"kind": data["kind"], "text": ""})
            part["text"] += str(data["text"])
        elif event == "retry":
            self.partial.clear()
        self.run.emit(event, data)

    async def _guard(self, work: Coroutine[Any, Any, Reply]) -> Reply:
        """Run a model call, unless the person stops the request or time runs out."""
        call = asyncio.ensure_future(work)
        stop = asyncio.ensure_future(self.run.stop.wait())
        try:
            await asyncio.wait(
                {call, stop},
                timeout=max(0.0, self.deadline - time.monotonic()),
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            call.cancel()
            raise
        finally:
            stop.cancel()
        if call.done():
            return call.result()
        call.cancel()
        with contextlib.suppress(asyncio.CancelledError, ProviderError):
            await call
        if self.run.stop.is_set():
            raise Stopped
        raise TimedOut

    def _version(self, db: Session) -> int | None:
        dashboard = db.get(Dashboard, self.dashboard_id)
        return dashboard.version if dashboard is not None else None

    def _context(self, db: Session) -> str:
        dashboard = db.get(Dashboard, self.dashboard_id)
        if dashboard is None:
            raise ProviderError("The dashboard no longer exists.")
        detail = dashboards.detail(db, dashboard, self.ctx.principal)
        listing = tools.list_datasets(self.ctx, db, {})["datasets"]
        return prompt.context_block(
            today=self.settings.today().isoformat(),
            timezone=self.settings.workspace_timezone,
            fiscal_year_start_month=self.settings.fiscal_year_start_month,
            dashboard=detail,
            spec=detail["spec"],
            selected=self.selected,
            datasets=listing,
        )

    # ---- the request -----------------------------------------------------------------

    async def execute(self) -> None:
        before: int | None = None
        try:
            before = await self._db(self._version)
            await self._converse()
        except Stopped:
            self.outcome = "stopped"
            await self._keep_partial()
            await self._entry("notice", {"level": "info", "text": "Stopped."})
        except TimedOut:
            self.outcome = "timed_out"
            await self._keep_partial()
            minutes = self.settings.assistant_timeout_seconds / 60
            await self._entry(
                "notice",
                {
                    "level": "warning",
                    "text": f"Stopped: this request ran for more than {minutes:g} minutes. "
                    "Ask the assistant to continue if it has not finished.",
                },
            )
        except ProviderError as error:
            self.outcome = "error"
            await self._entry("notice", {"level": "error", "text": error.message})
        except Exception:
            self.outcome = "error"
            log.exception("assistant: the request failed")
            await self._entry(
                "notice",
                {
                    "level": "error",
                    "text": "Something went wrong in the assistant. The details are in the "
                    "server log.",
                },
            )
        finally:
            await self._finish(before)

    async def _keep_partial(self) -> None:
        for part in self.partial.values():
            if part["text"].strip():
                await self._entry(part["kind"], {"text": part["text"].strip(), "partial": True})
        self.partial.clear()

    async def _nudge(self, text: str, history: list[dict[str, Any]]) -> None:
        await self._entry(
            "notice",
            {
                "level": "info",
                "text": "The model's tool call did not arrive, so it was asked again.",
            },
        )
        await self._append([self.provider.user_message(text)], history)

    async def _converse(self) -> None:
        system = prompt.instructions(one_item_per_call=self.provider.one_item_per_call)
        history = await self._db(lambda db: store.history(db, self.conversation_id))
        context = await self._db(self._context)
        await self._append([self.provider.user_message(f"{context}\n\n{self.text}")], history)
        await self.provider.check(None)
        nudges = 0
        while True:
            remaining = self.settings.assistant_max_output_tokens - self.totals.output_tokens
            if remaining < MIN_CALL_TOKENS:
                self.outcome = "budget"
                await self._entry(
                    "notice",
                    {
                        "level": "warning",
                        "text": "Stopped: this request used its budget of "
                        f"{self.settings.assistant_max_output_tokens:,} output tokens. Ask the "
                        "assistant to continue if it has not finished.",
                    },
                )
                return
            await self._permit()
            self.partial.clear()
            reply = await self._guard(
                self.provider.complete(
                    Call(system, self.tools, history, min(PER_CALL_MAX_TOKENS, remaining)),
                    self._emit,
                )
            )
            self.partial.clear()
            self.totals.add(reply)
            await self.provider.check(reply)
            if reply.messages:
                await self._append(reply.messages, history)
            for level, text in reply.notices:
                await self._entry("notice", {"level": level, "text": text})
            if not reply.parts and reply.stop == "end":
                # Nothing at all: usually a tool call the server could not read and
                # dropped (seen with small local models). Ask again.
                if nudges < MAX_NUDGES:
                    nudges += 1
                    await self._nudge(EMPTY_REPLY, history)
                    continue
                await self._entry(
                    "notice",
                    {
                        "level": "warning",
                        "text": "The model stopped without replying. Try again, or ask in a "
                        "different way.",
                    },
                )
                self.outcome = "empty"
                return
            if not reply.calls:
                if reply.stop != "refusal":  # a declined reply's partial output is dropped
                    await self._show(reply.parts)
                if (
                    reply.stop == "end"
                    and self.provider.may_drop_calls
                    and nudges < MAX_NUDGES
                    and ANNOUNCES.search(reply.text.strip().split("\n")[-1])
                ):
                    # "I will add the chart." and nothing more: the call was likely dropped.
                    nudges += 1
                    await self._nudge(MISSING_CALL, history)
                    continue
                if reply.stop == "max_tokens":
                    await self._entry(
                        "notice",
                        {"level": "warning", "text": "The reply was cut off: it got too long."},
                    )
                elif reply.stop == "context":
                    await self._entry(
                        "notice",
                        {
                            "level": "warning",
                            "text": "The conversation is too long for the model. Start a new "
                            "conversation.",
                        },
                    )
                if reply.stop == "refusal":
                    self.outcome = "refused"
                return
            results, halt = await self._work_through(reply)
            await self._append(self.provider.tool_results(results), history)
            if halt == "stopped":
                raise Stopped
            if halt == "time":
                raise TimedOut
            if halt == "steps":
                self.outcome = "budget"
                await self._entry(
                    "notice",
                    {
                        "level": "warning",
                        "text": "Stopped: this request used its budget of "
                        f"{self.settings.assistant_max_steps} tool calls. Ask the assistant to "
                        "continue if it has not finished.",
                    },
                )
                return
            if reply.stop == "max_tokens":
                await self._entry(
                    "notice",
                    {
                        "level": "warning",
                        "text": "A tool call was cut off before it was complete, so it was not "
                        "run. The assistant will try again in smaller steps.",
                    },
                )

    async def _permit(self) -> None:
        """Stop when the person may no longer use this option (a grant taken away, the
        key deleted, personal keys switched off): it takes effect at once."""
        option = self.option
        if option is None:
            return
        user_id = self.ctx.principal.user_id
        reason = await self._db(
            lambda db: choices.still_allowed(db, self.settings, user_id, option.id)
        )
        if reason is not None:
            raise ProviderError(reason)

    async def _show(self, parts: list[Part]) -> None:
        for part in parts:
            if part.kind in ("text", "update"):
                await self._entry(part.kind, {"text": part.text})

    async def _work_through(self, reply: Reply) -> tuple[list[ToolResult], str | None]:
        """Show the reply's text and run its tool calls, in the order written.
        Every call gets a result, run or not, so the history stays valid."""
        results: list[ToolResult] = []
        halt: str | None = None
        for part in reply.parts:
            if part.call is None:
                await self._show([part])
                continue
            call = part.call
            refusal = None
            if reply.stop == "max_tokens":
                refusal = (
                    "Not run: your reply was cut off before this call was complete. Make "
                    "smaller changes per call."
                )
            elif halt is not None or self.run.stop.is_set():
                halt = halt or "stopped"
                refusal = "Not run: the request was stopped."
            elif self.totals.steps >= self.settings.assistant_max_steps:
                halt = "steps"
                refusal = "Not run: this request has used its budget of tool calls."
            elif time.monotonic() > self.deadline:
                halt = "time"
                refusal = "Not run: this request has run out of time."
            if refusal is not None:
                results.append(ToolResult(call.id, call.name, refusal, is_error=True))
                continue
            results.append(await self._run_call(call))
        return results, halt

    async def _run_call(self, call: ToolCall) -> ToolResult:
        self.totals.steps += 1
        tool = self.by_name.get(call.name)
        shown = call.arguments if call.arguments is not None else call.raw
        step: dict[str, Any] = {
            "callId": call.id,
            "tool": call.name,
            "title": tool.title if tool is not None else call.name,
            "input": _shorten(json.dumps(shown, ensure_ascii=False, indent=1, default=str)),
        }
        self.run.emit("step_started", step)
        started = time.monotonic()
        ok = False
        version: dict[str, int | None] | None = None
        if tool is None:
            text = (
                f"Error (unknown_tool): There is no tool called {call.name!r} here.\n"
                f"What to do: Use one of: {', '.join(self.by_name)}."
            )
        elif call.arguments is None:
            text = (
                "Error (invalid_json): The arguments were not valid JSON, so nothing was run.\n"
                "What to do: Send the arguments again as one JSON object.\n"
                + json.dumps({"INVALID_JSON": call.raw})
            )
        elif call.name in ON_THIS_DASHBOARD and str(call.arguments.get("id", "")).strip() != str(
            self.dashboard_id
        ):
            text = (
                "Error (forbidden): In the editor you can only change the open dashboard, id "
                f"{self.dashboard_id}.\nWhat to do: Call {call.name} again with that id."
            )
        else:
            arguments = call.arguments
            try:
                if call.name in ON_THIS_DASHBOARD:
                    before, result = await self._db(
                        lambda db: (self._version(db), tools.call(self.ctx, call.name, arguments))
                    )
                    if isinstance(result, dict) and result.get("version") != before:
                        version = {"before": before, "after": result.get("version")}
                else:
                    result = await anyio.to_thread.run_sync(
                        tools.call, self.ctx, call.name, arguments
                    )
                ok = True
                text = render(result, self.settings.assistant_max_rows)
            except tools.ToolFailure as failure:
                text = failure.text
            except Exception:
                log.exception("assistant: tool %s failed", call.name)
                text = (
                    "Error (internal): The tool failed unexpectedly. Tell the person; do not retry."
                )
        step.update(ok=ok, output=_shorten(text), ms=int((time.monotonic() - started) * 1000))
        if tool is not None and tool.writes and ok:
            step["writes"] = True
        if version is not None:
            step["version"] = version
        await self._entry("step", step)
        return ToolResult(call.id, call.name, text, is_error=not ok)

    async def _finish(self, before: int | None) -> None:
        totals = self.totals
        ms = int((time.monotonic() - self.started) * 1000)
        try:
            after = await self._db(self._version)
            option = self.option
            summary = {
                "outcome": self.outcome,
                "provider": self.provider.name,
                # Whose key: own (with the key's id and name), workspace or ollama.
                "source": option.source if option else None,
                "keyId": str(option.key_id) if option and option.key_id else None,
                "keyLabel": option.key_label if option else None,
                "model": totals.model or self.provider.model,
                "modelCalls": totals.model_calls,
                "steps": totals.steps,
                "inputTokens": totals.input_tokens,
                "cachedTokens": totals.cached_tokens,
                "outputTokens": totals.output_tokens,
                "ms": ms,
                "versionBefore": before,
                "versionAfter": after,
            }
            await self._entry("done", summary)

            def record(db: Session) -> None:
                store.add_usage(db, self.conversation_id, totals.input_tokens, totals.output_tokens)
                if option is not None and totals.model_calls:
                    db.add(
                        AssistantUsage(
                            user_id=self.ctx.principal.user_id,
                            key_id=option.key_id,
                            key_label=option.key_label,
                            source=option.source,
                            provider=self.provider.name,
                            model=str(summary["model"])[:200],
                            input_tokens=totals.input_tokens,
                            cached_tokens=totals.cached_tokens,
                            output_tokens=totals.output_tokens,
                        )
                    )
                    key = db.get(UserApiKey, option.key_id) if option.key_id else None
                    if key is not None:
                        key.last_used_at = datetime.now(UTC)
                audit.record(
                    db,
                    action="assistant.request",
                    actor=self.ctx.principal,
                    target_type="dashboard",
                    target_id=str(self.dashboard_id),
                    detail={k: v for k, v in summary.items() if k != "ms"}
                    | {"seconds": ms // 1000},
                )
                db.commit()

            await self._db(record)
            log.info(
                "assistant request: user=%s dashboard=%s provider=%s model=%s outcome=%s "
                "model_calls=%d steps=%d input_tokens=%d cached_tokens=%d output_tokens=%d ms=%d",
                self.ctx.principal.user_id,
                self.dashboard_id,
                self.provider.name,
                summary["model"],
                self.outcome,
                totals.model_calls,
                totals.steps,
                totals.input_tokens,
                totals.cached_tokens,
                totals.output_tokens,
                ms,
            )
        except Exception:
            log.exception("assistant: could not record the end of a request")
        finally:
            self.run.emit("done", {"outcome": self.outcome})
