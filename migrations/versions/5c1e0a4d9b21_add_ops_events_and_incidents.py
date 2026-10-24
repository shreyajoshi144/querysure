"""add ops_events and ops_incidents (Sprint 4 incident / event tracking)

Revision ID: 5c1e0a4d9b21
Revises: f5a6b7c8d9e0
Create Date: 2026-10-04 00:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "5c1e0a4d9b21"
down_revision: Union[str, Sequence[str], None] = "f5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ops_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), server_default="info", nullable=False),
        sa.Column("component", sa.String(length=32), server_default="app", nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ops_events_created_at", "ops_events", ["created_at"], unique=False)
    op.create_index("ix_ops_events_kind_created", "ops_events", ["kind", "created_at"], unique=False)

    op.create_table(
        "ops_incidents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("incident_key", sa.String(length=96), nullable=False),
        sa.Column("alert", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), server_default="warning", nullable=False),
        sa.Column("component", sa.String(length=32), server_default="app", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("occurrences", sa.Integer(), server_default="1", nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ops_incidents_status_opened", "ops_incidents", ["status", "opened_at"], unique=False)
    op.create_index(
        "uq_ops_incidents_open_key", "ops_incidents", ["incident_key"], unique=True,
        postgresql_where=sa.text("status = 'open'"), sqlite_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("uq_ops_incidents_open_key", table_name="ops_incidents",
                  postgresql_where=sa.text("status = 'open'"), sqlite_where=sa.text("status = 'open'"))
    op.drop_index("ix_ops_incidents_status_opened", table_name="ops_incidents")
    op.drop_table("ops_incidents")
    op.drop_index("ix_ops_events_kind_created", table_name="ops_events")
    op.drop_index("ix_ops_events_created_at", table_name="ops_events")
    op.drop_table("ops_events")
