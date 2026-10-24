from __future__ import annotations

import json

from db import Job, SessionLocal


def test_upload_returns_202_and_job_is_owner_scoped(client, auth_headers):
    import io
    # Use the raw client to assert the real product contract rather than the
    # compatibility wrapper used by legacy synchronous-upload tests.
    session = client.raw_post("/api/session", headers=auth_headers, json={})
    sid = session.json()["session_id"]
    response = client.raw_post(
        "/api/connect/upload",
        headers=auth_headers,
        data={"session_id": sid},
        files={"file": ("sample.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    job = client.get(f"/api/jobs/{job_id}", headers=auth_headers)
    assert job.status_code == 200
    assert job.json()["job_id"] == job_id


def test_job_is_not_visible_to_another_user(client, auth_headers):
    import uuid
    from tests.auth_test_utils import verify_registered_email
    email = f"other-{uuid.uuid4().hex[:10]}@example.com"
    password = "OtherPassword123!"
    r = client.post("/api/auth/register", json={"email": email, "password": password})
    assert r.status_code == 200
    verify_registered_email(client, email)
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    other_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    session = client.raw_post("/api/session", headers=auth_headers, json={})
    sid = session.json()["session_id"]
    import io
    r = client.raw_post("/api/connect/upload", headers=auth_headers, data={"session_id": sid}, files={"file": ("x.csv", io.BytesIO(b"a\n1\n"), "text/csv")})
    job_id = r.json()["job_id"]
    assert client.get(f"/api/jobs/{job_id}", headers=other_headers).status_code == 404


def _job_db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from db import Base, User
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    user_id = "job-user"
    db.add(User(id=user_id, email="job-user@example.com", hashed_password="x", email_verified=True, auth_version=0))
    db.commit()
    return engine, Session, db, user_id


def test_worker_success_state_transition_and_idempotency(tmp_path, monkeypatch):
    import jobs as jobs_module
    from db import Job
    engine, Session, db, user_id = _job_db(tmp_path)
    job = Job(id="job-success", user_id=user_id, kind="auth_email", status="queued")
    db.add(job); db.commit()
    calls = []
    monkeypatch.setattr(jobs_module, "SessionLocal", Session)
    monkeypatch.setattr(jobs_module, "_dispatch", lambda db, job, payload: calls.append(job.id) or {"sent": True})
    jobs_module._execute_job(job.id, {"to": "a@example.com"})
    jobs_module._execute_job(job.id, {"to": "a@example.com"})
    check = Session().query(Job).filter_by(id=job.id).one()
    assert check.status == "succeeded"
    assert calls == [job.id]
    engine.dispose()


def test_worker_failure_retries_three_attempts_and_persists_safe_error(tmp_path, monkeypatch):
    import jobs as jobs_module
    from db import Job
    engine, Session, db, user_id = _job_db(tmp_path)
    job = Job(id="job-fail", user_id=user_id, kind="auth_email", status="queued")
    db.add(job); db.commit()
    calls = []
    sleeps = []
    def fail(db, job, payload):
        calls.append(1)
        raise RuntimeError("secret stack trace must not escape")
    monkeypatch.setattr(jobs_module, "SessionLocal", Session)
    monkeypatch.setattr(jobs_module, "_dispatch", fail)
    monkeypatch.setattr(jobs_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    jobs_module._execute_job(job.id, {})
    check = Session().query(Job).filter_by(id=job.id).one()
    assert check.status == "failed"
    assert check.error_code == "JOB_FAILED"
    assert json.loads(check.result_ref)["error"] == "The background operation could not be completed. Please try again."
    assert len(calls) == 3
    assert sleeps == [1, 2]
    engine.dispose()


def test_worker_retry_eventually_succeeds(tmp_path, monkeypatch):
    import jobs as jobs_module
    from db import Job
    engine, Session, db, user_id = _job_db(tmp_path)
    job = Job(id="job-retry", user_id=user_id, kind="auth_email", status="queued")
    db.add(job); db.commit()
    attempts = []
    sleeps = []
    def flaky(db, job, payload):
        attempts.append(1)
        if len(attempts) < 3:
            raise ValueError("temporary")
        return {"sent": True}
    monkeypatch.setattr(jobs_module, "SessionLocal", Session)
    monkeypatch.setattr(jobs_module, "_dispatch", flaky)
    monkeypatch.setattr(jobs_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    jobs_module._execute_job(job.id, {})
    check = Session().query(Job).filter_by(id=job.id).one()
    assert check.status == "succeeded"
    assert len(attempts) == 3
    assert sleeps == [1, 2]
    engine.dispose()


def test_upload_job_contract_has_durable_object_reference_not_api_path(client, auth_headers, monkeypatch, tmp_path):
    import io
    import api as api_module
    import storage
    captured = {}
    session = client.raw_post("/api/session", headers=auth_headers, json={})
    sid = session.json()["session_id"]
    def stage(path, user_id, ext):
        captured["staged_input"] = path
        captured["object_ref"] = f"s3://bucket/staging/{user_id}/00000000-0000-0000-0000-000000000000{ext}"
        return captured["object_ref"]
    def enqueue(db, user_id, kind, payload):
        captured["payload"] = payload
        class JobObj:
            id = "job-contract"
        return JobObj()
    monkeypatch.setattr(storage, "stage_upload", stage)
    monkeypatch.setattr(api_module, "enqueue_job", enqueue)
    monkeypatch.setattr(storage, "delete_staged", lambda *args, **kwargs: True)
    r = client.raw_post("/api/connect/upload", headers=auth_headers, data={"session_id": sid}, files={"file": ("x.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")})
    assert r.status_code == 202
    assert "object_ref" in captured["payload"]
    assert captured["payload"]["object_ref"].startswith("s3://")
    assert "raw_path" not in captured["payload"]
    assert not str(captured["payload"]["object_ref"]).startswith(str(tmp_path))


def test_frontend_upload_uses_completed_job_result():
    from pathlib import Path
    js = Path("static/js/app.js").read_text()
    assert "renderSchemaList(completed.tables)" in js
    assert "stageDone('connect', completed)" in js   # records the completed job response as the Connect stage detail
    assert "renderSchemaList(res.tables)" not in js[js.index("async function uploadFile"):js.index("function throwApiError")]


def test_auth_email_worker_sends_through_smtp_capture(tmp_path, monkeypatch):
    import jobs as jobs_module
    from db import Job
    from tests.conftest import SMTP_CAPTURE

    engine, Session, db, user_id = _job_db(tmp_path)
    job = Job(id="job-auth-email-smtp", user_id=user_id, kind="auth_email", status="queued")
    db.add(job)
    db.commit()
    monkeypatch.setattr(jobs_module, "SessionLocal", Session)

    jobs_module._execute_job(job.id, {
        "to": "worker-capture@example.com",
        "subject": "Verify your QuerySure email",
        "text": "Open http://localhost:5000/#verify_token=worker-token-123",
    })

    check = Session().query(Job).filter_by(id=job.id).one()
    assert check.status == "succeeded"
    messages = SMTP_CAPTURE.messages()
    assert any(
        "worker-capture@example.com" in message["recipients"]
        and "verify_token=worker-token-123" in message["data"]
        for message in messages
    )
    engine.dispose()
