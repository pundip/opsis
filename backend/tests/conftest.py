"""Test fixtures. DB tests use a throwaway database on the Compose Postgres."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from opsis.db.migrate import upgrade_to_head
from opsis.db.models import Base
from opsis.main import create_app
from opsis.settings import Settings, build_settings

TEST_DB = "opsis_test"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "postgres_db": TEST_DB,
        "static_dir": Path("/nonexistent-static"),
        "branding_file": Path("/nonexistent-branding.json"),
    }
    values.update(overrides)
    return build_settings(**values)


@pytest.fixture(scope="session")
def migrated_db() -> Settings:
    """Create an empty test database and run every migration against it."""
    settings = make_settings()
    admin = create_engine(
        settings.sqlalchemy_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
    admin.dispose()
    upgrade_to_head(settings.sqlalchemy_url)
    return settings


@pytest.fixture
def db(migrated_db: Settings) -> Iterator[Session]:
    engine = create_engine(migrated_db.sqlalchemy_url)
    with engine.begin() as conn:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    with sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    engine.dispose()


@pytest.fixture
def client_for(db: Session) -> Iterator[Callable[..., TestClient]]:
    """Build a TestClient for an app with the given settings overrides."""
    clients: list[TestClient] = []

    def factory(**overrides: Any) -> TestClient:
        client = TestClient(create_app(make_settings(**overrides)))
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)
