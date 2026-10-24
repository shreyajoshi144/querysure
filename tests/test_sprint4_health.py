"""Sprint 4 item 6: critical vs degraded readiness; safe DB / Redis / worker / storage health checks."""
from __future__ import annotations

import fakeredis
import pytest
from rq import Queue

import api as api_module
import config
import storage
from observability import gauges, health
from tests.sprint4_helpers import fresh_store  # noqa: F401


@pytest.fixture(autouse=True)
def _clean_health_cache():
    health.reset_cache()
    yield
    health.reset_cache()
    health.set_rq_connection(None)


def test_ready_when_everything_is_healthy(client):
    r = client.get("/api/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready" and body["success"] is True and body["degraded"] == []
    assert body["checks"] == {"database": True, "redis": True}


def test_public_readiness_document_is_minimal(client):
    body = client.get("/api/ready").json()
    assert set(body) == {"success", "status", "checks", "degraded"}
    assert all(isinstance(v, bool) for v in body["checks"].values())


def test_database_failure_is_critical_503(client, monkeypatch):
    monkeypatch.setattr(health, "check_database", lambda timeout_ms=None: (False, 2000.0))
    r = client.get("/api/ready")
    assert r.status_code == 503 and r.json()["status"] == "not_ready" and r.json()["checks"]["database"] is False
    assert r.json()["degraded"] == []                       # non-critical probes are not even run while critical is down


def test_redis_failure_is_critical_503(client, monkeypatch):
    monkeypatch.setattr(api_module, "_redis_ready", lambda: False)
    r = client.get("/api/ready")
    assert r.status_code == 503 and r.json()["checks"]["redis"] is False


def test_storage_outage_is_degraded_not_unready(client, monkeypatch):
    monkeypatch.setattr(storage, "health_check", lambda: False)
    r = client.get("/api/ready")
    assert r.status_code == 200 and r.json()["status"] == "degraded" and "storage" in r.json()["degraded"]


def test_kill_switch_and_open_circuits_are_degraded_ai(client, monkeypatch):
    monkeypatch.setattr(config, "AI_KILL_SWITCH", True)
    assert "ai" in client.get("/api/ready").json()["degraded"]
    monkeypatch.setattr(config, "AI_KILL_SWITCH", False)
    health.reset_cache()
    monkeypatch.setattr(health, "breaker_summary", lambda: {"groq": "open", "openai": "open"})
    monkeypatch.setattr(health, "configured_providers", lambda: ["groq", "openai"])
    assert "ai" in client.get("/api/ready").json()["degraded"]
    health.reset_cache()
    monkeypatch.setattr(health, "breaker_summary", lambda: {"groq": "open", "openai": "closed"})
    assert "ai" not in client.get("/api/ready").json()["degraded"]      # one provider left: still serving, fallback works


def test_global_spend_cap_reached_is_degraded(client, monkeypatch):
    monkeypatch.setitem(health._probes, "ai_budget", lambda: {"enabled": True, "global_cap_reached": True})
    assert "ai" in client.get("/api/ready").json()["degraded"]


def test_db_pool_pressure_is_degraded(client, monkeypatch):
    monkeypatch.setattr(health, "pool_summary", lambda: {"processes": 1, "max_utilization_pct": 97.0})
    assert "db_pool" in client.get("/api/ready").json()["degraded"]


def test_worker_down_with_backlog_is_degraded_and_clears_with_a_heartbeat(monkeypatch):
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(config, "REDIS_URL", "redis://fake")
    health.set_rq_connection(fake)
    from jobs import QUEUE_NAME
    assert "worker" not in health.degraded_components(force=True)                    # no backlog, no worker: nothing waiting
    Queue(QUEUE_NAME, connection=fake).enqueue("builtins.print", "x")
    monkeypatch.setattr(gauges, "read_all", lambda: [])
    assert "worker" in health.degraded_components(force=True)
    monkeypatch.setattr(gauges, "read_all", lambda: [{"role": "worker", "proc": "w1", "job_kind": None}])
    assert "worker" not in health.degraded_components(force=True)


def test_queue_stats_depth_and_oldest_age(monkeypatch):
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(config, "REDIS_URL", "redis://fake")
    health.set_rq_connection(fake)
    from jobs import QUEUE_NAME
    q = Queue(QUEUE_NAME, connection=fake)
    for _ in range(3):
        q.enqueue("builtins.print", "x")
    stats = health.queue_stats()
    assert stats["available"] and stats["depth"] == 3 and stats["oldest_age_s"] is not None and stats["oldest_age_s"] >= 0


def test_backlog_over_threshold_is_degraded(monkeypatch):
    monkeypatch.setattr(config, "REDIS_URL", "redis://fake")
    monkeypatch.setattr(health, "queue_stats", lambda: {"available": True, "depth": config.ALERT_QUEUE_DEPTH + 1, "oldest_age_s": 1, "workers_alive": 1})
    assert "queue" in health.degraded_components(force=True)


def test_degraded_probes_are_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(storage, "health_check", lambda: calls.append(1) or True)
    monkeypatch.setattr(config, "READINESS_CACHE_SECONDS", 60.0)
    for _ in range(5):
        health.degraded_components()
    assert len(calls) == 1


def test_a_crashing_probe_never_fails_readiness(client, monkeypatch):
    def explode():
        raise RuntimeError("probe bug")
    monkeypatch.setattr(storage, "health_check", explode)
    r = client.get("/api/ready")
    assert r.status_code == 200


def test_database_check_returns_fast_failure_for_a_dead_database(monkeypatch):
    import db
    class Dead:
        def execute(self, *a, **k): raise RuntimeError("connection refused")
        def close(self): pass
    monkeypatch.setattr(db, "SessionLocal", lambda: Dead())
    ok, ms = health.check_database()
    assert ok is False and ms < 1000


def test_storage_probe_local_and_s3(monkeypatch, tmp_path):
    assert storage.health_check() in (True, False)
    monkeypatch.setattr(storage, "BACKEND", "s3")
    monkeypatch.setattr(storage, "BUCKET", "nope")
    monkeypatch.setattr(storage, "_probe_client", type("C", (), {"head_bucket": lambda self, Bucket: (_ for _ in ()).throw(RuntimeError("no"))})())
    assert storage.health_check() is False
    monkeypatch.setattr(storage, "_probe_client", type("C", (), {"head_bucket": lambda self, Bucket: {}})())
    assert storage.health_check() is True


def test_health_endpoint_stays_a_pure_liveness_check(client, monkeypatch):
    monkeypatch.setattr(health, "check_database", lambda timeout_ms=None: (False, 1.0))
    assert client.get("/api/health").json() == {"status": "ok", "success": True}


def test_gauge_publish_and_read_across_processes(monkeypatch):
    from observability.store import MetricsStore, set_store
    fake = fakeredis.FakeRedis()
    store = MetricsStore(redis_factory=lambda: fake, autoflush=False)
    set_store(store)
    try:
        monkeypatch.setattr(gauges, "process_id", lambda role=None: "worker:host:1")
        gauges.set_role("worker")
        assert gauges.publish() is True
        monkeypatch.setattr(gauges, "process_id", lambda role=None: "web:host:2")
        gauges.set_role("web")
        roles = sorted(d["role"] for d in gauges.read_all())
        assert roles == ["web", "worker"]
        assert fake.ttl("qs:g:worker:host:1") > 0           # a dead process disappears on its own
    finally:
        gauges.set_role("web")
        set_store(None)


def test_readiness_probe_is_bounded_when_the_pool_is_exhausted(monkeypatch):
    """Closure finding: SessionLocal() blocks for the pool timeout (10 s) when the pool is exhausted; the probe must still answer
    within READINESS_DB_TIMEOUT_MS and must not pile up helper threads."""
    import threading
    import time
    import db
    monkeypatch.setattr(config, "READINESS_DB_TIMEOUT_MS", 300)
    release = threading.Event()
    created = []

    def blocking_session():
        created.append(1)
        release.wait(5)                                   # stands in for the pool-checkout wait
        raise RuntimeError("QueuePool limit reached")
    monkeypatch.setattr(db, "SessionLocal", blocking_session)
    t0 = time.monotonic()
    ok, ms = health.check_database()
    assert ok is False and time.monotonic() - t0 < 1.5 and ms >= 250
    t1 = time.monotonic()
    ok2, _ = health.check_database()                      # earlier probe still stuck: fail immediately, no second thread
    assert ok2 is False and time.monotonic() - t1 < 0.2 and len(created) == 1
    release.set()
    for _ in range(50):                                   # the stuck probe finishes; the next probe runs normally again
        if not health._probe_inflight.is_set():
            break
        time.sleep(0.05)
    monkeypatch.undo()
    assert health.check_database()[0] is True


def test_ready_endpoint_returns_503_json_quickly_when_the_database_probe_hangs(client, monkeypatch):
    import threading
    import time
    import db
    monkeypatch.setattr(config, "READINESS_DB_TIMEOUT_MS", 300)
    gate = threading.Event()
    monkeypatch.setattr(db, "SessionLocal", lambda: (gate.wait(3), (_ for _ in ()).throw(RuntimeError("pool")))[1])
    t0 = time.monotonic()
    r = client.get("/api/ready")
    elapsed = time.monotonic() - t0
    gate.set()
    assert r.status_code == 503 and r.json()["status"] == "not_ready" and r.json()["checks"]["database"] is False and elapsed < 1.5
    for _ in range(60):
        if not health._probe_inflight.is_set():
            break
        time.sleep(0.05)
