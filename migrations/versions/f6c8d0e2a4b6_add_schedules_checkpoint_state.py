"""dataset_schedules; ingestion_checkpoints.state

Revision ID: f6c8d0e2a4b6
Revises: e5b7c9d1f3a5

Additive. Downgrade drops the table and the column.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f6c8d0e2a4b6"
down_revision: Union[str, Sequence[str], None] = "e5b7c9d1f3a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ingestion_checkpoints") as batch:
        batch.add_column(sa.Column("state", sa.JSON(), nullable=True))
    op.create_table(
        "dataset_schedules",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("dataset_id", sa.String(length=36), sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="incremental"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=16), nullable=True),
        sa.Column("last_job_id", sa.String(length=36), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("disabled_reason", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dataset_id"),
    )
    op.create_index("ix_dataset_schedules_due", "dataset_schedules", ["enabled", "next_run_at"])


def downgrade() -> None:
    op.drop_index("ix_dataset_schedules_due", table_name="dataset_schedules")
    op.drop_table("dataset_schedules")
    with op.batch_alter_table("ingestion_checkpoints") as batch:
        batch.drop_column("state")
