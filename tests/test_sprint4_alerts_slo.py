"""Sprint 4 item 7: configurable SLOs, alerts, privacy-safe incident / event tracking."""
from __future__ import annotations

import threading
import uuid

import pytest

import config
from db import OpsEvent, OpsIncident, SessionLocal
from observability import alerts, events, metrics as m, slo
from observability.alerts import Signal
from observability.store import MetricsStore, set_store
from tests.sprint4_helpers import fresh_store  # noqa: F401


def _feed_http(store, ok=0, err=0, ms=40, route="/api/sessions"):
    for _ in range(ok):
        store.inc(m.HTTP_REQUESTS, route=route, method="GET", status_class="2xx"); store.observe(m.HTTP_LATENCY, ms, route=route)
    for _ in range(err):
        store.inc(m.HTTP_REQUESTS, route=route, method="GET", status_class="5xx"); store.observe(m.HTTP_LATENCY, ms, route=route)


def _by_name(res):
    return {o["name"]: o for o in res["objectives"]}


# ---- SLOs ------------------------------------------------------------------------------------------------------------
def test_slo_reports_no_data_below_min_samples(fresh_store):
    _feed_http(fresh_store, ok=3)
    assert _by_name(slo.evaluate(fresh_store))["availability"]["status"] == "no_data"


def test_slo_availability_ok_and_breach_and_error_budget(fresh_store):
    _feed_http(fresh_store, ok=1000, err=2)
    av = _by_name(slo.evaluate(fresh_store))["availability"]
    assert av["status"] == "ok" and av["sli"] > 99.5 and 0 < av["error_budget_remaining_pct"] < 100
    _feed_http(fresh_store, err=200)
    res = slo.evaluate(fresh_store)
    assert _by_name(res)["availability"]["status"] == "breach" and "availability" in res["breaches"]
    assert _by_name(res)["availability"]["burn_rate_short"] > config.SLO_BURN_RATE_THRESHOLD


def test_slo_targets_are_configurable(fresh_store, monkeypatch):
    _feed_http(fresh_store, ok=100, ms=2000)
    assert _by_name(slo.evaluate(fresh_store))["api_latency_p95"]["status"] == "breach"       # default 1500ms
    monkeypatch.setattr(config, "SLO_API_P95_MS", 10_000.0)
    assert _by_name(slo.evaluate(fresh_store))["api_latency_p95"]["status"] == "ok"


def test_slow_ai_routes_do_not_pollute_interactive_latency_slo(fresh_store):
    _feed_http(fresh_store, ok=100, ms=40)
    _feed_http(fresh_store, ok=100, ms=20000, route="/api/generate-sql")
    assert _by_name(slo.evaluate(fresh_store))["api_latency_p95"]["status"] == "ok"


def test_ai_and_job_slos(fresh_store):
    for _ in range(95):
        fresh_store.inc(m.AI_REQUESTS, provider="groq", outcome="success")
    for _ in range(25):
        fresh_store.inc(m.AI_REQUESTS, provider="groq", outcome="failure")
    for _ in range(30):
        fresh_store.inc(m.JOBS, kind="report_generation", outcome="succeeded")
    res = _by_name(slo.evaluate(fresh_store))
    assert res["ai_success"]["status"] == "breach" and res["job_success"]["status"] == "ok"


# ---- event privacy ---------------------------------------------------------------------------------------------------
def test_event_detail_is_allow_listed_and_value_checked():
    clean = events.sanitize_detail({
        "provider": "groq", "count": 3, "pct": 83.123456, "user_id": "u-1", "email": "a@b.co", "question": "secret",
        "sql": "select 1", "reason": "select * from t; drop table x", "state": "open"})
    assert clean == {"provider": "groq", "count": 3, "pct": 83.123, "state": "open"}


def test_events_persist_without_user_data_and_never_block(client):
    events.record_event("test_event_kind", severity="warning", component="ai",
                        detail={"provider": "groq", "user_id": "SHOULD-NOT-APPEAR", "question": "SHOULD-NOT-APPEAR"}, sync=True)
    with SessionLocal() as db:
        row = db.query(OpsEvent).filter(OpsEvent.kind == "test_event_kind").order_by(OpsEvent.created_at.desc()).first()
        assert row.detail == {"provider": "groq"} and row.severity == "warning"


def test_record_event_never_raises_when_db_is_down(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(events, "_insert_event", broken)
    events.record_event("x", sync=True)            # must not raise
    events.record_event("x")                       # async path: failure is swallowed by the writer
    events.flush(2.0)


def test_circuit_transitions_become_events(client):
    import ai_guard
    b = ai_guard.get_breaker("evtprov")
    for _ in range(config.CIRCUIT_FAILURE_THRESHOLD):
        b.record_failure()
    b.record_success()
    events.flush(5.0)
    with SessionLocal() as db:
        kinds = {r.kind for r in db.query(OpsEvent).filter(OpsEvent.kind.in_(("circuit_opened", "circuit_closed"))).all()}
        details = [r.detail for r in db.query(OpsEvent).filter(OpsEvent.kind == "circuit_opened").all()]
    assert {"circuit_opened", "circuit_closed"} <= kinds and {"provider": "evtprov", "from_state": "closed", "to_state": "open",
                                                           "threshold": config.CIRCUIT_FAILURE_THRESHOLD} in details


# ---- incidents + alerts ----------------------------------------------------------------------------------------------
@pytest.fixture()
def deliveries(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "deliver", lambda payload: sent.append(payload) or True)
    return sent


def _sig(key=None, severity="warning", **detail):
    key = key or f"test_{uuid.uuid4().hex[:8]}"
    return Signal(key, "test_alert", severity, "ai", "Something is wrong", detail or {"component": "ai"})


def test_alert_lifecycle_open_dedupe_resolve(client, deliveries, monkeypatch):
    monkeypatch.setattr(config, "ALERT_RESOLVE_AFTER_SECONDS", 0)
    sig = _sig()
    first = alerts.evaluate(signals=[sig])
    again = alerts.evaluate(signals=[sig])
    assert first["opened"] == [sig.key] and again["opened"] == []                 # one notification, not one per tick
    firing = [p for p in deliveries if p["status"] == "firing" and p["alert"] == "test_alert"]
    assert len(firing) == 1 and firing[0]["incident_id"]
    with SessionLocal() as db:
        rows = db.query(OpsIncident).filter(OpsIncident.incident_key == sig.key).all()
        assert len(rows) == 1 and rows[0].status == "open" and rows[0].occurrences == 2
    cleared = alerts.evaluate(signals=[])
    assert sig.key in cleared["resolved"] and any(p["status"] == "resolved" for p in deliveries)
    with SessionLocal() as db:
        assert db.query(OpsIncident).filter(OpsIncident.incident_key == sig.key).one().status == "resolved"
    reopened = alerts.evaluate(signals=[sig])                                       # a new occurrence is a NEW incident
    assert reopened["opened"] == [sig.key]
    alerts.evaluate(signals=[])


def test_resolution_waits_for_the_hold_time(client, deliveries, monkeypatch):
    monkeypatch.setattr(config, "ALERT_RESOLVE_AFTER_SECONDS", 3600)
    sig = _sig()
    alerts.evaluate(signals=[sig])
    assert alerts.evaluate(signals=[])["resolved"] == []                            # flapping signal does not resolve instantly
    monkeypatch.setattr(config, "ALERT_RESOLVE_AFTER_SECONDS", 0)
    alerts.evaluate(signals=[])


def test_only_one_open_incident_per_key_under_concurrency(client):
    key = f"race_{uuid.uuid4().hex[:8]}"
    barrier = threading.Barrier(6)
    created = []

    def worker():
        db = SessionLocal()
        try:
            barrier.wait()
            _row, was_created = events.open_or_touch_incident(db, key, alert="race", severity="warning", component="ai", detail={})
            created.append(was_created)
        finally:
            db.close()
    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert created.count(True) == 1
    with SessionLocal() as db:
        assert db.query(OpsIncident).filter(OpsIncident.incident_key == key, OpsIncident.status == "open").count() == 1
        db.query(OpsIncident).filter(OpsIncident.incident_key == key).delete(); db.commit()


def test_alert_payload_carries_no_user_data():
    sig = Signal("k", "slo_breach", "warning", "api", "SLO breached", {"slo": "api_latency_p95", "sli": 2100.5, "user_id": "u1",
                                                                       "email": "a@b.co", "question": "q"})
    payload = alerts.build_payload("firing", sig, "inc-1")
    text = str(payload)
    assert "u1" not in text and "a@b.co" not in text and payload["detail"] == {"slo": "api_latency_p95", "sli": 2100.5}


def test_webhook_delivery_failure_is_swallowed_and_counted(fresh_store, monkeypatch):
    monkeypatch.setattr(config, "ALERT_WEBHOOK_URL", "https://127.0.0.1:1/never")
    assert alerts.deliver({"alert": "x"}) is False
    assert fresh_store.snapshot(5).counter(m.ALERT_DELIVERY, outcome="failed") == 1
    monkeypatch.setattr(config, "ALERT_WEBHOOK_URL", "")
    assert alerts.deliver({"alert": "x"}) is False


def test_signals_cover_breaker_budget_and_kill_switch(monkeypatch, fresh_store):
    from observability import health
    monkeypatch.setattr(health, "breaker_summary", lambda: {"groq": "open"})
    monkeypatch.setattr(health, "configured_providers", lambda: ["groq"])
    monkeypatch.setattr(config, "AI_KILL_SWITCH", True)
    monkeypatch.setitem(health._probes, "ai_budget", lambda: {"enabled": True, "global_cap_reached": True, "spent_pct": 100.0})
    keys = {s.key for s in alerts.collect_signals()}
    assert {"ai_circuit_open:groq", "ai_all_providers_down", "ai_kill_switch_active", "ai_budget_exhausted"} <= keys


def test_availability_burn_alert_needs_both_windows(monkeypatch, fresh_store):
    _feed_http(fresh_store, ok=500, err=300)
    keys = {s.key for s in alerts.collect_signals()}
    assert "slo_availability_burn" in keys


def test_retention_prune_removes_old_rows(client):
    from datetime import datetime, timedelta, timezone
    with SessionLocal() as db:
        old = OpsEvent(kind="old_event_x", severity="info", component="app", created_at=datetime.now(timezone.utc) - timedelta(days=90))
        db.add(old); db.commit()
        assert events.prune(db, 30) >= 1
        assert db.query(OpsEvent).filter(OpsEvent.kind == "old_event_x").count() == 0
