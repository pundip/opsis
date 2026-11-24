"""Job execution shared by both runners.

JOB_RUNNER=worker: `run_forever` is the worker container's main loop.
JOB_RUNNER=inline: `InlineRunner` runs the same loop in a thread inside the api.
Either way one job runs at a time per process, through `run_one`, and the same
loop runs the scheduler (services/schedules.py) about every 30 seconds.
"""

from __future__ import annotations

import logging
import signal
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from opsis.db.models import Job
from opsis.errors import JobError
from opsis.jobs import queue
from opsis.settings import Settings
from opsis.storage import snapshots

log = logging.getLogger("opsis.jobs")
TICK_SECONDS = 30.0

# A handler does the work and returns the job result. It must not commit its
# final writes: run_one commits them together with the job status.
Handler = Callable[[Session, Job, Settings], dict[str, Any] | None]
# Runs after that commit, for cleanup that must not affect the job outcome.
AfterCommit = Callable[[Session, Job, Settings], None]

HANDLERS: dict[str, Handler] = {}
AFTER_COMMIT: dict[str, AfterCommit] = {}


def register_handlers() -> None:
    """Register every job type. Imported lazily to avoid import cycles."""
    from opsis.services import backups, demo, maintenance, refresh, schedules, subscriptions
    from opsis.services.datasets import REFRESH_JOB

    HANDLERS.setdefault(REFRESH_JOB, refresh.run_refresh)
    AFTER_COMMIT.setdefault(REFRESH_JOB, refresh.prune_snapshots)
    HANDLERS.setdefault(schedules.BACKUP_JOB, backups.run_backup)
    HANDLERS.setdefault(schedules.CLEANUP_JOB, maintenance.run_cleanup)
    HANDLERS.setdefault(schedules.SNAPSHOT_JOB, subscriptions.run_snapshot)
    HANDLERS.setdefault(demo.DEMO_JOB, demo.run_demo)


def run_one(db: Session, worker_id: str, settings: Settings) -> bool:
    """Claim and run a single job. Returns False when the queue is empty."""
    job = queue.claim_next(db, worker_id)
    if job is None:
        return False
    handler = HANDLERS.get(job.type)
    if handler is None:
        queue.finish(db, job, error=f"No handler is registered for job type {job.type!r}.")
        return True
    try:
        result = handler(db, job, settings)
        queue.finish(db, job, result=result)  # one commit: handler writes + job status
    except JobError as exc:
        db.rollback()
        queue.finish(db, job, error=str(exc))
        return True
    except Exception as exc:  # a failing job must never stop the runner
        db.rollback()
        log.exception("Job %s (%s) failed", job.id, job.type)
        queue.finish(db, job, error=f"Unexpected error ({type(exc).__name__}): {exc}")
        return True
    after = AFTER_COMMIT.get(job.type)
    if after is not None:
        try:
            after(db, job, settings)
        except Exception:
            db.rollback()
            log.exception("Cleanup after job %s (%s) failed", job.id, job.type)
    return True


def _tick(session_factory: sessionmaker[Session], settings: Settings) -> None:
    from opsis.services import schedules

    try:
        with session_factory() as db:
            schedules.tick(db, settings)
    except Exception:  # a broken schedule must never stop the runner
        log.exception("Scheduler error; trying again later")


def _loop(settings: Settings, worker_id: str, stop: threading.Event, idle_seconds: float) -> None:
    from opsis.services import schedules

    engine = create_engine(settings.sqlalchemy_url, pool_pre_ping=True)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    register_handlers()
    try:
        with session_factory() as db:
            stale = queue.fail_stale(db)
            schedules.ensure_system(db, settings)
        if stale:
            log.warning("Marked %d interrupted job(s) as failed", stale)
        snapshots.sweep_temp(settings)
        log.info("Job runner %s started", worker_id)
        last_tick = 0.0
        while not stop.is_set():
            if time.monotonic() - last_tick >= TICK_SECONDS:
                last_tick = time.monotonic()
                _tick(session_factory, settings)
            try:
                with session_factory() as db:
                    worked = run_one(db, worker_id, settings)
            except Exception:  # for example the database restarting
                log.exception("Job runner error; retrying")
                worked = False
            if not worked:
                stop.wait(idle_seconds)
    finally:
        engine.dispose()
        log.info("Job runner %s stopped", worker_id)


def run_forever(settings: Settings) -> None:
    """Main loop of the worker container."""
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    _loop(settings, f"worker:{socket.gethostname()}", stop, idle_seconds=2.0)


class InlineRunner:
    """Runs jobs in a background thread of the api process (JOB_RUNNER=inline)."""

    def __init__(self, settings: Settings) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=_loop,
            args=(settings, f"inline:{socket.gethostname()}", self._stop, 1.0),
            name="opsis-inline-runner",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
