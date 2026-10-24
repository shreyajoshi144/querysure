"""
Dataset storage tests (IMP-1 persistence + IMP-2 cleanup).

S3 is exercised against `moto`'s in-memory fake, so the real boto3 code paths
(upload_file / download_file / delete_object / error codes) run -- nothing is
mocked at the storage-module level. Local-filesystem behaviour (the
development fallback) is covered separately.
"""
from __future__ import annotations

from tests.auth_test_utils import verify_registered_email

import io
import json
import os
import sqlite3
import subprocess
import sys
import uuid

import boto3
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

import api as api_module
import storage
from db import PipelineSession, SessionLocal

BUCKET = "qs-test-bucket"
REGION = "us-east-1"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------------ fixtures

@pytest.fixture()
def local_env(tmp_path, monkeypatch):
    """Development fallback: local backend, isolated upload dir."""
    uploads = str(tmp_path / "uploads")
    os.makedirs(uploads)
    monkeypatch.setattr(storage, "BACKEND", "local")
    monkeypatch.setattr(storage, "UPLOAD_DIR", uploads)
    monkeypatch.setattr(storage, "CACHE_DIR", os.path.join(uploads, ".cache"))
    monkeypatch.setattr(api_module, "UPLOAD_DIR", uploads)
    return uploads


@pytest.fixture()
def s3_env(tmp_path, monkeypatch):
    """S3 backend against moto, isolated upload/cache dirs."""
    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
    }.items():
        monkeypatch.setenv(k, v)
    uploads = str(tmp_path / "uploads")
    os.makedirs(uploads)
    with mock_aws():
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        monkeypatch.setattr(storage, "BACKEND", "s3")
        monkeypatch.setattr(storage, "BUCKET", BUCKET)
        monkeypatch.setattr(storage, "PREFIX", "datasets")
        monkeypatch.setattr(storage, "REGION", REGION)
        monkeypatch.setattr(storage, "ENDPOINT_URL", None)
        monkeypatch.setattr(storage, "UPLOAD_DIR", uploads)
        monkeypatch.setattr(storage, "CACHE_DIR", os.path.join(uploads, ".cache"))
        monkeypatch.setattr(api_module, "UPLOAD_DIR", uploads)
        storage._reset_client_cache()
        yield uploads
        storage._reset_client_cache()


def _s3():
    return boto3.client("s3", region_name=REGION)


def _keys() -> list:
    resp = _s3().list_objects_v2(Bucket=BUCKET)
    return sorted(o["Key"] for o in resp.get("Contents", []))


def _wipe_cache():
    """Simulates a container restart/redeploy: local disk is gone."""
    import shutil
    shutil.rmtree(storage.CACHE_DIR, ignore_errors=True)


def _make_sqlite(dir_: str, name: str = None, rows=((1, "a"), (2, "b"))) -> str:
    path = os.path.join(dir_, name or f"{uuid.uuid4()}.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sales (id INTEGER, region TEXT)")
    conn.executemany("INSERT INTO sales VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return path


def _rows(path: str) -> list:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT id, region FROM sales ORDER BY id").fetchall()
    finally:
        conn.close()


def _register(client) -> dict:
    email = f"st-{uuid.uuid4().hex[:10]}@example.com"
    pw = "TestPassword123!"
    assert client.post("/api/auth/register", json={"email": email, "password": pw}).status_code == 200
    verify_registered_email(client, email)
    r = client.post("/api/auth/login", json={"email": email, "password": pw})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _df():
    return pd.DataFrame({"region": ["North", "South", "North", "East"], "amount": [100.0, 200.0, 150.0, 50.0]})


def _payload(kind: str):
    """(filename, bytes) for each supported upload format; table is always `sales`."""
    if kind == "csv":
        return "sales.csv", _df().to_csv(index=False).encode()
    if kind == "json":
        return "sales.json", json.dumps(_df().to_dict(orient="records")).encode()
    if kind in ("xlsx", "xlsm"):
        buf = io.BytesIO()
        _df().to_excel(buf, index=False, engine="openpyxl")
        return f"sales.{kind}", buf.getvalue()
    if kind == "sqlite":
        d = os.path.join(os.path.dirname(storage.UPLOAD_DIR), "src")
        os.makedirs(d, exist_ok=True)
        conn_path = os.path.join(d, "src.sqlite")
        if os.path.exists(conn_path):
            os.remove(conn_path)
        conn = sqlite3.connect(conn_path)
        _df().to_sql("sales", conn, index=False)
        conn.close()
        with open(conn_path, "rb") as f:
            return "sales.sqlite", f.read()
    raise AssertionError(kind)


def _upload(client, headers, kind="csv", session_id=None):
    fname, data = _payload(kind)
    r = client.post(
        "/api/connect/upload",
        headers=headers,
        data={"session_id": session_id} if session_id else None,
        files={"file": (fname, data, "application/octet-stream")},
    )
    return r


SQL = "SELECT region, SUM(amount) AS total FROM sales GROUP BY region ORDER BY region"


def _execute(client, headers, session_id, sql=SQL):
    return client.post("/api/execute-sql", headers=headers, json={"session_id": session_id, "sql": sql})


def _session_row(session_id: str) -> PipelineSession:
    db = SessionLocal()
    try:
        row = db.query(PipelineSession).filter(PipelineSession.id == session_id).first()
        db.expunge(row)
        return row
    finally:
        db.close()


def _set_db_path(session_id: str, value: str):
    db = SessionLocal()
    try:
        row = db.query(PipelineSession).filter(PipelineSession.id == session_id).first()
        row.db_path = value
        db.commit()
    finally:
        db.close()


def _leftovers(uploads: str) -> list:
    """Files sitting in the upload dir (excluding the cache directory)."""
    return sorted(n for n in os.listdir(uploads) if n != ".cache")


# ======================================================== storage: S3 backend

def test_s3_roundtrip_survives_loss_of_local_disk(s3_env):
    src = _make_sqlite(s3_env, "up.db")
    user = str(uuid.uuid4())
    ref = storage.store_dataset(src, user)

    assert ref.startswith(f"s3://{BUCKET}/datasets/{user}/")
    assert not os.path.exists(src), "local temp file should have moved into the cache"
    assert len(_keys()) == 1

    _wipe_cache()  # container restarted / redeployed
    local = storage.ensure_local(ref, user_id=user)
    assert _rows(local) == [(1, "a"), (2, "b")]


def test_cached_copy_is_reused_without_redownload(s3_env):
    user = str(uuid.uuid4())
    ref = storage.store_dataset(_make_sqlite(s3_env), user)
    first = storage.ensure_local(ref, user_id=user)
    # Remove the remote object: a cache hit must not need it.
    _s3().delete_object(Bucket=BUCKET, Key=ref.split(f"s3://{BUCKET}/")[1])
    assert storage.ensure_local(ref, user_id=user) == first


def test_missing_remote_object_is_dataset_unavailable(s3_env):
    user = str(uuid.uuid4())
    ref = storage.store_dataset(_make_sqlite(s3_env), user)
    _s3().delete_object(Bucket=BUCKET, Key=ref.split(f"s3://{BUCKET}/")[1])
    _wipe_cache()
    with pytest.raises(storage.DatasetUnavailable):
        storage.ensure_local(ref, user_id=user)


@pytest.mark.parametrize("bad_ref", [
    f"s3://{BUCKET}/datasets/../../etc/passwd",                                    # traversal
    f"s3://{BUCKET}/other-prefix/{uuid.uuid4()}/{uuid.uuid4()}.db",                # wrong prefix
    f"s3://someone-elses-bucket/datasets/{uuid.uuid4()}/{uuid.uuid4()}.db",        # foreign bucket
    f"s3://{BUCKET}/datasets/{uuid.uuid4()}/not-a-uuid.db",                        # bad layout
    "s3://",                                                                       # malformed
])
def test_ensure_local_rejects_forged_references(s3_env, bad_ref):
    with pytest.raises(storage.DatasetUnavailable):
        storage.ensure_local(bad_ref, user_id=str(uuid.uuid4()))
    assert not storage.is_managed(bad_ref)


def test_ensure_local_refuses_another_users_dataset(s3_env):
    owner, intruder = str(uuid.uuid4()), str(uuid.uuid4())
    ref = storage.store_dataset(_make_sqlite(s3_env), owner)
    with pytest.raises(storage.DatasetUnavailable):
        storage.ensure_local(ref, user_id=intruder)
    assert storage.ensure_local(ref, user_id=owner)  # owner still fine


def test_upload_failure_raises_storage_error_and_keeps_local_file(s3_env, monkeypatch):
    monkeypatch.setattr(storage, "BUCKET", "bucket-that-does-not-exist")
    src = _make_sqlite(s3_env)
    with pytest.raises(storage.StorageError):
        storage.store_dataset(src, str(uuid.uuid4()))
    assert os.path.exists(src), "caller needs the file back to clean it up"


def test_delete_dataset_removes_object_and_cached_copy(s3_env):
    user = str(uuid.uuid4())
    ref = storage.store_dataset(_make_sqlite(s3_env), user)
    cached = storage.ensure_local(ref, user_id=user)
    assert os.path.exists(cached) and len(_keys()) == 1

    assert storage.delete_dataset(ref) is True
    assert _keys() == []
    assert not os.path.exists(cached)


def test_delete_never_touches_unmanaged_files(s3_env, tmp_path):
    outside = _make_sqlite(str(tmp_path), "sample_like.db")           # outside the upload dir
    traversal = os.path.join(s3_env, "..", "sample_like.db")          # ../ escape attempt
    for ref in (outside, traversal, s3_env, "", None):
        assert storage.delete_dataset(ref) is False
    assert os.path.exists(outside)


def test_cache_is_bounded_lru(s3_env, monkeypatch):
    user = str(uuid.uuid4())
    one = os.path.getsize(_make_sqlite(s3_env, "probe.db"))
    monkeypatch.setattr(storage, "CACHE_MAX_BYTES", one * 2 + 10)   # room for ~2 files
    refs = [storage.store_dataset(_make_sqlite(s3_env), user) for _ in range(4)]

    cached = [n for n in os.listdir(storage.CACHE_DIR) if not n.startswith(".tmp-")]
    assert len(cached) <= 2
    # Evicted datasets are still safely recoverable from object storage.
    for ref in refs:
        assert _rows(storage.ensure_local(ref, user_id=user)) == [(1, "a"), (2, "b")]


# ====================================================== storage: local backend

def test_local_backend_is_a_passthrough(local_env):
    src = _make_sqlite(local_env, "keep.db")
    assert storage.store_dataset(src, "u1") == src
    assert os.path.exists(src)
    assert storage.ensure_local(src) == src


def test_local_missing_file_is_dataset_unavailable(local_env):
    with pytest.raises(storage.DatasetUnavailable):
        storage.ensure_local(os.path.join(local_env, "gone.db"))


def test_local_delete_only_inside_upload_dir(local_env, tmp_path):
    inside = _make_sqlite(local_env, "mine.db")
    outside = _make_sqlite(str(tmp_path), "notmine.db")
    assert storage.delete_dataset(inside) is True and not os.path.exists(inside)
    assert storage.delete_dataset(outside) is False and os.path.exists(outside)


# ============================================================ config guards

def _config_import(env_overrides: dict):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("S3_", "STORAGE_", "ENVIRONMENT", "DATABASE_URL", "REDIS_URL", "CORS_", "JWT_", "SMTP_", "EMAIL_", "APP_BASE_URL", "GOOGLE_"))}
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", "import config; print(config.STORAGE_BACKEND)"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
    )


PROD = {
    "ENVIRONMENT": "production",
    "JWT_SECRET_KEY": "x" * 40,
    "DATABASE_URL": "postgresql://u:p@h/db",
    "REDIS_URL": "redis://h:6379/0",
    "CORS_ALLOWED_ORIGINS": "https://app.example.com",
    # production also requires SMTP auth email + Google sign-in config (config.py)
    "SMTP_HOST": "smtp.example.com",
    "SMTP_USERNAME": "u",
    "SMTP_PASSWORD": "p",
    "EMAIL_FROM": "no-reply@example.com",
    "APP_BASE_URL": "https://app.example.com",
    "GOOGLE_CLIENT_ID": "test-client-id",
    "TRUSTED_PROXY_IPS": "10.0.0.0/8",
}


def test_production_refuses_to_start_without_object_storage():
    r = _config_import(PROD)
    assert r.returncode != 0
    assert "Object storage must be configured in production" in r.stderr


def test_production_starts_with_s3_bucket():
    r = _config_import({**PROD, "S3_BUCKET": "my-bucket"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "s3"


def test_s3_backend_requires_bucket_and_backend_is_validated():
    assert "requires S3_BUCKET" in _config_import({"STORAGE_BACKEND": "s3"}).stderr
    assert "must be 'local' or 's3'" in _config_import({"STORAGE_BACKEND": "ftp"}).stderr


def test_development_defaults_to_local():
    r = _config_import({})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "local"


# =========================================================== HTTP: S3 backend

@pytest.mark.parametrize("kind", ["csv", "json", "xlsx", "xlsm", "sqlite"])
def test_every_upload_format_is_persisted_to_object_storage(client, s3_env, kind):
    headers = _register(client)
    r = _upload(client, headers, kind)
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]

    row = _session_row(sid)
    assert row.db_path.startswith(f"s3://{BUCKET}/datasets/{row.user_id}/")
    assert _keys() == [row.db_path.split(f"s3://{BUCKET}/")[1]]
    assert _leftovers(s3_env) == [], "no raw/temporary upload should be left on local disk"

    ex = _execute(client, headers, sid)
    assert ex.status_code == 200, ex.text
    assert {x["region"] for x in ex.json()["rows"]} == {"North", "South", "East"}


def test_dataset_survives_restart_that_wipes_local_disk(client, s3_env):
    headers = _register(client)
    sid = _upload(client, headers, "csv").json()["session_id"]
    before = _execute(client, headers, sid).json()["rows"]

    _wipe_cache()  # redeploy: ./uploads is empty again, Postgres row still points at s3://

    r = _execute(client, headers, sid)
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == before


def test_reupload_deletes_the_superseded_dataset(client, s3_env):
    headers = _register(client)
    sid = _upload(client, headers, "csv").json()["session_id"]
    first_key = _keys()[0]

    r = _upload(client, headers, "json", session_id=sid)
    assert r.status_code == 200 and r.json()["session_id"] == sid
    keys = _keys()
    assert len(keys) == 1 and keys[0] != first_key, "old object must be removed, new one kept"
    assert _execute(client, headers, sid).status_code == 200


def test_switching_to_sample_db_releases_previous_upload_and_spares_the_sample(client, s3_env, tmp_path, monkeypatch):
    sample = _make_sqlite(str(tmp_path), "retail_sample.db")
    monkeypatch.setattr(api_module, "SAMPLE_DB", sample)

    headers = _register(client)
    sid = _upload(client, headers, "csv").json()["session_id"]
    assert len(_keys()) == 1

    r = client.post("/api/connect/sample", headers=headers, json={"session_id": sid})
    assert r.status_code == 200, r.text
    assert _keys() == [], "the replaced upload must be deleted"
    assert os.path.exists(sample), "the bundled sample database must never be deleted"
    assert _session_row(sid).db_path == sample


def test_dataset_still_referenced_by_another_session_is_not_deleted(client, s3_env):
    headers = _register(client)
    sid = _upload(client, headers, "csv").json()["session_id"]
    shared_ref = _session_row(sid).db_path

    other = _register(client)
    other_sid = _upload(client, other, "json").json()["session_id"]
    _set_db_path(other_sid, shared_ref)  # second session now points at the same dataset

    _upload(client, headers, "xlsx", session_id=sid)  # first session replaces its dataset
    assert shared_ref.split(f"s3://{BUCKET}/")[1] in _keys()


def test_storage_outage_on_upload_returns_503_and_leaves_no_trace(client, s3_env, monkeypatch):
    headers = _register(client)
    sid = _upload(client, headers, "csv").json()["session_id"]
    good_ref = _session_row(sid).db_path

    monkeypatch.setattr(storage, "BUCKET", "bucket-that-does-not-exist")
    r = _upload(client, headers, "json", session_id=sid)
    assert r.status_code == 503
    assert r.json()["error_code"] == "STORAGE_UNAVAILABLE"
    assert _leftovers(s3_env) == [], "failed upload must not leave a temp file behind"

    monkeypatch.setattr(storage, "BUCKET", BUCKET)
    assert _session_row(sid).db_path == good_ref, "session must keep its previous dataset"
    assert _execute(client, headers, sid).status_code == 200


def test_missing_object_returns_410_with_clear_error_code(client, s3_env):
    headers = _register(client)
    sid = _upload(client, headers, "csv").json()["session_id"]
    _s3().delete_object(Bucket=BUCKET, Key=_keys()[0])
    _wipe_cache()

    r = _execute(client, headers, sid)
    assert r.status_code == 410
    assert r.json()["error_code"] == "DATASET_UNAVAILABLE"
    assert "upload it again" in r.json()["error"]


class _FailingS3:
    """Stands in for boto3's client when the storage service is misbehaving."""

    def __init__(self, code: str):
        self.code = code

    def download_file(self, *a, **k):
        from botocore.exceptions import ClientError
        raise ClientError({"Error": {"Code": self.code, "Message": "simulated"}}, "GetObject")


@pytest.mark.parametrize("code", ["500", "503", "AccessDenied", "SlowDown"])
def test_transient_storage_failure_is_503_not_treated_as_missing_dataset(client, s3_env, code):
    headers = _register(client)
    sid = _upload(client, headers, "csv").json()["session_id"]
    _wipe_cache()  # force a download so the failing backend is actually hit
    storage._client_singleton = _FailingS3(code)

    r = _execute(client, headers, sid)
    assert r.status_code == 503, r.text
    assert r.json()["error_code"] == "STORAGE_UNAVAILABLE"

    # Backend recovers -> same session works again (dataset was never declared lost).
    storage._reset_client_cache()
    assert _execute(client, headers, sid).status_code == 200


def test_tampered_session_cannot_read_another_users_dataset(client, s3_env):
    alice, bob = _register(client), _register(client)
    alice_sid = _upload(client, alice, "csv").json()["session_id"]
    alice_ref = _session_row(alice_sid).db_path

    bob_sid = _upload(client, bob, "json").json()["session_id"]
    _set_db_path(bob_sid, alice_ref)  # e.g. a corrupted/forged row
    r = _execute(client, bob, bob_sid)
    assert r.status_code == 410, "bob must not be served alice's object"


# ======================================================== HTTP: local fallback

@pytest.mark.parametrize("kind", ["csv", "json", "xlsx", "xlsm", "sqlite"])
def test_local_fallback_upload_and_query_unchanged(client, local_env, kind):
    headers = _register(client)
    r = _upload(client, headers, kind)
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]

    path = _session_row(sid).db_path
    assert not path.startswith("s3://")
    assert os.path.dirname(path) == local_env and os.path.exists(path)
    assert _leftovers(local_env) == [os.path.basename(path)]

    ex = _execute(client, headers, sid)
    assert ex.status_code == 200, ex.text
    assert {x["region"] for x in ex.json()["rows"]} == {"North", "South", "East"}


def test_local_reupload_deletes_superseded_file(client, local_env):
    headers = _register(client)
    sid = _upload(client, headers, "csv").json()["session_id"]
    first = _session_row(sid).db_path

    _upload(client, headers, "json", session_id=sid)
    second = _session_row(sid).db_path
    assert first != second
    assert not os.path.exists(first), "superseded upload must be removed (was orphaned before)"
    assert _leftovers(local_env) == [os.path.basename(second)]


def test_local_missing_file_returns_410_instead_of_raw_500(client, local_env):
    headers = _register(client)
    sid = _upload(client, headers, "csv").json()["session_id"]
    os.remove(_session_row(sid).db_path)  # what a redeploy does to a legacy local path

    r = _execute(client, headers, sid)
    assert r.status_code == 410
    assert r.json()["error_code"] == "DATASET_UNAVAILABLE"


def test_failure_after_upload_validated_does_not_orphan_the_file(local_env, monkeypatch):
    def boom(_path):
        raise RuntimeError("schema scan blew up")

    import jobs as jobs_module
    monkeypatch.setattr(jobs_module, "load_database", boom)
    monkeypatch.setattr(jobs_module.time, "sleep", lambda _s: None)
    with TestClient(api_module.app, raise_server_exceptions=False) as c:
        headers = _register(c)
        sid = c.post("/api/session", headers=headers, json={}).json()["session_id"]
        r = _upload(c, headers, "csv", session_id=sid)
        # Sprint 2: ingestion runs in the job; the API answers 202 and the *job* fails.
        assert r.status_code == 202
        job = c.get(f"/api/jobs/{r.json()['job_id']}", headers=headers).json()
        assert job["status"] == "failed"
    assert _leftovers(local_env) == [], "failed processing used to leave the .db behind forever"
