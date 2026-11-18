"""ai_calls (one row per provider attempt) and ai_invocations.trace_id

Revision ID: c3f5a7b9d1e2
Revises: b8d2e4f6a1c3

Additive only: a new table and one nullable column. Existing ai_invocations rows keep trace_id NULL.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3f5a7b9d1e2"
down_revision: Union[str, Sequence[str], None] = "b8d2e4f6a1c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_calls",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("retry_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("usage_estimated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("estimated_cost_cents", sa.Float(), nullable=False, server_default="0"),
        sa.Column("tool_calls", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ai_calls_trace_id", "ai_calls", ["trace_id"])
    op.create_index("ix_ai_calls_user_created", "ai_calls", ["user_id", "created_at"])
    with op.batch_alter_table("ai_invocations") as batch:
        batch.add_column(sa.Column("trace_id", sa.String(length=64), nullable=True))
        batch.create_index("ix_ai_invocations_trace_id", ["trace_id"])


def downgrade() -> None:
    with op.batch_alter_table("ai_invocations") as batch:
        batch.drop_index("ix_ai_invocations_trace_id")
        batch.drop_column("trace_id")
    op.drop_index("ix_ai_calls_user_created", table_name="ai_calls")
    op.drop_index("ix_ai_calls_trace_id", table_name="ai_calls")
    op.drop_table("ai_calls")
