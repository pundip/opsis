"""Run Alembic migrations from Python (used by the entrypoint and the tests)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def alembic_config(url: URL) -> Config:
    config = Config(str(ALEMBIC_INI))
    # Passed as an attribute, not an ini option, so special characters in the
    # password are never interpolated or logged.
    config.attributes["url"] = url
    return config


def upgrade_to_head(url: URL) -> None:
    command.upgrade(alembic_config(url), "head")
