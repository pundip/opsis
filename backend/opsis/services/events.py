"""Live events for open editors: an in-process publish and subscribe hub.

Dashboard writes happen in the api process (REST, MCP and the assistant all
run there), so subscribers and publishers share memory and no broker is
needed. If the api ever runs as several processes, replace the body of
`publish` with Postgres NOTIFY and keep the interface.

Publishers are ordinary threads (FastAPI runs sync endpoints in a pool);
subscribers are async generators on the event loop.
"""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

Event = tuple[str, dict[str, Any]]
QUEUE_SIZE = 100
_Subscriber = tuple[asyncio.AbstractEventLoop, "asyncio.Queue[Event]"]


class EventHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, set[_Subscriber]] = defaultdict(set)

    def publish(self, channel: str, event: str, data: dict[str, Any]) -> None:
        """Send to everyone listening on `channel`. Safe to call from any thread."""
        with self._lock:
            targets = list(self._subscribers.get(channel, ()))
        for loop, queue in targets:
            if not loop.is_closed():
                loop.call_soon_threadsafe(_offer, queue, (event, data))

    async def subscribe(self, channel: str) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_SIZE)
        entry: _Subscriber = (asyncio.get_running_loop(), queue)
        with self._lock:
            self._subscribers[channel].add(entry)
        try:
            while True:
                yield await queue.get()
        finally:
            with self._lock:
                self._subscribers[channel].discard(entry)
                if not self._subscribers[channel]:
                    del self._subscribers[channel]

    def count(self, channel: str) -> int:
        with self._lock:
            return len(self._subscribers.get(channel, ()))


def _offer(queue: asyncio.Queue[Event], item: Event) -> None:
    # A listener that has fallen far behind loses the event; it reloads on the next one.
    if not queue.full():
        queue.put_nowait(item)


def dashboard_channel(dashboard_id: Any) -> str:
    return f"dashboard:{dashboard_id}"


# One hub per process. See the module docstring for why that is enough.
hub = EventHub()
