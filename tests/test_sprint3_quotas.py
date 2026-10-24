from __future__ import annotations

import io
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import config
import quotas
from db import PipelineSession, SessionLocal, User
from tests.sprint3_helpers import connect_csv, register_headers

CSV = b"region,amount\nN,1\nS,2\n"


@pytest.fixture()
def limits(monkeypatch):
    """Tiny, isolated free-plan limits per test."""
    def apply(**kw):
        monkeypatch.setitem(config.QUOTA_LIMITS, "free", {**config.QUOTA_LIMITS["free"], **kw})
    return apply


def _session(client, headers):
    r = client._inner.post("/api/session", headers=headers, json={})
    return r


def _upload(client, headers, sid, data=CSV, name="a.csv"):
    return client._inner.post("/api/connect/upload", headers=headers, data={"session_id": sid},
                              files={"file": (name, io.BytesIO(data), "text/csv")})


def _user_rows(email):
    with SessionLocal() as db:
        uid = db.query(User).filter(User.email == email).one().id
        return db.query(PipelineSession).filter(PipelineSession.user_id == uid).all()


# ------------------------------------------------------------------ sessions
def test_session_quota_enforced_with_clear_403(client, limits):
    limits(max_sessions=3)
    headers, _ = register_headers(client)
    for _ in range(3):
        assert _session(client, headers).status_code == 200
    r = _session(client, headers)
    assert r.status_code == 403
    body = r.json()
    assert body["error_code"] == "QUOTA_EXCEEDED" and body["quota"] == "sessions"
    assert body["limit"] == 3 and body["used"] == 3 and body["success"] is False and "3" in body["error"]


def test_quota_is_per_user(client, limits):
    limits(max_sessions=1)
    a, _ = register_headers(client)
    b, _ = register_headers(client)
    assert _session(client, a).status_code == 200
    assert _session(client, a).status_code == 403
    assert _session(client, b).status_code == 200


def test_empty_idle_sessions_are_reclaimed_so_reloads_do_not_lock_users_out(client, limits, monkeypatch):
    limits(max_sessions=2)
    headers, email = register_headers(client)
    _session(client, headers); _session(client, headers)
    # age them past the reclaim window
    with SessionLocal() as db:
        for r in db.query(PipelineSession).all():
            r.updated_at = datetime.now(timezone.utc) - timedelta(hours=3)
        db.commit()
    assert _session(client, headers).status_code == 200          # oldest empty session reclaimed
    assert len(_user_rows(email)) <= 2


def test_fresh_empty_sessions_and_dataset_sessions_are_never_reclaimed(client, limits):
    limits(max_sessions=2, max_datasets=10)
    headers, email = register_headers(client)
    sid = connect_csv(client, headers)                           # session with a dataset
    assert _session(client, headers).status_code == 200          # fresh empty session (not yet idle)
    r = _session(client, headers)
    assert r.status_code == 403
    assert sid in {row.id for row in _user_rows(email)}


# ------------------------------------------------------------------ datasets / bytes
def test_dataset_count_quota_on_upload(client, limits):
    limits(max_datasets=2, max_sessions=50)
    headers, _ = register_headers(client)
    connect_csv(client, headers); connect_csv(client, headers)
    sid3 = _session(client, headers).json()["session_id"]
    r = _upload(client, headers, sid3)
    assert r.status_code == 403
    b = r.json()
    assert b["error_code"] == "QUOTA_EXCEEDED" and b["quota"] == "datasets" and b["limit"] == 2 and b["used"] == 2


def test_replacing_a_sessions_own_dataset_does_not_count_twice(client, limits):
    limits(max_datasets=1, max_sessions=50)
    headers, _ = register_headers(client)
    sid = connect_csv(client, headers)
    r = _upload(client, headers, sid, data=b"x,y\n1,2\n", name="b.csv")   # re-upload INTO the same session
    assert r.status_code == 202


def test_storage_bytes_quota_on_upload(client, limits):
    limits(max_dataset_bytes=40_000, max_datasets=50, max_sessions=50)
    headers, email = register_headers(client)
    connect_csv(client, headers)                                  # a small SQLite (tens of KB with profiling tables)
    used = quotas.usage_for_user(SessionLocal(), _user_rows(email)[0].user_id)["dataset_bytes"]
    assert used > 0
    limits(max_dataset_bytes=used + 100, max_datasets=50, max_sessions=50)
    sid2 = _session(client, headers).json()["session_id"]
    r = _upload(client, headers, sid2, data=b"a,b\n" + b"1,2\n" * 200)
    assert r.status_code == 403 and r.json()["quota"] == "storage"
    assert r.json()["limit"] == used + 100


def test_rejected_upload_leaves_no_file_behind(client, limits):
    import storage
    limits(max_datasets=1, max_sessions=50)
    headers, _ = register_headers(client)
    connect_csv(client, headers)
    sid2 = _session(client, headers).json()["session_id"]
    before = set(os.listdir(storage.UPLOAD_DIR))
    assert _upload(client, headers, sid2).status_code == 403
    assert set(os.listdir(storage.UPLOAD_DIR)) == before


def test_sample_database_is_never_counted(client, limits, tmp_path, monkeypatch):
    import api as api_module
    import sqlite3
    sample = str(tmp_path / "retail.db")
    con = sqlite3.connect(sample); con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"); con.execute("INSERT INTO t VALUES (1,'x')"); con.commit(); con.close()
    monkeypatch.setattr(api_module, "SAMPLE_DB", sample)
    limits(max_datasets=1, max_sessions=50)
    headers, email = register_headers(client)
    sid = _session(client, headers).json()["session_id"]
    assert client.post("/api/connect/sample", headers=headers, json={"session_id": sid}).status_code == 200
    assert quotas.usage_for_user(SessionLocal(), _user_rows(email)[0].user_id)["datasets"] == 0
    assert connect_csv(client, headers)                           # still room for one real upload


def test_switching_to_sample_or_flushing_releases_the_quota(client, limits):
    limits(max_datasets=1, max_sessions=50)
    headers, email = register_headers(client)
    sid = connect_csv(client, headers)
    uid = _user_rows(email)[0].user_id
    assert quotas.usage_for_user(SessionLocal(), uid)["datasets"] == 1
    assert client.post("/api/session/flush", headers=headers, json={"session_id": sid}).status_code == 200
    assert quotas.usage_for_user(SessionLocal(), uid) == {"sessions": 1, "datasets": 0, "dataset_bytes": 0}


def test_bytes_recorded_at_ingestion(client):
    headers, email = register_headers(client)
    connect_csv(client, headers)
    row = [r for r in _user_rows(email) if r.db_path][0]
    assert row.dataset_bytes and row.dataset_bytes == os.path.getsize(row.db_path)


# ------------------------------------------------------------------ escape hatch + plans
def test_user_can_free_quota_by_deleting_a_session(client, limits):
    limits(max_datasets=1, max_sessions=50)
    headers, email = register_headers(client)
    sid = connect_csv(client, headers)
    listing = client.get("/api/sessions", headers=headers).json()
    assert listing["quota"]["used"]["datasets"] == 1 and listing["quota"]["limits"]["max_datasets"] == 1
    assert any(s["session_id"] == sid and s["has_dataset"] for s in listing["sessions"])
    assert _upload(client, headers, _session(client, headers).json()["session_id"]).status_code == 403
    assert client.delete(f"/api/session/{sid}", headers=headers).status_code == 200
    assert _upload(client, headers, _session(client, headers).json()["session_id"]).status_code == 202


def test_delete_session_is_owner_only(client):
    a, _ = register_headers(client)
    b, _ = register_headers(client)
    sid = connect_csv(client, a)
    assert client.delete(f"/api/session/{sid}", headers=b).status_code == 404
    assert client.get("/api/sessions", headers=b).json()["sessions"] == []
    assert client.delete(f"/api/session/{sid}", headers=a).status_code == 200


def test_paid_plans_have_higher_limits_and_unknown_plan_is_free():
    f, p = config.plan_quota("free"), config.plan_quota("pro")
    assert all(p[k] > f[k] for k in f)
    assert config.plan_quota("???") == f and config.plan_quota(None) == f


def test_quota_env_override_and_validation():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ok = subprocess.run([sys.executable, "-c", "import config; print(config.plan_quota('free')['max_datasets'], config.plan_quota('free')['max_sessions'])"],
                        env={**os.environ, "QUOTA_LIMITS_JSON": '{"free": {"max_datasets": 9}}'}, capture_output=True, text=True, cwd=root)
    assert ok.stdout.split() == ["9", str(config.QUOTA_LIMITS["free"]["max_sessions"])], ok.stderr
    bad = subprocess.run([sys.executable, "-c", "import config"], env={**os.environ, "QUOTA_LIMITS_JSON": '{"free": {"nope": 1}}'},
                         capture_output=True, text=True, cwd=root)
    assert bad.returncode != 0 and "QUOTA_LIMITS_JSON" in bad.stderr
