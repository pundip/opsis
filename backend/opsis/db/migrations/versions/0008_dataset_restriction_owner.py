"""Who restricted a dataset, and whether an admin did (only an admin may lift that).

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("datasets", sa.Column("restricted_by", sa.Uuid(), nullable=True))
    op.add_column(
        "datasets",
        sa.Column("restricted_by_admin", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_foreign_key(
        "fk_datasets_restricted_by_users",
        "datasets",
        "users",
        ["restricted_by"],
        ["id"],
        ondelete="SET NULL",
    )
    # Restrictions made in M6 could only be made by admins.
    op.execute("UPDATE datasets SET restricted_by_admin = true WHERE restricted")


def downgrade() -> None:
    op.drop_constraint("fk_datasets_restricted_by_users", "datasets", type_="foreignkey")
    op.drop_column("datasets", "restricted_by_admin")
    op.drop_column("datasets", "restricted_by")
