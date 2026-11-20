"""The in-app assistant: conversations, the provider's history, and the panel's entries.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
NOW = sa.func.now()


def upgrade() -> None:
    op.create_table(
        "assistant_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dashboard_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["dashboard_id"],
            ["dashboards.id"],
            name="fk_assistant_conversations_dashboard_id_dashboards",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_assistant_conversations_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assistant_conversations"),
    )
    op.create_index(
        "ix_assistant_conversations_lookup",
        "assistant_conversations",
        ["dashboard_id", "user_id"],
    )
    op.create_index(
        "ix_assistant_conversations_user_id", "assistant_conversations", ["user_id"]
    )
    for table, column in (("assistant_messages", "message"), ("assistant_entries", "data")):
        columns: list[sa.schema.SchemaItem] = [
            sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
            sa.Column("conversation_id", sa.Uuid(), nullable=False),
        ]
        if table == "assistant_entries":
            columns.append(sa.Column("kind", sa.String(20), nullable=False))
        op.create_table(
            table,
            *columns,
            sa.Column(column, sa.JSON(), nullable=False),
            sa.Column("created_at", TS, server_default=NOW, nullable=False),
            sa.ForeignKeyConstraint(
                ["conversation_id"],
                ["assistant_conversations.id"],
                name=f"fk_{table}_conversation_id_assistant_conversations",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        )
        op.create_index(f"ix_{table}_conversation_id", table, ["conversation_id"])


def downgrade() -> None:
    op.drop_table("assistant_entries")
    op.drop_table("assistant_messages")
    op.drop_table("assistant_conversations")
