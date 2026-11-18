"""ingestion_runs; datasets.version/status/connector/fingerprint; pipeline_sessions.dataset_version

Revision ID: d4a6b8c0e2f4
Revises: c3f5a7b9d1e2

Additive. Existing datasets become version 1, status ready. Downgrade drops the table and the new columns.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4a6b8c0e2f4"
down_revision: Union[str, Sequence[str], None] = "c3f5a7b9d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("datasets") as batch:
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("status", sa.String(length=16), nullable=False, server_default="ready"))
        batch.add_column(sa.Column("connector", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("fingerprint", sa.String(length=64), nullable=True))
    with op.batch_alter_table("pipeline_sessions") as batch:
        batch.add_column(sa.Column("dataset_version", sa.Integer(), nullable=True))
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("dataset_id", sa.String(length=36), sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("connector", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="full_refresh"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("source_name", sa.String(length=255), nullable=True),
        sa.Column("source_bytes", sa.BigInteger(), nullable=True),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("records_read", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("records_inserted", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("records_updated", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("records_skipped", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("records_rejected", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("duplicate_rows", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reject_reasons", sa.JSON(), nullable=True),
        sa.Column("table_stats", sa.JSON(), nullable=True),
        sa.Column("schema_changes", sa.JSON(), nullable=True),
        sa.Column("dataset_version", sa.Integer(), nullable=True),
        sa.Column("curated_ref", sa.Text(), nullable=True),
        sa.Column("raw_ref", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail", sa.String(length=300), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_ingestion_runs_workspace_id", "ingestion_runs", ["workspace_id"])
    op.create_index("ix_ingestion_runs_job_id", "ingestion_runs", ["job_id"])
    op.create_index("ix_ingestion_runs_dataset_started", "ingestion_runs", ["dataset_id", "started_at"])
    active = sa.text("status IN ('queued', 'running')")
    op.create_index("uq_ingestion_runs_active_dataset", "ingestion_runs", ["dataset_id"], unique=True,
                    postgresql_where=active, sqlite_where=active)


def downgrade() -> None:
    op.drop_index("uq_ingestion_runs_active_dataset", table_name="ingestion_runs")
    op.drop_index("ix_ingestion_runs_dataset_started", table_name="ingestion_runs")
    op.drop_index("ix_ingestion_runs_job_id", table_name="ingestion_runs")
    op.drop_index("ix_ingestion_runs_workspace_id", table_name="ingestion_runs")
    op.drop_table("ingestion_runs")
    with op.batch_alter_table("pipeline_sessions") as batch:
        batch.drop_column("dataset_version")
    with op.batch_alter_table("datasets") as batch:
        batch.drop_column("fingerprint")
        batch.drop_column("connector")
        batch.drop_column("status")
        batch.drop_column("version")
