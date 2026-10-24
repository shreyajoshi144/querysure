"""add query_history table (per-user query history)

Revision ID: a9c1d2e3f4b5
Revises: 5a1e7c3b9d20
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a9c1d2e3f4b5"
down_revision: Union[str, Sequence[str], None] = "5a1e7c3b9d20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "query_history",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("question", sa.String(), nullable=True),
        sa.Column("sql", sa.String(), nullable=False),
        sa.Column("sql_edited", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dataset_ref", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("answer_meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_query_history_user_id", "query_history", ["user_id"], unique=False)
    op.create_index("ix_query_history_created_at", "query_history", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_query_history_created_at", table_name="query_history")
    op.drop_index("ix_query_history_user_id", table_name="query_history")
    op.drop_table("query_history")
