"""conversations, conversation_messages

Revision ID: c9e1a3b5d7f2
Revises: b8f0a2c4d6e1

Additive. Downgrade drops both tables.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c9e1a3b5d7f2"
down_revision: Union[str, Sequence[str], None] = "b8f0a2c4d6e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_conversations_ws_user_updated", "conversations", ["workspace_id", "user_id", "updated_at"])
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(length=36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("intent", sa.String(length=24), nullable=True),
        sa.Column("grounded", sa.Boolean(), nullable=True),
        sa.Column("citations", sa.JSON(), nullable=True),
        sa.Column("sources", sa.JSON(), nullable=True),
        sa.Column("provider", sa.String(length=40), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_conversation_messages_conv", "conversation_messages", ["conversation_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_conversation_messages_conv", table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_index("ix_conversations_ws_user_updated", table_name="conversations")
    op.drop_table("conversations")
