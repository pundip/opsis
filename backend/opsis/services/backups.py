"""Backups (FEATURE_BACKUPS) and restore.

A backup is one file, `opsis-<date>-<time>.tar` in BACKUP_DIR, written under a
temporary name and renamed when complete:

- manifest.json: what is in it, the app version and the database revision;
- metadata.dump: the metadata database (pg_dump, custom format);
- uploads/: the uploaded CSV and Excel files;
- datasets/<id>/<snapshot>/: each dataset's current snapshot, so dashboards work
  straight after a restore, before any source is reached.

The backup runs as a job, and the runner runs one job at a time, so no refresh
can swap a snapshot halfway through: the dump and the files always agree.
Older backups beyond BACKUP_RETENTION are removed.

Not in a backup: ENCRYPTION_KEY (it is in .env; without it the saved connection
passwords in a restored database cannot be read) and snapshots older than the
current one.

Restore (`python -m opsis restore <file> --yes`, with the api and worker
stopped) replaces the metadata database and the data folder's uploads and
datasets with the backup's. The previous ones are moved aside, not deleted.
The api upgrades the restored database to the current version when it starts.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from opsis import __version__
from opsis.db.models import Dataset, Job
from opsis.errors import JobError
from opsis.services.connections import uploads_dir
from opsis.settings import Settings
from opsis.storage import snapshots

log = logging.getLogger("opsis.backups")

FORMAT = 1
PREFIX = "opsis-"
SUFFIX = ".tar"
DUMP_TIMEOUT = 3600


def _pg_env(settings: Settings) -> dict[str, str]:
    return {
        **os.environ,
        "PGHOST": settings.postgres_host,
        "PGPORT": str(settings.postgres_port),
        "PGUSER": settings.postgres_user,
        "PGPASSWORD": settings.postgres_password.get_secret_value(),
    }


def _run(command: list[str], settings: Settings, what: str) -> None:
    try:
        done = subprocess.run(  # noqa: S603  fixed arguments, no shell
            command,
            env=_pg_env(settings),
            capture_output=True,
            text=True,
            timeout=DUMP_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        raise JobError(
            f"{command[0]} is not installed in this image, so {what} cannot run."
        ) from None
    except subprocess.TimeoutExpired:
        raise JobError(f"{what} took longer than an hour and was stopped.") from None
    if done.returncode != 0:
        raise JobError(f"{what} failed: {done.stderr.strip()[-1000:]}")


def _revision(settings: Settings, database: str | None = None) -> str | None:
    engine = create_engine(settings.sqlalchemy_url.set(database=database or settings.postgres_db))
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        return None
    finally:
        engine.dispose()


def backups(settings: Settings) -> list[dict[str, Any]]:
    """The backups in BACKUP_DIR, newest first."""
    folder = settings.backup_dir
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.glob(f"{PREFIX}*{SUFFIX}"), reverse=True):
        stat = path.stat()
        found.append(
            {
                "name": path.name,
                "bytes": stat.st_size,
                "createdAt": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
            }
        )
    return found


def _prune(settings: Settings) -> list[str]:
    removed = []
    for entry in backups(settings)[settings.backup_retention :]:
        (settings.backup_dir / entry["name"]).unlink(missing_ok=True)
        removed.append(entry["name"])
    return removed


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(datetime.now(UTC).timestamp())
    tar.addfile(info, io.BytesIO(data))


def create(db: Session, settings: Settings) -> dict[str, Any]:
    """Write a new backup and prune old ones. Returns what was written."""
    folder = settings.backup_dir
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise JobError(f"BACKUP_DIR {folder} cannot be written: {exc.strerror}.") from None
    stamp = datetime.now(ZoneInfo(settings.workspace_timezone)).strftime("%Y%m%d-%H%M%S")
    target = folder / f"{PREFIX}{stamp}{SUFFIX}"
    partial = folder / f".{target.name}.partial"
    current = db.execute(
        select(Dataset.id, Dataset.current_snapshot_id).where(
            Dataset.current_snapshot_id.is_not(None)
        )
    ).all()
    work = settings.data_dir / "tmp"
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work) as tmp:
        dump = Path(tmp) / "metadata.dump"
        _run(
            ["pg_dump", "--format=custom", "--no-owner", "--no-privileges",
             "--dbname", settings.postgres_db, "--file", str(dump)],
            settings,
            "The database dump",
        )  # fmt: skip
        manifest = {
            "format": FORMAT,
            "app": "opsis",
            "version": __version__,
            "databaseRevision": _revision(settings),
            "createdAt": datetime.now(UTC).isoformat(),
            "timezone": settings.workspace_timezone,
            "datasets": len(current),
        }
        try:
            with tarfile.open(partial, "w") as tar:
                _add_bytes(tar, "manifest.json", json.dumps(manifest, indent=2).encode())
                tar.add(dump, "metadata.dump")
                uploads = uploads_dir(settings)
                if uploads.is_dir():
                    tar.add(uploads, "uploads")
                for dataset_id, snapshot_id in current:
                    if snapshot_id is None:
                        continue
                    folder_path = snapshots.snapshot_dir(settings, dataset_id, snapshot_id)
                    if folder_path.is_dir():
                        tar.add(folder_path, f"datasets/{dataset_id}/{snapshot_id}")
            partial.replace(target)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise JobError(
                f"The backup could not be written to {folder}: {exc.strerror}."
            ) from None
    removed = _prune(settings)
    size = target.stat().st_size
    log.info("Backup written: %s (%d bytes); removed %d old one(s)", target, size, len(removed))
    return {"file": target.name, "bytes": size, "datasets": len(current), "removed": removed}


def run_backup(db: Session, job: Job, settings: Settings) -> dict[str, Any]:
    """Job handler."""
    return create(db, settings)


# ---- restore ----------------------------------------------------------------------------


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        with tarfile.open(path, "r") as tar:
            member = tar.extractfile("manifest.json")
            if member is None:
                raise KeyError("manifest.json")
            manifest: dict[str, Any] = json.loads(member.read())
    except (OSError, KeyError, tarfile.TarError, ValueError):
        raise JobError(f"{path} is not an Opsis backup.") from None
    if manifest.get("app") != "opsis" or manifest.get("format") != FORMAT:
        raise JobError(f"{path} is not an Opsis backup of a format this version can restore.")
    return manifest


def restore(settings: Settings, path: Path, *, database: str | None = None) -> dict[str, Any]:
    """Replace the metadata database and the data folder's contents with a backup's.

    Run with the api and worker stopped. `database` restores into another
    database (the tests use it).
    """
    manifest = read_manifest(path)
    database = database or settings.postgres_db
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    work = settings.data_dir / "tmp"
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work) as tmp:
        unpacked = Path(tmp)
        with tarfile.open(path, "r") as tar:
            tar.extractall(unpacked, filter="data")  # refuses paths outside the folder
        # A clean schema first: tables of a newer version must not survive the restore.
        engine = create_engine(settings.sqlalchemy_url.set(database=database))
        try:
            with engine.begin() as conn:
                conn.execute(text("DROP SCHEMA public CASCADE"))
                conn.execute(text("CREATE SCHEMA public"))
        finally:
            engine.dispose()
        _run(
            ["pg_restore", "--no-owner", "--no-privileges", "--single-transaction",
             "--dbname", database, str(unpacked / "metadata.dump")],
            settings,
            "The database restore",
        )  # fmt: skip
        aside = settings.data_dir / f"before-restore-{stamp}"
        for name, source in (
            ("uploads", unpacked / "uploads"),
            ("datasets", unpacked / "datasets"),
        ):
            live = uploads_dir(settings) if name == "uploads" else settings.data_dir / "datasets"
            if live.exists():
                aside.mkdir(parents=True, exist_ok=True)
                shutil.move(str(live), str(aside / name))
            if source.exists():
                live.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(live))
    return {
        "restored": path.name,
        "backupCreatedAt": manifest.get("createdAt"),
        "databaseRevision": manifest.get("databaseRevision"),
        "previousDataMovedTo": str(aside) if aside.exists() else None,
    }
