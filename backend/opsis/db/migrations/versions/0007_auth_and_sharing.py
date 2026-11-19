"""Sign-in, groups, access lists and share links.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
NOW = sa.func.now()


def _by(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["created_by"], ["users.id"], name=f"fk_{table}_created_by_users", ondelete="SET NULL"
    )


def upgrade() -> None:
    op.add_column(
        "datasets",
        sa.Column("restricted", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_table(
        "groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_groups"),
        sa.UniqueConstraint("name", name="uq_groups_name"),
    )
    op.create_table(
        "group_members",
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("added_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("group_id", "user_id", name="pk_group_members"),
        sa.ForeignKeyConstraint(
            ["group_id"], ["groups.id"], name="fk_group_members_group_id_groups", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_group_members_user_id_users", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_group_members_user_id", "group_members", ["user_id"])
    op.create_table(
        "dashboard_shares",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dashboard_id", sa.Uuid(), nullable=False),
        sa.Column("grantee", sa.String(60), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_dashboard_shares"),
        sa.UniqueConstraint("dashboard_id", "grantee", name="uq_dashboard_shares_dashboard_id"),
        sa.ForeignKeyConstraint(
            ["dashboard_id"],
            ["dashboards.id"],
            name="fk_dashboard_shares_dashboard_id_dashboards",
            ondelete="CASCADE",
        ),
        _by("dashboard_shares"),
    )
    op.create_index("ix_dashboard_shares_dashboard_id", "dashboard_shares", ["dashboard_id"])
    op.create_table(
        "dataset_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("grantee", sa.String(60), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_dataset_grants"),
        sa.UniqueConstraint("dataset_id", "grantee", name="uq_dataset_grants_dataset_id"),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name="fk_dataset_grants_dataset_id_datasets",
            ondelete="CASCADE",
        ),
        _by("dataset_grants"),
    )
    op.create_index("ix_dataset_grants_dataset_id", "dataset_grants", ["dataset_id"])
    op.create_table(
        "share_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dashboard_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("name", sa.String(100), nullable=True),
        sa.Column(
            "attributes",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column("expires_at", TS, nullable=True),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("view_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_viewed_at", TS, nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_share_links"),
        sa.UniqueConstraint("token_hash", name="uq_share_links_token_hash"),
        sa.ForeignKeyConstraint(
            ["dashboard_id"],
            ["dashboards.id"],
            name="fk_share_links_dashboard_id_dashboards",
            ondelete="CASCADE",
        ),
        _by("share_links"),
    )
    op.create_index("ix_share_links_dashboard_id", "share_links", ["dashboard_id"])
    op.create_table(
        "login_attempts",
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("first_failure_at", TS, nullable=False),
        sa.Column("locked_until", TS, nullable=True),
        sa.PrimaryKeyConstraint("key", name="pk_login_attempts"),
    )
    op.create_table(
        "user_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(10), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("used_at", TS, nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_user_links"),
        sa.UniqueConstraint("token_hash", name="uq_user_links_token_hash"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_links_user_id_users", ondelete="CASCADE"
        ),
        _by("user_links"),
    )
    op.create_index("ix_user_links_user_id", "user_links", ["user_id"])


def downgrade() -> None:
    for table in (
        "user_links",
        "login_attempts",
        "share_links",
        "dataset_grants",
        "dashboard_shares",
        "group_members",
        "groups",
    ):
        op.drop_table(table)
    op.drop_column("datasets", "restricted")
