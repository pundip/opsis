"""The migration to the workspace model (ADR-031). Its data step writes the rules out
rather than importing them, so that it never changes with the app's code: here it is
checked against the app's own conversion and the planned rules, and run on a
database that holds v1 models."""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Collection, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from opsis.db.migrate import alembic_config
from opsis.db.models import Connection, Dataset, SemanticModel
from opsis.semantic import workspace as ws
from opsis.services import demo
from opsis.settings import Settings
from tests.test_spec_drafts import _planned_model_migration, problems

MIGRATION = (
    Path(__file__).resolve().parents[1] / "opsis/db/migrations/versions/0014_workspace_model.py"
)
SCRATCH_DB = "opsis_migrate_test"


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location("migration_0014", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migrated_document = _migration().migrated_document

TO_DONORS = {"to": "donors", "on": {"donor_id": "id"}}
CASES: dict[str, dict[str, dict[str, Any]]] = {
    "demo": demo.MODELS,
    "turned round": {
        "a": {"joins": [{"to": "b", "on": {"x": "y"}, "relationship": "one_to_many"}]},
        "b": {},
    },
    "both sides": {
        "a": {"joins": [{"to": "b", "on": {"x": "y"}, "relationship": "one_to_many"}]},
        "b": {"joins": [{"to": "a", "on": {"y": "x"}}]},
    },
    "inner one to one": {
        "a": {
            "joins": [{"to": "b", "on": {"x": "y"}, "type": "inner", "relationship": "one_to_one"}]
        },
        "b": {},
    },
    "two between the same tables": {
        "gifts": {
            "dataset": "gifts",
            "label": "Gifts",
            "joins": [TO_DONORS, {"to": "donors", "on": {"referrer_id": "id"}}],
        },
        "donors": {"measures": {"donor_count": {"agg": "count"}}},
    },
    "composite keys": {
        "lines": {"joins": [{"to": "orders", "on": {"order_no": "no", "branch": "branch"}}]},
        "orders": {},
    },
}


def rows_of(models: dict[str, dict[str, Any]], starters: Collection[str] = ()) -> list[Any]:
    """The rows the data step reads: (dataset, definition, is starter), by name."""
    return [(name, models[name], name in starters) for name in sorted(models)]


@pytest.mark.parametrize("case", list(CASES))
def test_the_migration_gives_the_apps_conversion_and_the_planned_rules(case: str) -> None:
    models = CASES[case]
    migrated = migrated_document(rows_of(models))
    assert migrated == ws.from_v1_models(models)
    planned = _planned_model_migration(models)
    assert migrated["relationships"] == planned["relationships"]
    assert migrated["tables"] == {
        name: {k: v for k, v in entry.items() if k != "dataset"}
        for name, entry in planned["tables"].items()
    }
    assert problems("workspace-model.schema.json", migrated) == []


def test_starter_models_stay_implicit_but_their_joins_are_kept() -> None:
    models = CASES["two between the same tables"]
    migrated = migrated_document(rows_of(models, starters={"donors"}))
    assert migrated == ws.from_v1_models(models, edited={"gifts"})
    assert set(migrated["tables"]) == {"gifts"}
    assert list(migrated["relationships"]) == ["gifts__donors", "gifts__donors_2"]
    assert migrated["relationships"]["gifts__donors_2"]["from"]["columns"] == ["referrer_id"]
    assert migrated_document([]) is None


@pytest.fixture
def scratch(migrated_db: Settings) -> Iterator[URL]:
    """A database of its own, at the revision before the workspace model."""
    server = create_engine(
        migrated_db.sqlalchemy_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    with server.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))
    url = migrated_db.sqlalchemy_url.set(database=SCRATCH_DB)
    command.upgrade(alembic_config(url), "0013")
    yield url
    with server.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
    server.dispose()


def test_the_data_step_makes_version_1_and_keeps_the_v1_tables(scratch: URL) -> None:
    engine = create_engine(scratch)
    # A new workspace: nothing to migrate, the model starts empty at version 0.
    command.upgrade(alembic_config(scratch), "0014")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM workspace_model")).scalar() == 0
    command.downgrade(alembic_config(scratch), "0013")

    models = CASES["two between the same tables"]
    with Session(engine) as db:
        connection = Connection(id=uuid.uuid4(), name="src", connector_id="postgres", config={})
        db.add(connection)
        db.flush()
        for name in models:
            dataset = Dataset(
                id=uuid.uuid4(), name=name, label=name.title(), connection_id=connection.id,
                stream=f"src.{name}", refresh_mode="full", primary_key=[],
            )  # fmt: skip
            db.add(dataset)
            db.flush()
            db.add(
                SemanticModel(
                    dataset_id=dataset.id, definition=models[name], is_starter=name == "donors"
                )
            )
        db.commit()

    command.upgrade(alembic_config(scratch), "0014")
    with engine.connect() as conn:
        row = conn.execute(text("SELECT definition, layout, version FROM workspace_model")).one()
        assert row.version == 1 and row.layout == {}
        assert row.definition == migrated_document(rows_of(models, starters={"donors"}))
        history = conn.execute(
            text("SELECT version, author_type, message, definition FROM workspace_model_versions")
        ).all()
        assert [(h.version, h.author_type, h.message) for h in history] == [
            (1, "system", "Migrated from the v1 models")
        ]
        assert history[0].definition == row.definition
        # The v1 models are kept as they were.
        assert conn.execute(text("SELECT count(*) FROM semantic_models")).scalar() == 2
    engine.dispose()
