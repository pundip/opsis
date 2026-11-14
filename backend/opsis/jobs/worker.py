"""Worker loop for JOB_RUNNER=worker. Job handlers register here from M2 onward."""

from __future__ import annotations

import logging
import signal
import socket
import threading
from collections.abc import Callable
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from opsis.db.models import Job
from opsis.jobs import queue
from opsis.settings import Settings

log = logging.getLogger("opsis.worker")

Handler = Callable[[Session, Job], dict[str, Any] | None]
HANDLERS: dict[str, Handler] = {}
IDLE_SLEEP_S = 2.0


def run_one(db: Session, worker_id: str) -> bool:
    """Claim and run a single job. Returns False when the queue is empty."""
    job = queue.claim_next(db, worker_id)
    if job is None:
        return False
    handler = HANDLERS.get(job.type)
    if handler is None:
        queue.finish(db, job, error=f"No handler is registered for job type {job.type!r}.")
        return True
    try:
        result = handler(db, job)
    except Exception as exc:  # a failing job must never stop the worker
        db.rollback()
        log.exception("Job %s (%s) failed", job.id, job.type)
        queue.finish(db, job, error=f"{type(exc).__name__}: {exc}")
    else:
        queue.finish(db, job, result=result)
    return True


def run_forever(settings: Settings) -> None:
    engine = create_engine(settings.sqlalchemy_url, pool_pre_ping=True)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    worker_id = f"{socket.gethostname()}"
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log.info("Worker %s started", worker_id)
    while not stop.is_set():
        with session_factory() as db:
            worked = run_one(db, worker_id)
        if not worked:
            stop.wait(IDLE_SLEEP_S)
    engine.dispose()
    log.info("Worker %s stopped", worker_id)
