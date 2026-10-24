"""add Google identity binding for authentication

Revision ID: a13f9c2d7e44
Revises: 9f4c1a2e8b71
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a13f9c2d7e44"
down_revision: Union[str, Sequence[str], None] = "9f4c1a2e8b71"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("google_subject", sa.String(length=255), nullable=True))
    op.create_index("ix_users_google_subject", "users", ["google_subject"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_google_subject", table_name="users")
    op.drop_column("users", "google_subject")
