"""Schedules: dataset refreshes, emailed dashboards, the backup and the clean-up.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "schedules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("cron", sa.String(100), nullable=False),
        sa.Column("next_run_at", TS, nullable=False),
        sa.Column("last_run_at", TS, nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_schedules_owner_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_schedules_created_by_users", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_schedules"),
        sa.UniqueConstraint("key", name="uq_schedules_key"),
    )
    op.create_index("ix_schedules_next_run_at", "schedules", ["next_run_at"])
    op.create_index("ix_schedules_owner_id", "schedules", ["owner_id"])


def downgrade() -> None:
    op.drop_table("schedules")
