"""Bring your own agent (v2 V1): people's own API keys, grants of the workspace key,
admin settings, each person's choice in the assistant, and token usage per key.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
NOW = sa.func.now()


def _user_fk(table: str, column: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], ["users.id"], name=f"fk_{table}_{column}_users", ondelete=ondelete
    )


def upgrade() -> None:
    op.create_table(
        "user_api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("last4", sa.String(4), nullable=False),
        sa.Column("default_model", sa.String(200), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("last_test_message", sa.Text(), nullable=True),
        sa.Column("last_tested_at", TS, nullable=True),
        sa.Column("last_used_at", TS, nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        _user_fk("user_api_keys", "user_id", "CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_user_api_keys"),
        sa.UniqueConstraint("user_id", "label", name="uq_user_api_keys_user_id"),
    )
    op.create_index("ix_user_api_keys_user_id", "user_api_keys", ["user_id"])

    op.create_table(
        "capability_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("capability", sa.String(60), nullable=False),
        sa.Column("grantee", sa.String(60), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        _user_fk("capability_grants", "created_by", "SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_capability_grants"),
        sa.UniqueConstraint("capability", "grantee", name="uq_capability_grants_capability"),
    )

    op.create_table(
        "workspace_settings",
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        _user_fk("workspace_settings", "updated_by", "SET NULL"),
        sa.PrimaryKeyConstraint("key", name="pk_workspace_settings"),
    )

    op.create_table(
        "assistant_choices",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("option", sa.String(80), nullable=False),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        _user_fk("assistant_choices", "user_id", "CASCADE"),
        sa.PrimaryKeyConstraint("user_id", name="pk_assistant_choices"),
    )

    op.create_table(
        "assistant_usage",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("key_id", sa.Uuid(), nullable=True),
        sa.Column("key_label", sa.String(100), nullable=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("cached_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        _user_fk("assistant_usage", "user_id", "CASCADE"),
        sa.ForeignKeyConstraint(
            ["key_id"],
            ["user_api_keys.id"],
            name="fk_assistant_usage_key_id_user_api_keys",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assistant_usage"),
    )
    op.create_index("ix_assistant_usage_user_id", "assistant_usage", ["user_id"])

    op.add_column("assistant_conversations", sa.Column("option", sa.String(80), nullable=True))
    # v1 conversations ran on the workspace's provider.
    op.execute(
        "UPDATE assistant_conversations SET option = CASE provider "
        "WHEN 'anthropic' THEN 'workspace' ELSE 'ollama' END"
    )


def downgrade() -> None:
    op.drop_column("assistant_conversations", "option")
    op.drop_table("assistant_usage")
    op.drop_table("assistant_choices")
    op.drop_table("workspace_settings")
    op.drop_table("capability_grants")
    op.drop_table("user_api_keys")
