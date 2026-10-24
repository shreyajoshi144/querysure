"""add pipeline_sessions.dataset_bytes for per-user storage quotas

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7

Nullable on purpose: sessions that existed before this migration keep NULL (size unknown) and are
not counted toward the byte/dataset quota until their dataset is next replaced.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, Sequence[str], None] = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("pipeline_sessions") as batch:
        batch.add_column(sa.Column("dataset_bytes", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("pipeline_sessions") as batch:
        batch.drop_column("dataset_bytes")
