"""Server-sent events that survive reverse proxies (brief 3.1).

Sends `X-Accel-Buffering: no` so nginx does not buffer, and a comment line
every SSE_KEEPALIVE_SECONDS so idle connections are not closed by the proxy.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi.responses import StreamingResponse

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def format_event(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def with_keepalive(
    events: AsyncIterator[tuple[str, Any]], keepalive_seconds: float
) -> AsyncIterator[str]:
    """Yield formatted events, inserting ': keep-alive' comments while idle."""
    iterator = events.__aiter__()
    pending: asyncio.Task[tuple[str, Any]] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=keepalive_seconds)
            if not done:
                yield ": keep-alive\n\n"
                continue
            try:
                event, data = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield format_event(event, data)
    finally:
        if pending is not None:
            pending.cancel()


def sse_response(
    events: AsyncIterator[tuple[str, Any]], keepalive_seconds: float
) -> StreamingResponse:
    return StreamingResponse(
        with_keepalive(events, keepalive_seconds),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
