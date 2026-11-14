"""Alembic environment. The URL comes from opsis.db.migrate or from settings."""

from alembic import context
from sqlalchemy import create_engine

from opsis.db.models import Base
from opsis.settings import load_settings

target_metadata = Base.metadata


def run_migrations_online() -> None:
    url = context.config.attributes.get("url") or load_settings().sqlalchemy_url
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline migrations are not supported.")
run_migrations_online()
