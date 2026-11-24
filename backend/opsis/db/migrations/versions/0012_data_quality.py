"""Data quality: what each refresh changed, and a dataset's column type overrides.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Per column: values stored as NULL because they could not be converted, why,
    # and a few raw samples; plus type suggestions for text columns.
    op.add_column("snapshots", sa.Column("report", postgresql.JSONB(), nullable=True))
    # {column: {"type": "date", "format": "%d/%m/%Y"}}, kept when columns are rediscovered.
    op.add_column(
        "datasets",
        sa.Column(
            "column_overrides",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("datasets", "column_overrides")
    op.drop_column("snapshots", "report")
