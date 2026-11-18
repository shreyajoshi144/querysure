"""dataset_quality_reports

Revision ID: a7e9f1b3c5d8
Revises: f6c8d0e2a4b6

Additive. Downgrade drops the table.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7e9f1b3c5d8"
down_revision: Union[str, Sequence[str], None] = "f6c8d0e2a4b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dataset_quality_reports",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("dataset_id", sa.String(length=36), sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("dataset_version", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("profile", sa.JSON(), nullable=True),
        sa.Column("findings", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_dataset_quality_reports_workspace_id", "dataset_quality_reports", ["workspace_id"])
    op.create_index("ix_dataset_quality_dataset_created", "dataset_quality_reports", ["dataset_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_dataset_quality_dataset_created", table_name="dataset_quality_reports")
    op.drop_index("ix_dataset_quality_reports_workspace_id", table_name="dataset_quality_reports")
    op.drop_table("dataset_quality_reports")
