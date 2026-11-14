"""The one error type services raise. REST, MCP and the assistant all translate it."""

from __future__ import annotations

from typing import Any


class JobError(Exception):
    """A job failure whose message is safe and useful to show to the user as is."""


class ApiError(Exception):
    """A failure with an HTTP-style status, a stable code and a message for people.

    `details` is a list of {"path": "/json/pointer", "message": "..."} items so
    that a person or an agent can find exactly what to fix.
    """

    def __init__(
        self, status: int, code: str, message: str, details: list[dict[str, Any]] | None = None
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = details or []
        super().__init__(message)
