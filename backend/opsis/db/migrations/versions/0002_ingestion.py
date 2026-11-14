"""Data ingestion: connections, datasets, snapshots.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
NOW = sa.func.now()


def upgrade() -> None:
    op.create_table(
        "connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("connector_id", sa.String(50), nullable=False),
        sa.Column(
            "config", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("secrets", sa.Text(), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("last_test_message", sa.Text(), nullable=True),
        sa.Column("last_tested_at", TS, nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_connections"),
        sa.UniqueConstraint("name", name="uq_connections_name"),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_connections_created_by_users", ondelete="SET NULL"
        ),
    )

    op.create_table(
        "datasets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("stream", sa.String(300), nullable=False),
        sa.Column("refresh_mode", sa.String(20), nullable=False),
        sa.Column("cursor_field", sa.String(200), nullable=True),
        sa.Column(
            "primary_key", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("columns", postgresql.JSONB(), nullable=True),
        sa.Column("state", postgresql.JSONB(), nullable=True),
        sa.Column("current_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_datasets"),
        sa.UniqueConstraint("name", name="uq_datasets_name"),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name="fk_datasets_connection_id_connections",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_datasets_created_by_users", ondelete="SET NULL"
        ),
    )
    op.create_index("ix_datasets_connection_id", "datasets", ["connection_id"])

    op.create_table(
        "snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("columns", postgresql.JSONB(), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_snapshots"),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name="fk_snapshots_dataset_id_datasets",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_snapshots_dataset_id", "snapshots", ["dataset_id"])


def downgrade() -> None:
    op.drop_table("snapshots")
    op.drop_table("datasets")
    op.drop_table("connections")
