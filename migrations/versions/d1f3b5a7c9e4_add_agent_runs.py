"""agent_runs, agent_steps

Revision ID: d1f3b5a7c9e4
Revises: c9e1a3b5d7f2

Additive. Downgrade drops both tables.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d1f3b5a7c9e4"
down_revision: Union[str, Sequence[str], None] = "c9e1a3b5d7f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("provider", sa.String(length=40), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_runs_ws_user_created", "agent_runs", ["workspace_id", "user_id", "created_at"])
    op.create_table(
        "agent_steps",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("tool", sa.String(length=40), nullable=False),
        sa.Column("args", sa.JSON(), nullable=False),
        sa.Column("why", sa.String(length=300), nullable=True),
        sa.Column("risk", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(length=300), nullable=True),
        sa.Column("decided_by", sa.String(length=36), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_steps_run_ordinal", "agent_steps", ["run_id", "ordinal"])


def downgrade() -> None:
    op.drop_index("ix_agent_steps_run_ordinal", table_name="agent_steps")
    op.drop_table("agent_steps")
    op.drop_index("ix_agent_runs_ws_user_created", table_name="agent_runs")
    op.drop_table("agent_runs")
