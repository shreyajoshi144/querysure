"""knowledge_documents, knowledge_chunks

Revision ID: b8f0a2c4d6e1
Revises: a7e9f1b3c5d8

Additive. Downgrade drops both tables.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8f0a2c4d6e1"
down_revision: Union[str, Sequence[str], None] = "a7e9f1b3c5d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("file_format", sa.String(length=16), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_knowledge_documents_workspace_id", "knowledge_documents", ["workspace_id"])
    op.create_index("uq_knowledge_documents_ws_fingerprint", "knowledge_documents", ["workspace_id", "fingerprint"], unique=True)
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("document_id", sa.String(length=36), sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("heading", sa.String(length=200), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
    )
    op.create_index("ix_knowledge_chunks_workspace_id", "knowledge_chunks", ["workspace_id"])
    op.create_index("ix_knowledge_chunks_doc_ordinal", "knowledge_chunks", ["document_id", "ordinal"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_chunks_doc_ordinal", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_workspace_id", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_index("uq_knowledge_documents_ws_fingerprint", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_workspace_id", table_name="knowledge_documents")
    op.drop_table("knowledge_documents")
