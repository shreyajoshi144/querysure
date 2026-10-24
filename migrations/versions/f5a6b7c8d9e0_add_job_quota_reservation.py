"""jobs.session_id / jobs.reserved_bytes: count in-flight uploads against the storage quota

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9

Both nullable; existing rows are unaffected (no reservation).
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, Sequence[str], None] = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("session_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("reserved_bytes", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("reserved_bytes")
        batch.drop_column("session_id")
