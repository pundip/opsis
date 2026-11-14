"""Semantic models.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
NOW = sa.func.now()


def upgrade() -> None:
    op.create_table(
        "semantic_models",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        # JSON keeps key order (JSONB does not); see the model class.
        sa.Column("definition", postgresql.JSON(), nullable=False),
        sa.Column("is_starter", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_semantic_models"),
        sa.UniqueConstraint("dataset_id", name="uq_semantic_models_dataset_id"),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name="fk_semantic_models_dataset_id_datasets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name="fk_semantic_models_updated_by_users",
            ondelete="SET NULL",
        ),
    )


def downgrade() -> None:
    op.drop_table("semantic_models")
