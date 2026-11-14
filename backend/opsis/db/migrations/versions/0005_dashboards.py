"""Dashboards and their versions.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
NOW = sa.func.now()


def upgrade() -> None:
    op.create_table(
        "dashboards",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("published_version", sa.Integer(), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_dashboards"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_dashboards_owner_id_users", ondelete="SET NULL"
        ),
    )
    op.create_table(
        "dashboard_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dashboard_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec", postgresql.JSON(), nullable=False),
        sa.Column("author_type", sa.String(20), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=True),
        sa.Column("message", sa.String(500), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_dashboard_versions"),
        sa.UniqueConstraint("dashboard_id", "version", name="uq_dashboard_versions_dashboard_id"),
        sa.ForeignKeyConstraint(
            ["dashboard_id"],
            ["dashboards.id"],
            name="fk_dashboard_versions_dashboard_id_dashboards",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            name="fk_dashboard_versions_author_id_users",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_dashboard_versions_dashboard_id", "dashboard_versions", ["dashboard_id"])


def downgrade() -> None:
    op.drop_table("dashboard_versions")
    op.drop_table("dashboards")
