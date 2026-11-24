"""Connections: the state of a saved sign-in (for Google Sheets through OAuth).

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Not secret: which account is connected, the scopes, and whether it needs
    # reconnecting. The refresh token itself is in the encrypted secrets.
    op.add_column("connections", sa.Column("auth_state", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("connections", "auth_state")
