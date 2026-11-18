"""connections, ingestion_checkpoints; datasets.connection_id/source_table/sync_config; ingestion_runs watermark + fallback

Revision ID: e5b7c9d1f3a5
Revises: d4a6b8c0e2f4

Additive. Existing datasets are file uploads (connection_id NULL). Downgrade drops the new tables and columns.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5b7c9d1f3a5"
down_revision: Union[str, Sequence[str], None] = "d4a6b8c0e2f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connections",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("connector", sa.String(length=32), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("secret_ciphertext", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("last_test_error", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "name", name="uq_connections_workspace_name"),
    )
    op.create_index("ix_connections_workspace_id", "connections", ["workspace_id"])
    op.create_table(
        "ingestion_checkpoints",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("dataset_id", sa.String(length=36), sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("column_name", sa.String(length=255), nullable=False),
        sa.Column("column_type", sa.String(length=16), nullable=False),
        sa.Column("value", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dataset_id"),
    )
    with op.batch_alter_table("datasets") as batch:
        batch.add_column(sa.Column("connection_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("source_table", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("sync_config", sa.JSON(), nullable=True))
        batch.create_index("ix_datasets_connection_id", ["connection_id"])
    with op.batch_alter_table("ingestion_runs") as batch:
        batch.add_column(sa.Column("watermark_before", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("watermark_after", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("fallback_reason", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ingestion_runs") as batch:
        batch.drop_column("fallback_reason")
        batch.drop_column("watermark_after")
        batch.drop_column("watermark_before")
    with op.batch_alter_table("datasets") as batch:
        batch.drop_index("ix_datasets_connection_id")
        batch.drop_column("sync_config")
        batch.drop_column("source_table")
        batch.drop_column("connection_id")
    op.drop_table("ingestion_checkpoints")
    op.drop_index("ix_connections_workspace_id", table_name="connections")
    op.drop_table("connections")
