"""investigations

Revision ID: e2a4c6d8f0b5
Revises: d1f3b5a7c9e4

Additive. Downgrade drops the table.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2a4c6d8f0b5"
down_revision: Union[str, Sequence[str], None] = "d1f3b5a7c9e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "investigations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("dataset_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("exports", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_investigations_ws_created", "investigations", ["workspace_id", "created_at"])
    op.create_index("ix_investigations_dataset", "investigations", ["dataset_id"])


def downgrade() -> None:
    op.drop_index("ix_investigations_dataset", table_name="investigations")
    op.drop_index("ix_investigations_ws_created", table_name="investigations")
    op.drop_table("investigations")
