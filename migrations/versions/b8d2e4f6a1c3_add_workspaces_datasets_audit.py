"""workspaces, workspace_members, datasets, audit_log; pipeline_sessions.dataset_id

Revision ID: b8d2e4f6a1c3
Revises: a9c1d2e3f4b5

Backfill (idempotent per row): every existing user gets a personal workspace in which they are owner, and every distinct
uploaded dataset a session points at gets a catalog row in its owner's personal workspace, linked from the session.
Nothing existing is modified except the new pipeline_sessions.dataset_id column. Downgrade drops it all.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8d2e4f6a1c3"
down_revision: Union[str, Sequence[str], None] = "a9c1d2e3f4b5"
branch_labels = None
depends_on = None

def _dataset_name(schema) -> str:
    tables = sorted((schema or {}).get("tables", {}) or {}) if isinstance(schema, dict) else []
    if not tables:
        return "Uploaded dataset"
    name = ", ".join(tables[:4]) + (f" (+{len(tables) - 4} more)" if len(tables) > 4 else "")
    return name[:255]


def _is_upload(ref: str) -> bool:
    try:
        import storage
        return storage.is_managed(ref)
    except Exception:  # noqa: BLE001 - storage config unavailable: only S3 refs are certainly uploads
        return ref.startswith("s3://")


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="team"),
        sa.Column("owner_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("personal_owner_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("personal_owner_id"),
    )
    op.create_index("ix_workspaces_owner_id", "workspaces", ["owner_id"])
    op.create_table(
        "workspace_members",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_members_workspace_user"),
    )
    op.create_index("ix_workspace_members_workspace_id", "workspace_members", ["workspace_id"])
    op.create_index("ix_workspace_members_user_id", "workspace_members", ["user_id"])
    op.create_table(
        "datasets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False, server_default="upload"),
        sa.Column("file_format", sa.String(length=16), nullable=True),
        sa.Column("storage_ref", sa.Text(), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=True),
        sa.Column("table_count", sa.Integer(), nullable=True),
        sa.Column("row_count", sa.BigInteger(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("storage_ref"),
    )
    op.create_index("ix_datasets_workspace_id", "datasets", ["workspace_id"])
    op.create_index("ix_datasets_created_by", "datasets", ["created_by"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_log_workspace_created", "audit_log", ["workspace_id", "created_at"])
    with op.batch_alter_table("pipeline_sessions") as batch:
        batch.add_column(sa.Column("dataset_id", sa.String(length=36), nullable=True))
        batch.create_index("ix_pipeline_sessions_dataset_id", ["dataset_id"])

    _backfill()


def _backfill() -> None:
    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    users = sa.table("users", sa.column("id", sa.String))
    workspaces = sa.table(
        "workspaces", sa.column("id", sa.String), sa.column("name", sa.String), sa.column("kind", sa.String),
        sa.column("owner_id", sa.String), sa.column("personal_owner_id", sa.String),
        sa.column("created_at", sa.DateTime), sa.column("updated_at", sa.DateTime),
    )
    members = sa.table(
        "workspace_members", sa.column("id", sa.String), sa.column("workspace_id", sa.String),
        sa.column("user_id", sa.String), sa.column("role", sa.String),
        sa.column("created_at", sa.DateTime), sa.column("updated_at", sa.DateTime),
    )
    datasets = sa.table(
        "datasets", sa.column("id", sa.String), sa.column("workspace_id", sa.String), sa.column("created_by", sa.String),
        sa.column("name", sa.String), sa.column("source_type", sa.String), sa.column("file_format", sa.String),
        sa.column("storage_ref", sa.Text), sa.column("bytes", sa.BigInteger), sa.column("table_count", sa.Integer),
        sa.column("row_count", sa.BigInteger), sa.column("schema_version", sa.Integer),
        sa.column("created_at", sa.DateTime), sa.column("updated_at", sa.DateTime),
    )
    sessions = sa.table(
        "pipeline_sessions", sa.column("id", sa.String), sa.column("user_id", sa.String),
        sa.column("db_path", sa.String), sa.column("schema", sa.JSON), sa.column("dataset_bytes", sa.BigInteger),
        sa.column("created_at", sa.DateTime), sa.column("dataset_id", sa.String),
    )

    personal: dict[str, str] = {}
    for (user_id,) in bind.execute(sa.select(users.c.id)).fetchall():
        ws_id = str(uuid.uuid4())
        personal[user_id] = ws_id
        bind.execute(workspaces.insert().values(id=ws_id, name="Personal", kind="personal", owner_id=user_id,
                                                personal_owner_id=user_id, created_at=now, updated_at=now))
        bind.execute(members.insert().values(id=str(uuid.uuid4()), workspace_id=ws_id, user_id=user_id, role="owner",
                                             created_at=now, updated_at=now))

    by_ref: dict[str, str] = {}
    rows = bind.execute(
        sa.select(sessions.c.id, sessions.c.user_id, sessions.c.db_path, sessions.c.schema, sessions.c.dataset_bytes,
                  sessions.c.created_at).where(sessions.c.db_path.isnot(None)).order_by(sessions.c.created_at)
    ).fetchall()
    for sid, user_id, ref, schema, size, created in rows:
        if not ref or user_id not in personal or not _is_upload(ref):
            continue
        ds_id = by_ref.get(ref)
        if ds_id is None:
            ds_id = str(uuid.uuid4())
            by_ref[ref] = ds_id
            schema = schema if isinstance(schema, dict) else {}
            bind.execute(datasets.insert().values(
                id=ds_id, workspace_id=personal[user_id], created_by=user_id, name=_dataset_name(schema),
                source_type="upload", file_format=None, storage_ref=ref,
                bytes=size, table_count=schema.get("table_count"), row_count=schema.get("total_rows"), schema_version=1,
                created_at=created or now, updated_at=now,
            ))
        bind.execute(sessions.update().where(sessions.c.id == sid).values(dataset_id=ds_id))


def downgrade() -> None:
    with op.batch_alter_table("pipeline_sessions") as batch:
        batch.drop_index("ix_pipeline_sessions_dataset_id")
        batch.drop_column("dataset_id")
    op.drop_index("ix_audit_log_workspace_created", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_datasets_created_by", table_name="datasets")
    op.drop_index("ix_datasets_workspace_id", table_name="datasets")
    op.drop_table("datasets")
    op.drop_index("ix_workspace_members_user_id", table_name="workspace_members")
    op.drop_index("ix_workspace_members_workspace_id", table_name="workspace_members")
    op.drop_table("workspace_members")
    op.drop_index("ix_workspaces_owner_id", table_name="workspaces")
    op.drop_table("workspaces")
