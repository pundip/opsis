"""Semantic model version history.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column(
        "semantic_models",
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_table(
        "semantic_model_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition", postgresql.JSON(), nullable=False),
        sa.Column("author_type", sa.String(20), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=True),
        sa.Column("message", sa.String(500), nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_semantic_model_versions"),
        sa.UniqueConstraint("dataset_id", "version", name="uq_semantic_model_versions_dataset_id"),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name="fk_semantic_model_versions_dataset_id_datasets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            name="fk_semantic_model_versions_author_id_users",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_semantic_model_versions_dataset_id", "semantic_model_versions", ["dataset_id"]
    )
    # Models saved before version history existed become version 1.
    op.execute(
        """
        INSERT INTO semantic_model_versions
            (id, dataset_id, version, definition, author_type, author_id, message, created_at)
        SELECT gen_random_uuid(), dataset_id, 1, definition,
               CASE WHEN is_starter THEN 'system' ELSE 'user' END, updated_by,
               'Model as it was when version history was introduced', updated_at
        FROM semantic_models
        """
    )


def downgrade() -> None:
    op.drop_table("semantic_model_versions")
    op.drop_column("semantic_models", "version")
