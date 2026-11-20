"""Requests to the assistant that are in progress in this process.

A request runs as a task of its own on the event loop, not inside the HTTP
request that started it: closing the panel or losing the connection does not
stop it halfway through a change. The person stops it with the Stop button.

Like the live-update hub, this assumes one api process (see services/events.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Coroutine
from typing import Any

from opsis.errors import ApiError

log = logging.getLogger("opsis.assistant")

Event = tuple[str, dict[str, Any]]


class Run:
    def __init__(self, conversation_id: uuid.UUID) -> None:
        self.conversation_id = conversation_id
        self.stop = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self._listeners: set[asyncio.Queue[Event]] = set()

    @property
    def running(self) -> bool:
        return self.task is None or not self.task.done()

    def listen(self) -> asyncio.Queue[Event]:
        # Unbounded: a request is bounded by its budgets, and a listener that
        # missed events would show a garbled reply.
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._listeners.add(queue)
        return queue

    def unlisten(self, queue: asyncio.Queue[Event]) -> None:
        self._listeners.discard(queue)

    def emit(self, event: str, data: dict[str, Any]) -> None:
        for queue in list(self._listeners):
            queue.put_nowait((event, data))


class Runs:
    def __init__(self) -> None:
        self._runs: dict[uuid.UUID, Run] = {}

    def get(self, conversation_id: uuid.UUID) -> Run | None:
        run = self._runs.get(conversation_id)
        return run if run is not None and run.running else None

    def begin(self, conversation_id: uuid.UUID) -> Run:
        """Reserve the conversation for a new request. Call on the event loop."""
        if self.get(conversation_id) is not None:
            raise ApiError(
                409,
                "assistant_busy",
                "The assistant is still working on your last request. Wait for it, or stop it.",
            )
        run = Run(conversation_id)
        self._runs[conversation_id] = run
        return run

    def launch(self, run: Run, work: Coroutine[Any, Any, None]) -> None:
        run.task = asyncio.get_running_loop().create_task(work)

        def forget(_: asyncio.Task[None]) -> None:
            if self._runs.get(run.conversation_id) is run:
                del self._runs[run.conversation_id]

        run.task.add_done_callback(forget)

    def abandon(self, run: Run) -> None:
        """Release a reservation whose request never started."""
        if self._runs.get(run.conversation_id) is run and run.task is None:
            del self._runs[run.conversation_id]

    async def shutdown(self, grace_seconds: float = 10.0) -> None:
        """Ask every request to stop, then cancel those that do not in time."""
        tasks = [run.task for run in self._runs.values() if run.task is not None]
        for run in self._runs.values():
            run.stop.set()
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if pending:
            log.warning("assistant: cancelled %d request(s) at shutdown", len(pending))
