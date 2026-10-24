"""Sprint 4 item 2: production metrics (API p50/p95/p99, errors, traffic, rate limits, AI, queue, DB pool, Redis)."""
from __future__ import annotations

import fakeredis
import pytest
import redis

import api as api_module
from observability import gauges, health, metrics as m, slo
from observability.store import BUCKET_SECONDS, HIST_BOUNDS_MS, MetricsStore, iter_prom_lines, quantile_from_buckets, set_store
from tests.sprint3_helpers import connect_csv, fake_result, generate, register_headers
from tests.sprint4_helpers import fresh_store  # noqa: F401


# ---- store maths -------------------------------------------------------------------------------------------------
def test_percentiles_are_bucket_accurate():
    s = MetricsStore()
    for ms in [10] * 50 + [100] * 45 + [1000] * 4 + [40000]:
        s.observe("lat", ms, route="/x")
    snap = s.snapshot(5)
    summ = snap.latency_summary("lat")
    assert summ["count"] == 100 and summ["p50_ms"] <= 10 and 50 < summ["p95_ms"] <= 100 and 500 < summ["p99_ms"] <= 1000
    assert summ["mean_ms"] == pytest.approx(490.0, rel=0.01)


def test_quantile_edge_cases():
    assert quantile_from_buckets([0] * (len(HIST_BOUNDS_MS) + 1), 0.5) is None
    only_overflow = [0] * len(HIST_BOUNDS_MS) + [5]
    assert quantile_from_buckets(only_overflow, 0.99) == HIST_BOUNDS_MS[-1]


def test_counters_filter_and_group_by_label():
    s = MetricsStore()
    s.inc("http_requests", 7, route="/a", status_class="2xx")
    s.inc("http_requests", 2, route="/a", status_class="5xx")
    s.inc("http_requests", 1, route="/b", status_class="2xx")
    snap = s.snapshot(5)
    assert snap.counter("http_requests") == 10 and snap.counter("http_requests", route="/a", status_class="5xx") == 2
    assert snap.counter_by("http_requests", "status_class") == {"2xx": 8, "5xx": 2}


def test_label_values_cannot_break_the_wire_format():
    s = MetricsStore()
    s.inc("c", reason="a,b=c|d e")
    assert s.snapshot(5).counter("c") == 1


def test_old_buckets_fall_out_of_the_window():
    now = [1_000_000.0]
    s = MetricsStore(clock=lambda: now[0])
    s.inc("c", 5)
    now[0] += BUCKET_SECONDS * 10
    s.inc("c", 3)
    assert s.snapshot(5).counter("c") == 3 and s.snapshot(15).counter("c") == 8


def test_disabled_metrics_record_nothing(monkeypatch):
    import config
    monkeypatch.setattr(config, "METRICS_ENABLED", False)
    s = MetricsStore()
    s.inc("c")
    s.observe("h", 5)
    assert s.snapshot(5).counter("c") == 0


# ---- shared via Redis; Redis loss never loses or blocks ------------------------------------------------------------
def test_two_processes_share_one_view_through_redis():
    server = fakeredis.FakeServer()
    a = MetricsStore(redis_factory=lambda: fakeredis.FakeRedis(server=server), autoflush=False)
    b = MetricsStore(redis_factory=lambda: fakeredis.FakeRedis(server=server), autoflush=False)
    a.inc("jobs", 2, kind="x", outcome="succeeded")
    b.inc("jobs", 3, kind="x", outcome="succeeded")
    b.observe("lat", 50)
    a.flush(); b.flush()
    assert a.snapshot(5).counter("jobs") == 5 and b.snapshot(5).hist_count("lat") == 1


def test_redis_outage_buffers_locally_and_recovers_without_loss():
    class Dead:
        def pipeline(self, *a, **k):
            raise redis.exceptions.ConnectionError("down")
    now = [5_000.0]
    server = fakeredis.FakeServer()
    state = {"client": Dead()}
    s = MetricsStore(redis_factory=lambda: state["client"], autoflush=False, clock=lambda: now[0])
    s.inc("c", 4)
    assert s.flush() is False and not s.healthy()
    assert s.snapshot(5).counter("c") == 4          # still visible locally; request path never raised
    s.inc("c", 1)                                   # recording during the outage still works
    assert s.snapshot(5).counter("c") == 5
    now[0] += 60                                    # cooldown over, Redis is back
    s._client = None
    state["client"] = fakeredis.FakeRedis(server=server)
    assert s.flush() is True and s.healthy()
    assert s.snapshot(5).counter("c") >= 1


def test_prometheus_text_has_cumulative_buckets():
    s = MetricsStore()
    s.observe("http_request_ms", 7, route="/x"); s.observe("http_request_ms", 700, route="/x")
    s.inc("http_requests", 2, route="/x", status_class="2xx")
    text = "\n".join(iter_prom_lines(s.snapshot(5)))
    assert 'qs_http_request_ms_bucket{route="/x",le="+Inf"} 2' in text and 'qs_http_requests_window{route="/x",status_class="2xx"} 2' in text


# ---- recorded by the real app ----------------------------------------------------------------------------------
def test_http_metrics_use_route_templates_and_status_classes(client, fresh_store):
    headers, _ = register_headers(client, "met")
    sid = connect_csv(client, headers)
    client.get("/api/sessions", headers=headers)
    client.delete(f"/api/session/{sid}", headers=headers)
    client.get("/api/sessions")                      # 401
    snap = fresh_store.snapshot(5)
    routes = set(snap.hist_label_values(m.HTTP_LATENCY, "route"))
    assert "/api/session/{session_id}" in routes and sid not in " ".join(routes)
    by_class = snap.counter_by(m.HTTP_REQUESTS, "status_class", route="/api/sessions")
    assert by_class.get("2xx", 0) >= 1 and by_class.get("4xx", 0) == 1
    assert snap.latency_summary(m.HTTP_LATENCY, route="/api/sessions")["p95_ms"] is not None


def test_rate_limit_rejections_are_counted(client, fresh_store):
    previous = api_module.limiter.enabled
    api_module.limiter.enabled = True            # the suite disables slowapi globally; same switch the Sprint 1 test uses
    try:
        for _ in range(40):
            r = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": "x" * 12})
            if r.status_code == 429:
                break
    finally:
        api_module.limiter.enabled = previous
    assert r.status_code == 429
    assert fresh_store.snapshot(5).counter(m.RATE_LIMITED) >= 1


def test_ai_metrics_latency_tokens_cost_provider_and_fallback(client, fresh_store, monkeypatch):
    headers, _ = register_headers(client, "aimet")
    sid = connect_csv(client, headers)
    monkeypatch.setattr(api_module, "generate_sql", lambda **kw: fake_result(
        latency_ms=800, usage={"input_tokens": 300, "output_tokens": 40, "estimated": False, "cost_cents": 0.07}))
    assert generate(client, headers, sid).status_code == 200
    snap = fresh_store.snapshot(5)
    assert snap.counter(m.AI_REQUESTS, provider="groq", outcome="success") == 1
    assert snap.counter(m.AI_TOKENS, provider="groq", direction="input") == 300
    assert snap.counter(m.AI_TOKENS, provider="groq", direction="output") == 40
    assert snap.counter(m.AI_COST, provider="groq") == pytest.approx(0.07)
    assert snap.counter(m.AI_FALLBACKS) == 1 and snap.latency_summary(m.AI_LATENCY, provider="groq")["count"] == 1


def test_ai_failure_and_budget_denials_are_counted(client, fresh_store, monkeypatch):
    headers, _ = register_headers(client, "aifail")
    sid = connect_csv(client, headers)
    monkeypatch.setattr(api_module, "generate_sql", lambda **kw: {"success": False, "error": "all down", "error_type": "provider_error",
                                                                 "usage": None, "latency_ms": 5})
    assert generate(client, headers, sid).status_code == 502
    monkeypatch.setattr(api_module._config_module, "AI_KILL_SWITCH", True)
    assert generate(client, headers, sid).status_code == 503
    snap = fresh_store.snapshot(5)
    assert snap.counter(m.AI_REQUESTS, outcome="failure") == 1
    assert snap.counter(m.AI_BUDGET_DENIED, scope="kill_switch") == 1


def test_db_pool_gauge_reports_utilisation():
    stats = gauges.db_pool_stats()
    assert "supported" in stats
    if stats["supported"]:
        assert 0 <= stats["utilization_pct"] <= 100 and stats["capacity"] >= stats["size"]
    assert gauges.collect_local()["role"] in ("web", "worker")


def test_redis_info_when_not_configured(monkeypatch):
    import config
    monkeypatch.setattr(config, "REDIS_URL", None)
    health.set_rq_connection(None)
    assert health.redis_info() == {"configured": False}


def test_redis_info_reports_saturation_figures(monkeypatch):
    import config
    monkeypatch.setattr(config, "REDIS_URL", "redis://fake")
    class StubRedis:                      # fakeredis does not implement INFO
        def ping(self): return True
        def info(self): return {"connected_clients": 3, "blocked_clients": 1, "used_memory": 5 * 1048576, "maxmemory": 10 * 1048576,
                                "instantaneous_ops_per_sec": 42, "evicted_keys": 2, "rejected_connections": 0}
    health.set_rq_connection(StubRedis())
    try:
        info = health.redis_info()
    finally:
        health.set_rq_connection(None)
    assert info["reachable"] is True and info["memory_utilization_pct"] == 50.0 and info["evicted_keys"] == 2
    assert {"ping_ms", "connected_clients", "used_memory_mb", "ops_per_sec"} <= set(info)
