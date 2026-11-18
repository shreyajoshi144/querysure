"""Sprint 1 migration: upgrading a pre-workspace database backfills personal workspaces and the dataset catalog, the
models match the migrated schema (alembic check), and downgrade fully reverses it. Runs the real alembic CLI in a
subprocess against a throw-away SQLite file, exactly as a deploy does."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRE = "a9c1d2e3f4b5"
NEW = "b8d2e4f6a1c3"


def _alembic(db_file: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "ENVIRONMENT": "development", "JWT_SECRET_KEY": "migration-test",
           "DATABASE_URL": f"sqlite:///{db_file}"}
    run = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True,
                         timeout=180)
    assert run.returncode == 0, run.stdout + run.stderr
    return run


@pytest.fixture()
def legacy_db(tmp_path):
    db_file = str(tmp_path / "legacy.db")
    _alembic(db_file, "upgrade", PRE)
    users = [str(uuid.uuid4()) for _ in range(2)]
    shared_ref = os.path.join(ROOT, "uploads", f"{uuid.uuid4()}.db")     # a managed upload (inside uploads/)
    sample_ref = os.path.join(ROOT, "retail.db")                          # the bundled sample: not catalogued
    schema = {"table_count": 2, "total_rows": 7, "tables": {"orders": {}, "customers": {}}}
    con = sqlite3.connect(db_file)
    for i, uid in enumerate(users):
        con.execute("INSERT INTO users (id, email, hashed_password, is_active, created_at) VALUES (?, ?, 'x', 1, '2026-01-01')",
                    (uid, f"legacy{i}-{uid[:6]}@example.com"))
    sessions = {"a1": (users[0], shared_ref), "a2": (users[0], shared_ref), "a3": (users[0], sample_ref), "a4": (users[0], None)}
    for name, (uid, ref) in sessions.items():
        con.execute("INSERT INTO pipeline_sessions (id, user_id, db_path, schema, dataset_bytes, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, '2026-01-02', '2026-01-02')",
                    (name, uid, ref, json.dumps(schema) if ref else None, 4096 if ref == shared_ref else None))
    con.commit()
    con.close()
    return db_file, users, shared_ref


def test_upgrade_backfills_workspaces_and_datasets(legacy_db):
    db_file, users, shared_ref = legacy_db
    _alembic(db_file, "upgrade", "head")
    con = sqlite3.connect(db_file)
    ws = con.execute("SELECT id, kind, owner_id, personal_owner_id, name FROM workspaces").fetchall()
    assert sorted((w[2], w[1], w[3]) for w in ws) == sorted((u, "personal", u) for u in users)
    members = con.execute("SELECT workspace_id, user_id, role FROM workspace_members").fetchall()
    assert sorted((m[1], m[2]) for m in members) == sorted((u, "owner") for u in users)
    datasets = con.execute("SELECT id, workspace_id, created_by, name, storage_ref, bytes, table_count, row_count FROM datasets").fetchall()
    assert len(datasets) == 1, "one catalog row per distinct uploaded file; sample database excluded"
    ds = datasets[0]
    personal_a = next(w[0] for w in ws if w[2] == users[0])
    assert ds[1:] == (personal_a, users[0], "customers, orders", shared_ref, 4096, 2, 7)
    links = dict(con.execute("SELECT id, dataset_id FROM pipeline_sessions").fetchall())
    assert links == {"a1": ds[0], "a2": ds[0], "a3": None, "a4": None}
    assert con.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0
    con.close()


def test_models_match_migrated_schema(legacy_db):
    db_file, _, _ = legacy_db
    _alembic(db_file, "upgrade", "head")
    out = _alembic(db_file, "check")
    assert "No new upgrade operations detected" in (out.stdout + out.stderr)


def test_downgrade_removes_everything_and_upgrade_is_repeatable(legacy_db):
    db_file, users, _ = legacy_db
    _alembic(db_file, "upgrade", "head")
    _alembic(db_file, "downgrade", PRE)
    con = sqlite3.connect(db_file)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {"workspaces", "workspace_members", "datasets", "audit_log"} & tables
    cols = {r[1] for r in con.execute("PRAGMA table_info(pipeline_sessions)")}
    assert "dataset_id" not in cols
    assert con.execute("SELECT count(*) FROM pipeline_sessions").fetchone()[0] == 4  # legacy data untouched
    con.close()
    _alembic(db_file, "upgrade", "head")
    con = sqlite3.connect(db_file)
    assert con.execute("SELECT count(*) FROM workspaces").fetchone()[0] == len(users)
    con.close()


def test_sprint2_ai_calls_migration_is_additive_and_reversible(legacy_db):
    """c3f5a7b9d1e2 (Sprint 2) adds ai_calls and ai_invocations.trace_id; downgrade removes exactly that."""
    db_file, _, _ = legacy_db
    _alembic(db_file, "upgrade", "head")
    con = sqlite3.connect(db_file)
    assert "trace_id" in {r[1] for r in con.execute("PRAGMA table_info(ai_invocations)")}
    assert con.execute("SELECT count(*) FROM ai_calls").fetchone()[0] == 0
    con.close()
    _alembic(db_file, "downgrade", NEW)
    con = sqlite3.connect(db_file)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ai_calls" not in tables and "workspaces" in tables
    assert "trace_id" not in {r[1] for r in con.execute("PRAGMA table_info(ai_invocations)")}
    con.close()


def test_sprint3_migration_backfills_versions_and_is_reversible(legacy_db):
    """d4a6b8c0e2f4 (Sprint 3): existing datasets become version 1 / ready; downgrade removes the run table and new columns."""
    db_file, _, _ = legacy_db
    _alembic(db_file, "upgrade", "head")
    con = sqlite3.connect(db_file)
    assert con.execute("SELECT version, status FROM datasets").fetchall() == [(1, "ready")]
    assert con.execute("SELECT count(*) FROM ingestion_runs").fetchone()[0] == 0
    con.close()
    _alembic(db_file, "downgrade", "c3f5a7b9d1e2")
    con = sqlite3.connect(db_file)
    assert "ingestion_runs" not in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {"version", "status", "connector", "fingerprint"} & {r[1] for r in con.execute("PRAGMA table_info(datasets)")}
    assert "dataset_version" not in {r[1] for r in con.execute("PRAGMA table_info(pipeline_sessions)")}
    con.close()
    _alembic(db_file, "upgrade", "head")


def test_sprint4_migration_adds_connections_and_checkpoints_and_is_reversible(legacy_db):
    """e5b7c9d1f3a5 (Sprint 4): connections, ingestion_checkpoints and the dataset / run columns; downgrade removes them."""
    db_file, _, _ = legacy_db
    _alembic(db_file, "upgrade", "head")
    con = sqlite3.connect(db_file)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"connections", "ingestion_checkpoints"} <= tables
    assert {"connection_id", "source_table", "sync_config"} <= {r[1] for r in con.execute("PRAGMA table_info(datasets)")}
    assert con.execute("SELECT count(*) FROM datasets WHERE connection_id IS NOT NULL").fetchone()[0] == 0
    con.close()
    _alembic(db_file, "downgrade", "d4a6b8c0e2f4")
    con = sqlite3.connect(db_file)
    assert not {"connections", "ingestion_checkpoints"} & {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {"connection_id", "source_table", "sync_config"} & {r[1] for r in con.execute("PRAGMA table_info(datasets)")}
    assert not {"watermark_before", "watermark_after", "fallback_reason"} & {r[1] for r in con.execute("PRAGMA table_info(ingestion_runs)")}
    con.close()
    _alembic(db_file, "upgrade", "head")


def test_sprint5_migration_adds_schedules_and_checkpoint_state_and_is_reversible(legacy_db):
    """f6c8d0e2a4b6 (Sprint 5): dataset_schedules and ingestion_checkpoints.state; downgrade removes them."""
    db_file, _, _ = legacy_db
    _alembic(db_file, "upgrade", "head")
    con = sqlite3.connect(db_file)
    assert "dataset_schedules" in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "state" in {r[1] for r in con.execute("PRAGMA table_info(ingestion_checkpoints)")}
    con.close()
    _alembic(db_file, "downgrade", "e5b7c9d1f3a5")
    con = sqlite3.connect(db_file)
    assert "dataset_schedules" not in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "state" not in {r[1] for r in con.execute("PRAGMA table_info(ingestion_checkpoints)")}
    con.close()
    _alembic(db_file, "upgrade", "head")


def test_sprint6_migration_adds_quality_reports_and_is_reversible(legacy_db):
    """a7e9f1b3c5d8 (Sprint 6): dataset_quality_reports; downgrade removes it."""
    db_file, _, _ = legacy_db
    _alembic(db_file, "upgrade", "head")
    con = sqlite3.connect(db_file)
    assert "dataset_quality_reports" in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    _alembic(db_file, "downgrade", "f6c8d0e2a4b6")
    con = sqlite3.connect(db_file)
    assert "dataset_quality_reports" not in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    _alembic(db_file, "upgrade", "head")


def test_sprint7_migration_adds_knowledge_tables_and_is_reversible(legacy_db):
    """b8f0a2c4d6e1 (Sprint 7): knowledge_documents and knowledge_chunks; downgrade removes them."""
    db_file, _, _ = legacy_db
    _alembic(db_file, "upgrade", "head")
    tables = lambda: {r[0] for r in sqlite3.connect(db_file).execute("SELECT name FROM sqlite_master WHERE type='table'")}  # noqa: E731
    assert {"knowledge_documents", "knowledge_chunks"} <= tables()
    _alembic(db_file, "downgrade", "a7e9f1b3c5d8")
    assert not ({"knowledge_documents", "knowledge_chunks"} & tables())
    _alembic(db_file, "upgrade", "head")


def test_sprint8_migration_adds_conversation_tables_and_is_reversible(legacy_db):
    """c9e1a3b5d7f2 (Sprint 8): conversations and conversation_messages; downgrade removes them."""
    db_file, _, _ = legacy_db
    _alembic(db_file, "upgrade", "head")
    tables = lambda: {r[0] for r in sqlite3.connect(db_file).execute("SELECT name FROM sqlite_master WHERE type='table'")}  # noqa: E731
    assert {"conversations", "conversation_messages"} <= tables()
    _alembic(db_file, "downgrade", "b8f0a2c4d6e1")
    assert not ({"conversations", "conversation_messages"} & tables())
    _alembic(db_file, "upgrade", "head")


def test_sprint9_migration_adds_agent_tables_and_is_reversible(legacy_db):
    """d1f3b5a7c9e4 (Sprint 9): agent_runs and agent_steps; downgrade removes them."""
    db_file, _, _ = legacy_db
    _alembic(db_file, "upgrade", "head")
    tables = lambda: {r[0] for r in sqlite3.connect(db_file).execute("SELECT name FROM sqlite_master WHERE type='table'")}  # noqa: E731
    assert {"agent_runs", "agent_steps"} <= tables()
    _alembic(db_file, "downgrade", "c9e1a3b5d7f2")
    assert not ({"agent_runs", "agent_steps"} & tables())
    _alembic(db_file, "upgrade", "head")


def test_sprint10_migration_adds_investigations_and_is_reversible(legacy_db):
    """e2a4c6d8f0b5 (Sprint 10): investigations; downgrade removes the table."""
    db_file, _, _ = legacy_db
    _alembic(db_file, "upgrade", "head")
    tables = lambda: {r[0] for r in sqlite3.connect(db_file).execute("SELECT name FROM sqlite_master WHERE type='table'")}  # noqa: E731
    assert "investigations" in tables()
    _alembic(db_file, "downgrade", "d1f3b5a7c9e4")
    assert "investigations" not in tables()
    _alembic(db_file, "upgrade", "head")
