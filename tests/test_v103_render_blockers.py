"""Regression tests for the v1.0.3 Render/PostgreSQL deployment-blocker audit.

Each test pins a defect that was reproduced against the shipped ZIP:
  * multi-worker startup raced in db.init_db() on PostgreSQL (UniqueViolation "already exists" -> workers crash);
  * render.yaml declared the background worker on the (non-existent) free worker plan and provisioned no Redis;
  * docker-compose.yml had no worker, so queued e-mails/reports were never processed;
  * a ValueError subclass raised inside a non-ingestion job (e.g. ssl.SSLCertVerificationError) was persisted verbatim.
"""
from __future__ import annotations

import json
import os
import ssl
import threading
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _render():
    return yaml.safe_load((ROOT / "render.yaml").read_text())["services"]


# --------------------------------------------------------------------------- render.yaml
def test_render_worker_and_cron_are_not_on_the_free_plan():
    # Render offers free instances only for web services, Postgres and Key Value; a free worker/cron is an invalid blueprint.
    for svc in _render():
        if svc["type"] in ("worker", "cron"):
            assert svc["plan"] != "free", f"{svc['name']} cannot use the free plan on Render"


def test_render_provisions_redis_and_wires_it_to_web_worker_and_cron():
    services = _render()
    kv = [s for s in services if s["type"] == "keyvalue"]
    assert len(kv) == 1
    assert kv[0]["maxmemoryPolicy"] == "noeviction"   # never silently drop queued jobs
    assert kv[0]["ipAllowList"] == []                 # private network only; not publicly reachable
    for svc in services:
        if svc["type"] in ("web", "worker", "cron"):
            redis = next(e for e in svc["envVars"] if e["key"] == "REDIS_URL")
            assert redis["fromService"] == {"type": "keyvalue", "name": kv[0]["name"], "property": "connectionString"}, svc["name"]
            assert "value" not in redis  # no literal connection string / secret in the blueprint


def test_render_blueprint_commits_no_secret_values():
    secret_like = ("PASSWORD", "SECRET", "API_KEY", "ACCESS_KEY")
    for svc in _render():
        for env in svc.get("envVars", []):
            if any(tok in env["key"] for tok in secret_like):
                assert "value" not in env, f"{svc['name']}:{env['key']} must be sync:false/generated/from another service"


def test_render_web_is_first_service_and_runs_migrations_before_serving():
    web = _render()[0]
    assert web["type"] == "web" and web["name"] == "querysure-api"
    assert web["startCommand"].startswith("alembic upgrade head && uvicorn api:app")
    assert web["healthCheckPath"] == "/api/health"


# --------------------------------------------------------------------------- docker-compose.yml
def test_compose_has_a_worker_without_the_api_http_healthcheck():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    worker = compose["services"]["worker"]
    assert worker["command"] == ["python", "worker.py"]
    assert worker["environment"]["REDIS_URL"] == compose["services"]["api"]["environment"]["REDIS_URL"]
    assert worker["environment"]["DATABASE_URL"] == compose["services"]["api"]["environment"]["DATABASE_URL"]
    # The image HEALTHCHECK probes the API's HTTP port; the worker must override it with a check that needs neither curl
    # (absent from python:slim) nor an HTTP endpoint, and instead looks for the worker's own Redis heartbeat.
    probe = " ".join(worker["healthcheck"]["test"])
    assert worker["healthcheck"]["test"][0] == "CMD"
    assert "curl" not in probe and "http" not in probe.lower() and "5000" not in probe
    assert "qs:g:worker:" in probe and "REDIS_URL" in probe
    assert {"db", "redis"} <= set(worker["depends_on"])
    # Datastores stay bound to the host loopback only.
    for name in ("db", "redis"):
        assert all(p.startswith("127.0.0.1:") for p in compose["services"][name]["ports"])


def test_compose_worker_healthcheck_follows_the_worker_redis_heartbeat(monkeypatch):
    # Runs the exact probe from docker-compose.yml against the heartbeat that worker.py publishes (gauges.start_publisher).
    fakeredis = pytest.importorskip("fakeredis")
    import redis

    from observability import gauges
    from observability.store import MetricsStore, set_store

    probe = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["worker"]["healthcheck"]["test"]
    assert probe[:3] == ["CMD", "python", "-c"]
    server = fakeredis.FakeServer()
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setattr(redis.Redis, "from_url", classmethod(lambda cls, *a, **k: fakeredis.FakeRedis(server=server)))

    def run_probe() -> int:
        try:
            exec(probe[3], {})
        except SystemExit as exc:
            return exc.code
        raise AssertionError("probe must exit explicitly")

    assert run_probe() == 1                                    # no worker heartbeat yet
    set_store(MetricsStore(redis_factory=lambda: fakeredis.FakeRedis(server=server)))
    previous_role = gauges._role
    try:
        gauges.set_role("worker")
        assert gauges.publish()
        assert run_probe() == 0                                # this container's worker is alive
        client = fakeredis.FakeRedis(server=server)
        client.delete(*client.keys("qs:g:worker:*"))           # heartbeat TTL expired (worker dead)
        assert run_probe() == 1
        client.set("qs:g:worker:some-other-container:1", "{}")  # another container's worker does not count
        assert run_probe() == 1
        client.set("qs:g:web:" + gauges.socket.gethostname() + ":1", "{}")  # nor does a web process heartbeat
        assert run_probe() == 1
    finally:
        gauges.set_role(previous_role)
        set_store(None)


# --------------------------------------------------------------------------- jobs: error text
def test_non_ingestion_valueerror_subclass_is_not_persisted_verbatim(tmp_path, monkeypatch):
    import jobs as jobs_module
    from db import Job
    from tests.test_sprint2_jobs import _job_db

    engine, Session, db, user_id = _job_db(tmp_path)
    job = Job(id="job-ssl", user_id=user_id, kind="auth_email", status="queued")
    db.add(job); db.commit()

    def boom(db, job, payload):
        # ssl.SSLCertVerificationError is BOTH an OSError and a ValueError.
        raise ssl.SSLCertVerificationError("hostname smtp.internal.example mismatch, certificate CN=secret-ca")

    monkeypatch.setattr(jobs_module, "SessionLocal", Session)
    monkeypatch.setattr(jobs_module, "_dispatch", boom)
    monkeypatch.setattr(jobs_module.time, "sleep", lambda s: None)
    jobs_module._execute_job(job.id, {})
    row = Session().query(Job).filter_by(id=job.id).one()
    assert row.status == "failed" and row.error_code == "JOB_FAILED"
    stored = row.result_ref
    assert "smtp.internal.example" not in stored and "secret-ca" not in stored
    assert json.loads(stored)["error"] == "The background operation could not be completed. Please try again."
    engine.dispose()


def test_ingestion_validation_message_is_still_shown_to_the_user(tmp_path, monkeypatch):
    import jobs as jobs_module
    from db import Job
    from tests.test_sprint2_jobs import _job_db

    engine, Session, db, user_id = _job_db(tmp_path)
    job = Job(id="job-bad-file", user_id=user_id, kind="dataset_ingestion", status="queued")
    db.add(job); db.commit()

    def reject(db, job, payload):
        raise ValueError("CSV has more than 200 columns.")

    monkeypatch.setattr(jobs_module, "SessionLocal", Session)
    monkeypatch.setattr(jobs_module, "_dispatch", reject)
    monkeypatch.setattr(jobs_module.storage, "delete_staged", lambda *a, **k: None)
    jobs_module._execute_job(job.id, {"object_ref": "x"})
    row = Session().query(Job).filter_by(id=job.id).one()
    assert row.error_code == "INVALID_UPLOAD"
    assert json.loads(row.result_ref)["error"] == "CSV has more than 200 columns."
    engine.dispose()


# --------------------------------------------------------------------------- db.init_db on PostgreSQL
def test_init_db_serialises_postgres_schema_creation_with_an_advisory_lock(monkeypatch):
    import db as db_module

    events = []

    class FakeConn:
        def exec_driver_sql(self, sql, *a, **k):
            events.append(("sql", sql))
        def commit(self): events.append(("commit",))
        def rollback(self): events.append(("rollback",))
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    class FakeDialect: name = "postgresql"

    class FakeEngine:
        dialect = FakeDialect()
        def connect(self): return FakeConn()

    monkeypatch.setattr(db_module, "engine", FakeEngine())
    monkeypatch.setattr(db_module, "IS_PRODUCTION", False)
    monkeypatch.setattr(db_module.Base.metadata, "create_all", lambda bind=None: events.append(("create_all", type(bind).__name__)))
    db_module.init_db()
    kinds = [e[1] if e[0] == "sql" else e[0] for e in events]
    assert kinds[0].startswith("SELECT pg_advisory_lock")
    assert ("create_all", "FakeConn") in events
    assert any(k.startswith("SELECT pg_advisory_unlock") for k in kinds)
    assert kinds.index("create_all") < max(i for i, k in enumerate(kinds) if isinstance(k, str) and k.startswith("SELECT pg_advisory_unlock"))


def test_init_db_is_a_noop_in_production(monkeypatch):
    import db as db_module

    class Boom:
        dialect = type("D", (), {"name": "postgresql"})()
        def connect(self): raise AssertionError("production must never create_all; Alembic owns the schema")

    monkeypatch.setattr(db_module, "engine", Boom())
    monkeypatch.setattr(db_module, "IS_PRODUCTION", True)
    db_module.init_db()


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="set TEST_POSTGRES_URL (a server where the role may create databases)")
def test_concurrent_init_db_on_real_postgres_does_not_race():
    """N processes call init_db() on an EMPTY database at once (what `uvicorn --workers N` does in development/compose)."""
    import subprocess
    import sys
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    base = make_url(os.environ["TEST_POSTGRES_URL"])
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = f"qs_race_{os.getpid()}"
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    code = "import db; db.init_db()"
    env = dict(os.environ, ENVIRONMENT="development", DATABASE_URL=url)
    env.pop("REDIS_URL", None)
    try:
        procs = [subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(6)]
        results = [(p.wait(timeout=120), p.stderr.read()) for p in procs]
        failures = [(rc, err[-400:]) for rc, err in results if rc != 0]
        assert not failures, failures
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
