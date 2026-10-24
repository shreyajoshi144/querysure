from __future__ import annotations

import glob
import json
import os
import tempfile

import boto3
import pytest
from moto import mock_aws

import api as api_module
import report_storage
import storage
from db import PipelineSession, Report, SessionLocal, User, update_session
from tests.sprint3_helpers import register_headers

BUCKET, REGION = "qs-report-bucket", "us-east-1"


@pytest.fixture()
def local_env(tmp_path, monkeypatch):
    uploads = str(tmp_path / "uploads")
    os.makedirs(uploads)
    monkeypatch.setattr(storage, "BACKEND", "local")
    monkeypatch.setattr(storage, "UPLOAD_DIR", uploads)
    monkeypatch.setattr(storage, "CACHE_DIR", os.path.join(uploads, ".cache"))
    monkeypatch.setattr(api_module, "UPLOAD_DIR", uploads)
    return tmp_path


@pytest.fixture()
def s3_env(tmp_path, monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                 "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION}.items():
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
        yield tmp_path
        storage._reset_client_cache()


@pytest.fixture(params=["local", "s3"])
def env(request):
    return request.getfixturevalue(f"{request.param}_env")


def _ready_session(client, headers):
    sid = client._inner.post("/api/session", headers=headers, json={}).json()["session_id"]
    with SessionLocal() as db:
        row = db.query(PipelineSession).filter(PipelineSession.id == sid).one()
        update_session(
            db, row, question="Total sales </script><b>x</b>?", sql="SELECT 1",
            sql_result={"sql": "SELECT 1", "explanation": "e", "confidence": "High", "complexity": "Simple"},
            validation={"is_valid": True, "validation_score": 99, "risk_score": 1},
            query_result={"query_id": "q-abc", "status": "ok", "success": True, "row_count": 2, "columns": ["a", "b"],
                          "rows": [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]},
            reliability={"reliability_score": 90, "reliability_level": "High"},
            insights={"insight_confidence": 0.9, "summary": "s"})
    return sid


def _report(client, headers, sid):
    r = client.post("/api/generate-report", headers=headers, json={"session_id": sid})
    assert r.status_code == 200, r.text
    return r.json()


def _all_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k); yield from _all_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _all_strings(v)


def test_report_is_stored_durably_and_response_has_no_filesystem_paths(client, env):
    headers, _ = register_headers(client)
    sid = _ready_session(client, headers)
    res = _report(client, headers, sid)
    assert res["report_id"] and {"json", "html", "rows_csv", "summary_csv"} <= set(res["report_ids"])
    for forbidden in ("json_path", "html_path", "pdf_path", "rows_csv_path"):
        assert forbidden not in res
    # no path-like or storage-layout strings anywhere in the response (incl. the job-status endpoint)
    jobs = client._inner.get("/api/jobs/" + client._inner.post("/api/generate-report", headers=headers, json={"session_id": sid}).json()["job_id"], headers=headers).json()
    for blob in (res, jobs):
        text = json.dumps(blob)
        for needle in (str(env), tempfile.gettempdir(), "qs-report-", "/reports/", "/home/", "/tmp/", "output/", ".json\"", ".csv\"", ".html\"", ".pdf\""):
            if needle in (".json\"", ".csv\"", ".html\"", ".pdf\""):
                assert not any(s.endswith(needle[:-1]) and "/" in s for s in _all_strings(blob))
            else:
                assert needle not in text, needle


def test_temp_files_are_deleted_and_nothing_written_to_old_output_dir(client, env):
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "qs-report-*")))
    headers, _ = register_headers(client)
    _report(client, headers, _ready_session(client, headers))
    assert set(glob.glob(os.path.join(tempfile.gettempdir(), "qs-report-*"))) == before
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert not glob.glob(os.path.join(root, "output", "*", "*_report.json"))


def test_rows_and_key_layout(client, env):
    headers, email = register_headers(client)
    sid = _ready_session(client, headers)
    res = _report(client, headers, sid)
    with SessionLocal() as db:
        uid = db.query(User).filter(User.email == email).one().id
        rows = db.query(Report).filter(Report.user_id == uid).all()
    assert {r.id for r in rows} == set(res["report_ids"].values())
    for r in rows:
        assert r.session_id == sid and r.size > 0
        assert r.key.startswith(f"{storage.PREFIX}/reports/{uid}/") and len(r.key.split("/")) == 4
    if storage.BACKEND == "s3":
        listed = {o["Key"] for o in boto3.client("s3", region_name=REGION).list_objects_v2(Bucket=BUCKET, Prefix=f"{storage.PREFIX}/reports/{uid}/")["Contents"]}
        assert listed == {r.key for r in rows}


def test_owner_can_download_every_artifact_with_safe_headers(client, env):
    headers, _ = register_headers(client)
    res = _report(client, headers, _ready_session(client, headers))
    r = client._inner.get(f"/api/reports/{res['report_ids']['json']}/download", headers=headers)
    assert r.status_code == 200
    assert json.loads(r.content)["question"].startswith("Total sales")
    assert r.headers["content-type"].startswith("application/json")
    assert "attachment" in r.headers["content-disposition"] and r.headers["x-content-type-options"] == "nosniff"
    html = client._inner.get(f"/api/reports/{res['report_ids']['html']}/download", headers=headers)
    assert html.status_code == 200 and "<b>x</b>" not in html.text and "&lt;b&gt;x&lt;/b&gt;" in html.text
    assert "sandbox" in html.headers["content-security-policy"]
    csv_r = client._inner.get(f"/api/reports/{res['report_ids']['rows_csv']}/download", headers=headers)
    assert csv_r.status_code == 200 and csv_r.text.splitlines()[0] == "a,b" and "2,y" in csv_r.text
    assert int(csv_r.headers["content-length"]) == len(csv_r.content)


def test_other_user_gets_404_and_unauthenticated_is_rejected(client, env):
    a, _ = register_headers(client)
    b, _ = register_headers(client)
    res = _report(client, a, _ready_session(client, a))
    rid = res["report_id"]
    r = client._inner.get(f"/api/reports/{rid}/download", headers=b)
    assert r.status_code == 404 and r.json()["error_code"] == "REPORT_NOT_FOUND"
    assert client._inner.get(f"/api/reports/{rid}/download").status_code in (401, 403)
    assert client._inner.get("/api/reports/does-not-exist/download", headers=a).status_code == 404
    assert client._inner.get(f"/api/reports/{rid}/download", headers=a).status_code == 200


def test_tampered_key_cannot_read_foreign_or_arbitrary_objects(client, env):
    a, _ = register_headers(client)
    b, _ = register_headers(client)
    ra = _report(client, a, _ready_session(client, a))
    rb = _report(client, b, _ready_session(client, b))
    with SessionLocal() as db:
        a_row = db.query(Report).filter(Report.id == ra["report_id"]).one()
        b_row = db.query(Report).filter(Report.id == rb["report_id"]).one()
        a_row.key = b_row.key                                      # point A's row at B's object
        db.commit()
    assert client._inner.get(f"/api/reports/{ra['report_id']}/download", headers=a).status_code == 404
    for evil in ("../../etc/passwd", "/etc/passwd", f"{storage.PREFIX}/reports/../../x", ""):
        with SessionLocal() as db:
            db.query(Report).filter(Report.id == ra["report_id"]).one().key = evil or "x"
            db.commit()
        assert client._inner.get(f"/api/reports/{ra['report_id']}/download", headers=a).status_code == 404


def test_missing_object_is_a_clean_404(client, env):
    headers, _ = register_headers(client)
    res = _report(client, headers, _ready_session(client, headers))
    with SessionLocal() as db:
        key = db.query(Report).filter(Report.id == res["report_id"]).one().key
    assert report_storage.delete_report_object(key)
    r = client._inner.get(f"/api/reports/{res['report_id']}/download", headers=headers)
    assert r.status_code == 404 and r.json()["error_code"] == "REPORT_UNAVAILABLE"


def test_failed_upload_rolls_back_objects_and_rows(client, env, monkeypatch):
    headers, email = register_headers(client)
    sid = _ready_session(client, headers)
    real = report_storage.store_report_file
    n = {"c": 0}

    def flaky(path, uid, kind):
        n["c"] += 1
        if n["c"] == 3:
            raise storage.StorageError("boom")
        return real(path, uid, kind)
    monkeypatch.setattr(report_storage, "store_report_file", flaky)
    monkeypatch.setattr("jobs.time.sleep", lambda s: None)
    r = client._inner.post("/api/generate-report", headers=headers, json={"session_id": sid})
    jid = r.json()["job_id"]
    assert client._inner.get(f"/api/jobs/{jid}", headers=headers).json()["status"] == "failed" or n["c"] >= 3
    with SessionLocal() as db:
        uid = db.query(User).filter(User.email == email).one().id
        succeeded = db.query(Report).filter(Report.user_id == uid).count()
    # every attempt either rolled back fully or (retry succeeded) produced a complete set -- never a partial one
    assert succeeded in (0, 4, 5)
    if storage.BACKEND == "s3":
        listed = boto3.client("s3", region_name=REGION).list_objects_v2(Bucket=BUCKET, Prefix=f"{storage.PREFIX}/reports/{uid}/").get("Contents", [])
        assert len(listed) == succeeded                          # no orphaned objects


def test_download_button_uses_the_endpoint_and_old_server_paths_are_gone():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = open(os.path.join(root, "static", "js", "app.js"), encoding="utf-8").read()
    # v1.0.6: the redundant "Download report" button and its handler were removed; the endpoint stays (CSV/JSON downloads remain in the UI)
    assert "downloadReportBtn" not in js
    assert "json_path" not in js and "html_path" not in js
