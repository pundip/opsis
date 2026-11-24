"""Login protection: lockouts with backoff, counted in Postgres.

Failures are counted per key: the email being tried, the client address, or a
share link. Once a key reaches its limit within the window, it is locked for
one minute, then two, four and so on up to an hour, for each further failure.
A success clears the email key. Counting in Postgres means the limits hold
across restarts and across processes.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from opsis.db.models import LoginAttempt
from opsis.errors import ApiError

WINDOW = timedelta(minutes=15)
MAX_LOCK = timedelta(hours=1)
# "forgot" counts every password reset request, not only failures.
LIMITS = {"email": 5, "ip": 30, "link": 5, "forgot": 3, "forgotip": 10}


def _limit(key: str) -> int:
    return LIMITS.get(key.partition(":")[0], 5)


def check(db: Session, keys: list[str]) -> None:
    """Raise 429 while any of `keys` is locked."""
    now = datetime.now(UTC)
    rows = db.scalars(select(LoginAttempt).where(LoginAttempt.key.in_(keys))).all()
    until = max(
        (r.locked_until for r in rows if r.locked_until and r.locked_until > now), default=None
    )
    if until is not None:
        minutes = max(1, math.ceil((until - now).total_seconds() / 60))
        raise ApiError(
            429,
            "too_many_attempts",
            f"Too many failed attempts. Try again in {minutes} "
            f"minute{'s' if minutes != 1 else ''}.",
        )


def failed(db: Session, keys: list[str]) -> None:
    """Count a failure against each key. The caller commits."""
    now = datetime.now(UTC)
    for key in keys:
        db.execute(
            insert(LoginAttempt)
            .values(key=key, failures=0, first_failure_at=now)
            .on_conflict_do_nothing(index_elements=["key"])
        )
        row = db.scalar(select(LoginAttempt).where(LoginAttempt.key == key).with_for_update())
        assert row is not None
        locked = row.locked_until is not None and row.locked_until > now
        if not locked and now - row.first_failure_at > WINDOW:
            row.failures = 0
            row.first_failure_at = now
        row.failures += 1
        over = row.failures - _limit(key)
        if over >= 0:
            row.locked_until = now + min(timedelta(minutes=2**over), MAX_LOCK)


def succeeded(db: Session, keys: list[str]) -> None:
    """Forget the failures of these keys. The caller commits."""
    db.execute(delete(LoginAttempt).where(LoginAttempt.key.in_(keys)))
