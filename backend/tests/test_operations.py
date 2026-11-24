"""Schedules, backups and the clean-up job (M8)."""

from __future__ import annotations

import json
import tarfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from opsis.db.models import (
    Dashboard,
    Dataset,
    Job,
    LoginAttempt,
    Schedule,
    ShareLink,
    User,
    UserLink,
)
from opsis.db.models import Session as SessionRow
from opsis.errors import ApiError
from opsis.services import backups, maintenance, schedules
from tests.conftest import make_settings
from tests.test_auth import SIGN_IN, admin_client, person
from tests.test_ingestion import connect, make_dataset, ok, run_jobs, settings_of, upload

ClientFor = Callable[..., TestClient]
CSV = b"id,fund,amount\n1,Library,100\n2,Sport,250\n"
NOW = datetime(2026, 11, 24, 0, 0, tzinfo=UTC)  # 11:00 in Melbourne


def newest_migration() -> str:
    versions = Path(__file__).parents[1] / "opsis" / "db" / "migrations" / "versions"
    return max(p.name.split("_")[0] for p in versions.glob("[0-9]*_*.py"))


def csv_dataset(client: TestClient, name: str = "gifts_csv") -> dict[str, Any]:
    uploaded = upload(client, "gifts.csv", CSV)
    connection = connect(client, "file", {"file": uploaded["file"]}, name=f"{name} file")
    dataset = make_dataset(client, connection, "gifts", name=name)
    run_jobs(client)
    return dataset


# ---- schedules ---------------------------------------------------------------------------


def test_cron_expressions_are_checked_and_follow_the_workspace_clock() -> None:
    settings = make_settings()
    for preset in schedules.PRESETS:
        assert schedules.check_cron(preset["cron"]) == preset["cron"]
    assert schedules.check_cron("  0   6 * *  * ") == "0 6 * * *"
    for bad, says in (
        ("* * *", "five fields"),
        ("0 25 * * *", "five fields"),
        ("*/5 * * * *", "too often"),
    ):
        with pytest.raises(ApiError) as info:
            schedules.check_cron(bad)
        assert info.value.status == 422 and says in info.value.message
        assert info.value.details[0]["path"] == "/cron"
    # 06:00 in Melbourne, every day, across the end of daylight saving (5 April 2026).
    at = datetime(2026, 4, 3, 12, 0, tzinfo=UTC)
    seen = []
    for _ in range(4):
        at = schedules.next_after(settings, "0 6 * * *", at)
        seen.append(at)
    local = [t.astimezone(schedules._zone(settings)) for t in seen]
    assert [t.hour for t in local] == [6, 6, 6, 6]
    # Clocks go back at 03:00 on 5 April, so 06:00 that day is already standard time.
    assert [t.utcoffset() for t in local] == [timedelta(hours=11)] + [timedelta(hours=10)] * 3
    assert schedules.next_after(settings, "0 6 * * *", NOW) == datetime(
        2026, 11, 24, 19, 0, tzinfo=UTC
    )


def test_dataset_schedules_through_the_api(client_for: ClientFor) -> None:
    client = client_for(feature_scheduled_refresh=True)
    dataset = csv_dataset(client)
    presets = ok(client.get("/api/schedules/presets"))
    assert presets["timezone"] == "Australia/Melbourne" and len(presets["presets"]) == 6
    preview = ok(client.post("/api/schedules/preview", json={"cron": "0 7 * * 1-5"}))
    assert len(preview["next"]) == 3 and all("T07:00:00+1" in t for t in preview["next"])
    bad = client.post("/api/schedules/preview", json={"cron": "every day"})
    assert bad.status_code == 422 and bad.json()["error"]["details"][0]["path"] == "/cron"

    saved = ok(client.put(f"/api/datasets/{dataset['id']}/schedule", json={"cron": "0 6 * * *"}))[
        "schedule"
    ]
    assert (
        saved["cron"] == "0 6 * * *"
        and saved["preset"] == "daily"
        and "T06:00:00+11:00" in saved["nextRunAt"]
    )
    assert ok(client.get(f"/api/datasets/{dataset['id']}"))["schedule"]["cron"] == "0 6 * * *"
    assert (
        ok(client.put(f"/api/datasets/{dataset['id']}/schedule", json={"cron": None}))["schedule"]
        is None
    )
    assert ok(client.get(f"/api/datasets/{dataset['id']}"))["schedule"] is None

    # With the flag off the routes do not exist, and datasets carry no schedule.
    off = client_for()
    assert off.get("/api/schedules/presets").status_code == 404
    assert (
        off.put(f"/api/datasets/{dataset['id']}/schedule", json={"cron": "0 6 * * *"}).status_code
        == 404
    )
    assert "schedule" not in ok(off.get(f"/api/datasets/{dataset['id']}"))


def test_the_scheduler_queues_due_jobs_once(client_for: ClientFor, db: Session) -> None:
    client = client_for(feature_scheduled_refresh=True)
    dataset = csv_dataset(client)
    settings = settings_of(client)
    dataset_id = uuid.UUID(dataset["id"])
    row = Schedule(key=schedules.refresh_key(dataset_id), kind="refresh", target_id=dataset_id,
                   cron="0 6 * * *", next_run_at=NOW - timedelta(days=3))  # fmt: skip
    db.add(row)
    db.commit()
    refreshes = (
        select(func.count())
        .select_from(Job)
        .where(Job.type == "dataset.refresh", Job.status == "queued")
    )
    assert schedules.tick(db, settings, NOW) == 1  # three missed days: one run, not three
    db.refresh(row)
    assert db.scalar(refreshes) == 1
    assert row.last_run_at == NOW and row.next_run_at == datetime(2026, 11, 24, 19, 0, tzinfo=UTC)
    job = db.scalars(select(Job).where(Job.type == "dataset.refresh", Job.status == "queued")).one()
    assert job.payload == {"dataset_id": str(dataset_id), "scheduled": True}
    assert schedules.tick(db, settings, NOW) == 0  # not due again yet
    # Due again while the first is still queued: no second refresh of the same dataset.
    row.next_run_at = NOW
    db.commit()
    schedules.tick(db, settings, NOW + timedelta(minutes=1))
    assert db.scalar(refreshes) == 1
    run_jobs(client)
    # With the flag off, a schedule waits without running.
    row.next_run_at = NOW
    db.commit()
    off = make_settings(data_dir=settings.data_dir)
    assert schedules.tick(db, off, NOW) == 0
    # A deleted dataset's schedule goes with it.
    ok(client.delete(f"/api/datasets/{dataset['id']}"), 204)
    assert db.scalar(select(Schedule).where(Schedule.key == row.key)) is None
    stray = Schedule(
        key="refresh:gone",
        kind="refresh",
        target_id=uuid.uuid4(),
        cron="0 6 * * *",
        next_run_at=NOW,
    )
    db.add(stray)
    db.commit()
    schedules.tick(db, settings, NOW)
    assert db.scalar(select(Schedule).where(Schedule.key == "refresh:gone")) is None


def test_system_schedules_follow_the_settings(db: Session) -> None:
    schedules.ensure_system(db, make_settings())
    assert db.scalars(select(Schedule.key)).all() == ["cleanup"]
    schedules.ensure_system(db, make_settings(feature_backups=True, backup_schedule="0 1 * * *"))
    backup = db.scalar(select(Schedule).where(Schedule.key == "backup"))
    assert backup is not None and backup.cron == "0 1 * * *"
    schedules.ensure_system(db, make_settings())
    assert db.scalar(select(Schedule).where(Schedule.key == "backup")) is None


# ---- the clean-up ------------------------------------------------------------------------


def test_the_clean_up_removes_what_has_ended(db: Session) -> None:
    user = User(email="c@example.test", display_name="C", role="viewer")
    db.add(user)
    dashboard = Dashboard(title="D", owner_id=None, version=0)
    db.add(dashboard)
    db.flush()
    old, recent = NOW - timedelta(days=100), NOW - timedelta(days=1)
    db.add_all([
        SessionRow(id="ended", user_id=user.id, last_seen_at=old, expires_at=old + timedelta(days=1)),
        SessionRow(id="active", user_id=user.id, last_seen_at=NOW, expires_at=NOW + timedelta(days=10)),
        UserLink(user_id=user.id, purpose="invite", token_hash="a" * 64, expires_at=old, used_at=old),
        UserLink(user_id=user.id, purpose="reset", token_hash="b" * 64, expires_at=NOW + timedelta(hours=1)),
        ShareLink(dashboard_id=dashboard.id, token_hash="c" * 64, prefix="c", revoked_at=old),
        ShareLink(dashboard_id=dashboard.id, token_hash="d" * 64, prefix="d"),
        LoginAttempt(key="email:x", failures=3, first_failure_at=old),
        LoginAttempt(key="email:y", failures=3, first_failure_at=recent + timedelta(hours=23)),
        Job(type="dataset.refresh", dedupe_key="dataset:1", status="succeeded", finished_at=old, created_at=old),
        Job(type="dataset.refresh", dedupe_key="dataset:1", status="succeeded", finished_at=old,
            created_at=old + timedelta(hours=1)),  # the latest for its dataset: kept
        Job(type="dataset.refresh", dedupe_key="dataset:2", status="succeeded", finished_at=recent, created_at=recent),
    ])  # fmt: skip
    db.commit()
    counts = maintenance.clean_up(db, make_settings(), NOW)
    db.commit()
    assert counts == {
        "sessions": 1,
        "passwordLinks": 1,
        "shareLinks": 1,
        "signInFailures": 1,
        "jobs": 1,
    }
    assert db.scalars(select(SessionRow.id)).all() == ["active"]
    assert db.scalar(select(func.count()).select_from(Job)) == 2


# ---- backups -----------------------------------------------------------------------------


def test_a_backup_holds_everything_needed_and_restores(
    client_for: ClientFor, db: Session, tmp_path: Path
) -> None:
    client = client_for(backup_dir=tmp_path / "backups")
    dataset = csv_dataset(client)
    settings = settings_of(client)
    made = backups.create(db, settings)
    path = settings.backup_dir / made["file"]
    assert made["datasets"] == 1 and path.stat().st_size == made["bytes"]
    with tarfile.open(path) as tar:
        names = tar.getnames()
        manifest = json.loads(tar.extractfile("manifest.json").read())  # type: ignore[union-attr]
    assert (
        manifest["app"] == "opsis"
        and manifest["format"] == 1
        and manifest["databaseRevision"] == newest_migration()
    )
    assert "metadata.dump" in names
    assert any(n.startswith("uploads/") and n.endswith("gifts.csv") for n in names)
    snapshot = db.scalar(
        select(Dataset.current_snapshot_id).where(Dataset.id == uuid.UUID(dataset["id"]))
    )
    assert f"datasets/{dataset['id']}/{snapshot}/part-00000.parquet" in names
    assert not list(settings.backup_dir.glob(".*partial"))  # written under a temporary name

    # Only the newest BACKUP_RETENTION files are kept. Names carry the time to the second,
    # so the first backup is given an older one: the next must not take its name.
    path = path.rename(settings.backup_dir / "opsis-20260103-020000.tar")
    for day in ("20260101-020000", "20260102-020000"):
        (settings.backup_dir / f"opsis-{day}.tar").write_bytes(b"old")
    kept = backups.create(db, settings.model_copy(update={"backup_retention": 2}))
    assert {b["name"] for b in backups.backups(settings)} == {kept["file"], path.name}
    assert sorted(kept["removed"]) == ["opsis-20260101-020000.tar", "opsis-20260102-020000.tar"]

    # Restore into an empty database and data folder: the same rows, the same files.
    target = "opsis_restore_test"
    admin = create_engine(
        settings.sqlalchemy_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {target} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {target}"))
    admin.dispose()
    elsewhere = settings.model_copy(update={"data_dir": tmp_path / "restored"})
    (elsewhere.data_dir / "datasets").mkdir(parents=True)
    (elsewhere.data_dir / "datasets" / "stale").mkdir()
    done = backups.restore(elsewhere, path, database=target)
    restored = create_engine(settings.sqlalchemy_url.set(database=target))
    with restored.connect() as conn:
        for table in ("datasets", "connections", "snapshots", "users"):
            count = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            assert count == db.execute(text(f"SELECT count(*) FROM {table}")).scalar(), table
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version == newest_migration()
    restored.dispose()
    assert (
        elsewhere.data_dir / "datasets" / dataset["id"] / str(snapshot) / "part-00000.parquet"
    ).is_file()
    assert (
        done["previousDataMovedTo"]
        and (Path(done["previousDataMovedTo"]) / "datasets" / "stale").is_dir()
    )
    with pytest.raises(Exception, match="not an Opsis backup"):
        backups.read_manifest(settings.backup_dir / "opsis-20260102-020000.tar")


def test_admins_back_up_from_the_app(client_for: ClientFor, tmp_path: Path) -> None:
    admin = admin_client(client_for, feature_backups=True, backup_dir=tmp_path / "b")
    listed = ok(admin.get("/api/admin/backups"))
    assert listed["backups"] == [] and listed["retention"] == 7
    assert ok(admin.post("/api/admin/backups"), 202)["type"] == "backup"
    run_jobs(admin)
    assert len(ok(admin.get("/api/admin/backups"))["backups"]) == 1
    editor, _ = person(
        client_for, admin, "ed@example.test", "editor", **SIGN_IN, feature_backups=True
    )
    assert editor.get("/api/admin/backups").status_code == 403
    assert client_for().get("/api/admin/backups").status_code == 404  # flag off
