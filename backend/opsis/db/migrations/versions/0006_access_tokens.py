"""Personal access tokens for the MCP server.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "access_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("prefix", sa.String(20), nullable=False),
        sa.Column("scopes", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("last_used_at", TS, nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_access_tokens"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_access_tokens_user_id_users", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("token_hash", name="uq_access_tokens_token_hash"),
    )
    op.create_index("ix_access_tokens_user_id", "access_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_access_tokens_user_id", table_name="access_tokens")
    op.drop_table("access_tokens")
