"""Test fixtures. DB tests use a throwaway database on the Compose Postgres."""

from __future__ import annotations

import base64
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from opsis.auth.cookies import CSRF_COOKIE, CSRF_HEADER
from opsis.db.migrate import upgrade_to_head
from opsis.db.models import Base
from opsis.main import create_app
from opsis.settings import Settings, build_settings

TEST_DB = "opsis_test"
TEST_KEY = base64.urlsafe_b64encode(b"t" * 32).decode()  # a valid, fixed Fernet key
assert Fernet(TEST_KEY)

# A valid server-mode configuration. Tests override single values from here.
SERVER: dict[str, Any] = {
    "deploy_mode": "server",
    "auth_mode": "full",
    "mcp_auth": "token",
    "job_runner": "worker",
    "compose_profiles": "server",
    "bind_address": "192.168.1.50",
    "public_base_url": "https://bi.test/opsis",
    "trusted_proxies": "192.168.1.10",
    "postgres_password": "Zq7-fake-test-value-41",
    "encryption_key": TEST_KEY,
    "admin_email": "admin@example.test",
    "google_client_id": "client-123",
    "google_client_secret": "test-client-key-0099",
}


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "postgres_db": TEST_DB,
        "static_dir": Path("/nonexistent-static"),
        "branding_file": Path("/nonexistent-branding.json"),
        # No background runner thread in tests: jobs are run explicitly.
        "job_runner": "worker",
        "compose_profiles": None,
        "encryption_key": TEST_KEY,
    }
    values.update(overrides)
    settings = build_settings(**values)
    # Validation ran against the values above (server-mode tests pass a strong
    # password). The connection itself must use the real test database password.
    real = SecretStr(os.environ.get("POSTGRES_PASSWORD", "opsis"))
    return settings.model_copy(update={"postgres_password": real})


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
def client_for(db: Session, tmp_path: Path) -> Iterator[Callable[..., TestClient]]:
    """Build a TestClient for an app with the given settings overrides.

    The client already holds a CSRF cookie and sends the matching header, the
    way the frontend does. Paths are relative to the app's base path.
    """
    clients: list[TestClient] = []

    def factory(*, csrf: bool = True, **overrides: Any) -> TestClient:
        overrides.setdefault("data_dir", tmp_path / "data")
        settings = make_settings(**overrides)
        scheme = "https" if settings.is_https else "http"
        client = TestClient(
            create_app(settings), base_url=f"{scheme}://testserver{settings.base_path}"
        )
        client.__enter__()
        clients.append(client)
        if csrf:
            client.get("/api/config")
            client.headers[CSRF_HEADER] = client.cookies[CSRF_COOKIE]
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)
