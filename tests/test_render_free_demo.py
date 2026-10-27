"""Render free demo mode: uploads and reports finish without a separate worker, or fail clearly -- never "queued" forever.

On Render's free plan there is no background worker (a paid service), so before this change every upload and report job sat
in RQ with nothing to consume it until the 25-minute stale reaper failed it. Now, with no worker heartbeat, those jobs run in
the web process after the response (JOB_EXECUTION=auto/in_process). They are not durable: a job lost in a restart (or queued
for a worker that never comes) is failed as JOB_INTERRUPTED once its heartbeat is older than JOB_ORPHAN_SECONDS.
Redis is fakeredis, S3 is moto; nothing touches the network.
"""
from __future__ import annotations

import io
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import boto3
import fakeredis
import pytest
from moto import mock_aws

import api as api_module
import config
import jobs
import quotas
import storage
from db import Job, PipelineSession, SessionLocal, User, update_session
from tests.sprint3_helpers import register_headers

ROOT = Path(__file__).resolve().parent.parent
BUCKET, REGION = "qs-demo-bucket", "us-east-1"
CSV = b"region,amount\nNorth,10\nSouth,20\nNorth,5\n"


@pytest.fixture()
def s3(tmp_path, monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                 "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    uploads = str(tmp_path / "uploads")
    os.makedirs(uploads)
    with mock_aws():
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        for name, value in {"BACKEND": "s3", "BUCKET": BUCKET, "PREFIX": "datasets", "REGION": REGION, "ENDPOINT_URL": None,
                            "UPLOAD_DIR": uploads, "CACHE_DIR": os.path.join(uploads, ".cache")}.items():
            monkeypatch.setattr(storage, name, value)
        monkeypatch.setattr(api_module, "UPLOAD_DIR", uploads)
        storage._reset_client_cache()
        yield tmp_path
        storage._reset_client_cache()


@pytest.fixture(autouse=True)
def _pause_maintenance_thread(monkeypatch):
    """The app's maintenance thread (started with the TestClient, every 30 s) also fails orphaned jobs. These tests age jobs
    on purpose, so it is paused here: otherwise it can fail a job between a test's setup and its check (seen in CI)."""
    from observability import alerts
    monkeypatch.setattr(alerts, "run_once_if_leader", lambda: None)


class _Queue:
    """Stands in for rq.Queue: records what would have gone to the worker."""
    enqueued: list = []

    def __init__(self, *a, **k):
        pass

    def enqueue(self, fn, *args, **kwargs):
        _Queue.enqueued.append(args)


@pytest.fixture()
def redis_up(monkeypatch):
    """Production-like: Redis reachable (so the old code would hand the job to RQ), RQ itself recorded, not run."""
    server = fakeredis.FakeServer()
    monkeypatch.setattr(jobs, "_redis", lambda: fakeredis.FakeRedis(server=server))
    _Queue.enqueued = []
    monkeypatch.setattr(jobs, "Queue", _Queue)
    monkeypatch.setattr(config, "JOB_EXECUTION", "auto")
    return _Queue


@pytest.fixture()
def no_worker(monkeypatch, redis_up):
    monkeypatch.setattr(jobs, "worker_alive", lambda: False)
    return redis_up


def _session(client, headers):
    return client._inner.post("/api/session", headers=headers, json={}).json()["session_id"]


def _upload(client, headers, sid, body=CSV, name="sales.csv"):
    return client.raw_post("/api/connect/upload", headers=headers, data={"session_id": sid},
                           files={"file": (name, io.BytesIO(body), "text/csv")})


def _job(job_id):
    with SessionLocal() as db:
        return db.query(Job).filter(Job.id == job_id).one()


def _age(job_id, seconds):
    with SessionLocal() as db:
        job = db.query(Job).filter(Job.id == job_id).one()
        job.updated_at = jobs._now() - timedelta(seconds=seconds)
        db.commit()


def _new_job(user_id, kind="dataset_ingestion", status="queued", reserved=1234, session_id=None):
    import uuid
    with SessionLocal() as db:
        job = Job(id=str(uuid.uuid4()), user_id=user_id, kind=kind, status=status, created_at=jobs._now(),
                  updated_at=jobs._now(), reserved_bytes=reserved if kind == "dataset_ingestion" else None,
                  session_id=session_id)
        db.add(job)
        db.commit()
        return job.id


def _user_id(email):
    with SessionLocal() as db:
        return db.query(User).filter(User.email == email.lower()).one().id


# --------------------------------------------------------------------------------------------- uploads without a worker
def test_upload_completes_in_the_web_process_when_no_worker_is_running(client, s3, no_worker):
    headers, email = register_headers(client, "demo-up")
    sid = _session(client, headers)
    r = _upload(client, headers, sid)
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert no_worker.enqueued == [], "nothing may be handed to a worker that does not exist"
    job = client.get(f"/api/jobs/{job_id}", headers=headers).json()
    assert job["status"] == "succeeded", job
    assert job["result"]["table_count"] == 1 and job["result"]["total_rows"] == 3
    with SessionLocal() as db:
        row = db.query(PipelineSession).filter(PipelineSession.id == sid).one()
        assert row.user_id == _user_id(email) and row.db_path.startswith("s3://")
    assert job_id not in jobs._inflight


def test_the_job_runs_after_the_response_not_inside_the_request(client, s3, no_worker):
    headers, email = register_headers(client, "demo-order")
    uid = _user_id(email)

    class Tasks:
        def __init__(self):
            self.tasks = []

        def add_task(self, fn, *args):
            self.tasks.append((fn, args))

    tasks = Tasks()
    with SessionLocal() as db:
        job = jobs.enqueue_job(db, uid, "report_generation", {"session_id": "missing"}, tasks)
    assert _job(job.id).status == "queued" and len(tasks.tasks) == 1     # the request returns with the job still queued
    assert job.id in jobs._inflight
    fn, args = tasks.tasks[0]
    fn(*args)                                                            # what FastAPI runs after sending the response
    assert _job(job.id).status == "failed"                               # (no such session: a real, recorded outcome)
    assert job.id not in jobs._inflight


def test_report_completes_in_the_web_process_and_stays_owner_scoped(client, s3, no_worker):
    headers, _ = register_headers(client, "demo-rep")
    sid = _session(client, headers)
    with SessionLocal() as db:
        row = db.query(PipelineSession).filter(PipelineSession.id == sid).one()
        update_session(db, row, question="Total amount?", sql="SELECT 1",
                       sql_result={"sql": "SELECT 1", "explanation": "e", "confidence": "High", "complexity": "Simple"},
                       validation={"is_valid": True, "validation_score": 99, "risk_score": 1},
                       query_result={"query_id": "q", "status": "ok", "success": True, "row_count": 1, "columns": ["a"],
                                     "rows": [{"a": 1}]},
                       reliability={"reliability_score": 90, "reliability_level": "High"},
                       insights={"insight_confidence": 0.9, "summary": "s"})
    r = client.raw_post("/api/generate-report", headers=headers, json={"session_id": sid})
    assert r.status_code == 202
    job = client.get(f"/api/jobs/{r.json()['job_id']}", headers=headers).json()
    assert job["status"] == "succeeded" and no_worker.enqueued == []
    report_id = job["result"]["report_ids"]["json"]
    assert client._inner.get(f"/api/reports/{report_id}/download", headers=headers).status_code == 200
    other, _ = register_headers(client, "demo-rep-other")
    assert client._inner.get(f"/api/reports/{report_id}/download", headers=other).status_code == 404
    assert client.get(f"/api/jobs/{r.json()['job_id']}", headers=other).status_code == 404


def test_a_live_worker_still_gets_the_job(client, s3, redis_up, monkeypatch):
    monkeypatch.setattr(jobs, "worker_alive", lambda: False)             # sign-up e-mail goes out in-process
    headers, _ = register_headers(client, "demo-q")
    monkeypatch.setattr(jobs, "worker_alive", lambda: True)
    r = _upload(client, headers, _session(client, headers))
    assert r.status_code == 202 and len(redis_up.enqueued) == 1
    assert _job(r.json()["job_id"]).status == "queued"


def test_heavy_in_process_jobs_run_one_at_a_time(monkeypatch):
    monkeypatch.setattr(jobs, "_heavy_slots", {"sem": None})
    monkeypatch.setattr(config, "IN_PROCESS_JOB_CONCURRENCY", 1)
    running, peak, lock = [0], [0], threading.Lock()

    def fake_execute(job_id, payload):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1

    monkeypatch.setattr(jobs, "_execute_job", fake_execute)
    threads = [threading.Thread(target=jobs._run_in_process, args=(f"j{i}", {}, "dataset_ingestion")) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1


# ------------------------------------------------------------------------------------ never "queued" forever: fail clearly
def test_a_job_lost_in_a_restart_is_failed_clearly_and_releases_its_quota(client, no_worker):
    headers, email = register_headers(client, "demo-lost")
    uid = _user_id(email)
    job_id = _new_job(uid, status="running", reserved=5000)
    with SessionLocal() as db:
        assert quotas.usage_for_user(db, uid)["dataset_bytes"] == 5000
    body = client.get(f"/api/jobs/{job_id}", headers=headers).json()
    assert body["status"] == "running"                                   # fresh heartbeat: still in progress
    _age(job_id, config.JOB_ORPHAN_SECONDS + 5)                          # the process that ran it is gone
    body = client.get(f"/api/jobs/{job_id}", headers=headers).json()
    assert body["status"] == "failed" and body["error_code"] == "JOB_INTERRUPTED"
    assert "try again" in body["error"].lower()
    with SessionLocal() as db:
        assert quotas.usage_for_user(db, uid)["dataset_bytes"] == 0


def test_queue_mode_without_a_worker_fails_instead_of_waiting_forever(client, s3, redis_up, monkeypatch):
    monkeypatch.setattr(config, "JOB_EXECUTION", "queue")
    monkeypatch.setattr(jobs, "worker_alive", lambda: False)
    headers, _ = register_headers(client, "demo-qmode")
    r = _upload(client, headers, _session(client, headers))
    job_id = r.json()["job_id"]
    assert len(redis_up.enqueued) == 1 and client.get(f"/api/jobs/{job_id}", headers=headers).json()["status"] == "queued"
    _age(job_id, config.JOB_ORPHAN_SECONDS + 1)
    assert client.get(f"/api/jobs/{job_id}", headers=headers).json()["error_code"] == "JOB_INTERRUPTED"


def test_the_maintenance_reaper_fails_orphans_without_anyone_polling(client, no_worker):
    _, email = register_headers(client, "demo-reap")
    job_id = _new_job(_user_id(email))
    _age(job_id, config.JOB_ORPHAN_SECONDS + 1)
    assert jobs.reap_stale_jobs() >= 1
    assert _job(job_id).error_code == "JOB_INTERRUPTED"


@pytest.mark.parametrize("case", ["worker_alive", "in_this_process", "fresh_heartbeat", "finished"])
def test_live_or_finished_jobs_are_never_failed(client, no_worker, monkeypatch, case):
    _, email = register_headers(client, f"demo-keep-{case[:5]}")
    job_id = _new_job(_user_id(email), status="succeeded" if case == "finished" else "running")
    if case != "fresh_heartbeat":
        _age(job_id, config.JOB_ORPHAN_SECONDS + 60)
    if case == "worker_alive":
        monkeypatch.setattr(jobs, "worker_alive", lambda: True)
    if case == "in_this_process":
        monkeypatch.setattr(jobs, "_inflight", {job_id: time.monotonic()})
    with SessionLocal() as db:
        assert jobs.fail_orphaned_jobs(db, job_id=job_id) == 0
    assert _job(job_id).status in {"running", "succeeded"}


def test_the_heartbeat_does_not_share_time_sleep_with_job_retries(monkeypatch):
    # In CI a test that records jobs.time.sleep (retry back-off) also caught this thread's 15 s waits: [1, 2, 15, 15, ...].
    sleeps, touches = [], []
    monkeypatch.setattr(jobs.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(config, "JOB_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(jobs, "touch_inflight_jobs", lambda: touches.append(1) or 0)
    stop = threading.Event()
    t = threading.Thread(target=jobs._heartbeat_loop, args=(stop,))
    t.start()
    threading.Event().wait(0.2)                       # not time.sleep: it is patched (time is one shared module)
    stop.set()
    t.join(2)
    assert not t.is_alive() and len(touches) >= 2 and sleeps == []


def test_heartbeat_keeps_an_in_process_job_alive(client, no_worker, monkeypatch):
    _, email = register_headers(client, "demo-hb")
    job_id = _new_job(_user_id(email), status="running")
    _age(job_id, config.JOB_ORPHAN_SECONDS - 5)
    monkeypatch.setattr(jobs, "_inflight", {job_id: time.monotonic()})
    assert jobs.touch_inflight_jobs() == 1
    monkeypatch.setattr(jobs, "_inflight", {})                         # another process now looks at it
    with SessionLocal() as db:
        assert jobs.fail_orphaned_jobs(db, job_id=job_id) == 0
    assert _job(job_id).status == "running"


def test_a_task_that_never_started_stops_getting_heartbeats(client, no_worker, monkeypatch):
    # e.g. the client disconnected before the response, so Starlette never ran the background task
    _, email = register_headers(client, "demo-never")
    job_id = _new_job(_user_id(email))
    monkeypatch.setattr(jobs, "_inflight", {job_id: time.monotonic() - config.JOB_STALE_SECONDS - 1})
    assert jobs.touch_inflight_jobs() == 0
    _age(job_id, config.JOB_ORPHAN_SECONDS + 1)
    with SessionLocal() as db:
        assert jobs.fail_orphaned_jobs(db, job_id=job_id) == 1


def test_another_users_poll_cannot_touch_my_job(client, no_worker):
    _, email = register_headers(client, "demo-iso")
    job_id = _new_job(_user_id(email))
    _age(job_id, config.JOB_ORPHAN_SECONDS + 5)
    other, _ = register_headers(client, "demo-iso-other")
    assert client.get(f"/api/jobs/{job_id}", headers=other).status_code == 404
    assert _job(job_id).status == "queued"


def test_an_interrupted_job_is_not_resurrected_by_a_late_worker(client, no_worker, monkeypatch):
    _, email = register_headers(client, "demo-late")
    job_id = _new_job(_user_id(email))
    _age(job_id, config.JOB_ORPHAN_SECONDS + 5)
    with SessionLocal() as db:
        assert jobs.fail_orphaned_jobs(db, job_id=job_id) == 1
    deleted, ran = [], []
    monkeypatch.setattr(storage, "delete_staged", lambda ref, user_id=None: deleted.append(ref) or True)
    monkeypatch.setattr(jobs, "_dispatch", lambda *a: ran.append(1))
    jobs._execute_job(job_id, {"object_ref": "s3://b/staging/x.csv"})
    assert ran == [] and deleted == ["s3://b/staging/x.csv"] and _job(job_id).error_code == "JOB_INTERRUPTED"


def _interrupt(job_id):
    """What the orphan check / stale reaper does from another process while the job is still running."""
    with SessionLocal() as db:
        db.query(Job).filter(Job.id == job_id).update(
            {Job.status: "failed", Job.error_code: "JOB_INTERRUPTED",
             Job.result_ref: json.dumps({"error": jobs.INTERRUPTED_MESSAGE})}, synchronize_session=False)
        db.commit()


def test_a_late_success_never_overwrites_a_reported_failure(client, monkeypatch):
    _, email = register_headers(client, "demo-race")
    job_id = _new_job(_user_id(email), kind="report_generation")

    def slow_job(db, job, payload):
        _interrupt(job.id)                          # the user was told "failed, try again" while this still ran
        return {"report_id": "x"}

    monkeypatch.setattr(jobs, "_dispatch", slow_job)
    assert jobs._execute_job(job_id, {}) is None
    job = _job(job_id)
    assert job.status == "failed" and job.error_code == "JOB_INTERRUPTED"
    assert json.loads(job.result_ref)["error"] == jobs.INTERRUPTED_MESSAGE


def test_a_late_upload_does_not_switch_the_dataset_or_leave_objects(client, s3, no_worker, monkeypatch):
    headers, _ = register_headers(client, "demo-race-up")
    sid = _session(client, headers)
    real_ingest = jobs.ingest_to_sqlite

    def ingest_then_get_interrupted(*args, **kwargs):
        out = real_ingest(*args, **kwargs)
        with SessionLocal() as db:
            job = db.query(Job).filter(Job.session_id == sid, Job.kind == "dataset_ingestion").one()
        _interrupt(job.id)
        return out

    monkeypatch.setattr(jobs, "ingest_to_sqlite", ingest_then_get_interrupted)
    r = _upload(client, headers, sid)
    body = client.get(f"/api/jobs/{r.json()['job_id']}", headers=headers).json()
    assert body["status"] == "failed" and body["error_code"] == "JOB_INTERRUPTED"
    with SessionLocal() as db:
        assert db.query(PipelineSession).filter(PipelineSession.id == sid).one().db_path is None
    listed = boto3.client("s3", region_name=REGION).list_objects_v2(Bucket=BUCKET)
    assert listed.get("KeyCount", 0) == 0, [o["Key"] for o in listed.get("Contents", [])]


# --------------------------------------------------------------------------------------------------------- upload limit
def test_default_upload_limit_is_10_mb_for_every_format(client):
    assert config.MAX_UPLOAD_MB == 10
    assert set(api_module.UPLOAD_SIZE_LIMITS.values()) == {10 * 1024 * 1024}
    headers, _ = register_headers(client, "demo-big")
    r = _upload(client, headers, _session(client, headers), body=b"a\n" + b"1\n" * (5 * 1024 * 1024 + 1))
    assert r.status_code == 413 and "10 MB" in r.json()["error"]


def test_a_file_exactly_at_the_limit_is_accepted(client):
    # Content-Length includes the multipart framing; only the file's own bytes count against the limit.
    headers, _ = register_headers(client, "demo-edge")
    r = client.raw_post("/api/connect/upload", headers=headers, data={"session_id": _session(client, headers)},
                        files={"file": ("edge.db", io.BytesIO(b"x" * (10 * 1024 * 1024)), "application/octet-stream")})
    assert r.status_code == 202, r.text
    r = client.raw_post("/api/connect/upload", headers=headers, data={"session_id": _session(client, headers)},
                        files={"file": ("over.db", io.BytesIO(b"x" * (10 * 1024 * 1024 + 1)), "application/octet-stream")})
    assert r.status_code == 413


def _py(code, **env):
    base = {"ENVIRONMENT": "development", "PATH": "/usr/bin:/bin", "LOG_FORMAT": "text"}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, env={**base, **env})


def test_the_upload_limit_is_configurable():
    r = _py("import api; print(sorted(set(api.UPLOAD_SIZE_LIMITS.values())), api.UPLOAD_SIZE_LIMITS['.xlsx'])",
            MAX_UPLOAD_MB="3", MAX_UPLOAD_XLSX_MB="2", DATABASE_URL=f"sqlite:///{ROOT}/tests/.tmp_limit.db")
    try:
        assert r.returncode == 0, r.stderr[-2000:]
        mb = 1024 * 1024
        assert r.stdout.strip().splitlines()[-1] == f"{[2 * mb, 3 * mb]} {2 * mb}"   # Excel 2 MB, everything else 3 MB
    finally:
        Path(ROOT, "tests/.tmp_limit.db").unlink(missing_ok=True)


def test_an_unknown_job_execution_mode_is_refused():
    r = _py("import config", JOB_EXECUTION="sometimes")
    assert r.returncode != 0 and "JOB_EXECUTION" in r.stderr


# ------------------------------------------------------------------------------------------------- deployment settings
def _docker_cmd() -> str:
    m = re.search(r'^CMD \["sh", "-c", (".*")\]$', (ROOT / "Dockerfile").read_text(), re.M)
    return json.loads(m.group(1))


@pytest.mark.parametrize("value, expected", [("1", "--workers 1"), (None, "--workers 2")])
def test_docker_startup_honors_web_concurrency(value, expected):
    # Run the image's real start-up shell line, with `exec uvicorn` swapped for `echo` so nothing is started.
    script = _docker_cmd().replace("exec uvicorn", "echo uvicorn")
    env = {"PATH": "/usr/bin:/bin", "PORT": "5000", "ENVIRONMENT": "development"}
    if value:
        env["WEB_CONCURRENCY"] = value
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, env=env).stdout
    assert expected in out, out


def test_compose_passes_web_concurrency_and_upload_limit_to_the_api():
    import yaml
    api_env = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["api"]["environment"]
    assert api_env["WEB_CONCURRENCY"] == "${WEB_CONCURRENCY:-2}"
    assert api_env["MAX_UPLOAD_MB"] == "${MAX_UPLOAD_MB:-10}"
    assert api_env["JOB_EXECUTION"] == "${JOB_EXECUTION:-auto}"
    assert api_env["MAX_UPLOAD_XLSX_MB"] == "${MAX_UPLOAD_XLSX_MB:-10}"


def test_the_browser_waits_for_the_server_to_end_the_job():
    """It gave up after 180 one-second polls; a CSV of 10 MB took 305 s on 0.1 CPU and succeeded after the UI said 'timed out'."""
    app = (ROOT / "static" / "js" / "app.js").read_text()
    poll = app[app.index("async function pollJob"):app.index("async function connectSample")]
    assert "i < 180" not in poll and "JOB_POLL_LIMIT_MS" in poll
    limit = re.search(r"const JOB_POLL_LIMIT_MS = (\d+) \* 60 \* 1000;", app)
    # longer than the latest moment the server fails a job nothing can finish
    assert limit and int(limit.group(1)) * 60 > config.JOB_STALE_SECONDS + config.JOB_ORPHAN_SECONDS


def test_render_web_service_is_set_up_for_the_free_demo():
    import yaml
    web = next(s for s in yaml.safe_load((ROOT / "render.yaml").read_text())["services"] if s["type"] == "web")
    env = {e["key"]: e.get("value") for e in web["envVars"]}
    assert web["plan"] == "free"
    assert env["WEB_CONCURRENCY"] == "1" and env["MAX_UPLOAD_MB"] == "10" and env["JOB_EXECUTION"] == "auto"
    assert env["MAX_UPLOAD_XLSX_MB"] == "2"
    assert "${WEB_CONCURRENCY:-" in web["startCommand"]
    assert shlex.split(web["startCommand"])  # still a valid shell line
