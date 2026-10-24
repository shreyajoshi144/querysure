"""add QuerySure plan and daily query usage entitlements

Revision ID: c4e8f2a1b7d0
Revises: a13f9c2d7e44
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c4e8f2a1b7d0"
down_revision: Union[str, Sequence[str], None] = "a13f9c2d7e44"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("plan", sa.String(length=20), nullable=False, server_default="free"))
    op.add_column("usage_daily", sa.Column("query_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("usage_daily", "query_count")
    op.drop_column("users", "plan")
