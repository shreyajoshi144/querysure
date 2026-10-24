"""Sprint 4 item 5: AI reliability (timeouts, fallback, circuit breaker, spend cap, reservation rollback, query-slot refund)
and background-job reliability (stale-job reaper, job metrics)."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import ai_guard
import api as api_module
import config
import jobs
import quotas
from db import Job, SessionConflictError, SessionLocal, User, get_or_create_daily_usage
from modules import build_semantic_layer, load_database
from modules import sql_generator as sg
from observability import metrics as m
from tests.sprint3_helpers import connect_csv, fake_result, register_headers
from tests.sprint4_helpers import fresh_store  # noqa: F401

GOOD = {"sql": "SELECT s.region FROM sales AS s", "explanation": "x", "confidence": "High", "complexity": "Simple"}


@pytest.fixture()
def world(tmp_path):
    path = str(tmp_path / "t.db")
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE sales(id INTEGER PRIMARY KEY, region TEXT, amount REAL); INSERT INTO sales VALUES (1,'N',1.5);")
    con.commit(); con.close()
    schema = load_database(path)
    return schema, build_semantic_layer(schema)


class RateLimited(Exception):
    status_code = 429


# ---- deadline / timeouts ---------------------------------------------------------------------------------------------
def test_call_timeout_is_capped_by_remaining_deadline():
    assert ai_guard.remaining() is None and ai_guard.call_timeout(25.0) == 25.0
    token = ai_guard.start_deadline(10.0)
    try:
        assert ai_guard.call_timeout(25.0) <= 5.0          # SDK retries once => two attempts must fit
        assert ai_guard.can_start_call()
    finally:
        ai_guard.reset_deadline(token)
    assert ai_guard.remaining() is None


def test_no_provider_call_starts_after_the_deadline(monkeypatch, fresh_store):
    called = []
    monkeypatch.setattr(sg, "_call_groq", lambda *a: called.append(1) or sg.LLMResult("{}", 1, 1))
    token = ai_guard.start_deadline(0.0)
    try:
        with pytest.raises(sg.ProviderError) as exc:
            sg._run_provider_chain("s", "u", "k")
    finally:
        ai_guard.reset_deadline(token)
    assert not called and "deadline_exceeded" in str(exc.value)
    assert fresh_store.snapshot(5).counter(m.AI_DEADLINE) == 1


def test_generate_sql_runs_under_a_deadline_and_cleans_up(monkeypatch, world):
    schema, semantic = world
    seen = {}

    def fake(model, key, sp, up):
        seen["remaining"] = ai_guard.remaining()
        return sg.LLMResult(json.dumps(GOOD), 10, 5)
    monkeypatch.setattr(sg, "_call_groq", fake)
    out = sg.generate_sql("regions", schema, "k", semantic=semantic)
    assert out["success"] and 0 < seen["remaining"] <= config.AI_REQUEST_DEADLINE_SECONDS
    assert ai_guard.remaining() is None


def test_provider_timeout_config_is_wired_to_sdk_clients(monkeypatch):
    import sys
    import types
    seen = {}

    class Client:
        def __init__(self, *a, **k):
            seen.update(k)
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **kw: types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="{}"))], usage=None)))
    mod = types.ModuleType("groq"); mod.Groq = Client
    monkeypatch.setitem(sys.modules, "groq", mod)
    token = ai_guard.start_deadline(6.0)
    try:
        sg._call_groq("m", "k", "s", "u")
    finally:
        ai_guard.reset_deadline(token)
    assert seen["timeout"] <= 3.0 and seen["max_retries"] == 1
    assert sg._LLM_TIMEOUT_S == config.AI_PROVIDER_TIMEOUT_SECONDS


# ---- fallback + circuit breaker --------------------------------------------------------------------------------------
def test_fallback_model_is_used_and_failure_reason_is_classified_without_the_message(monkeypatch, fresh_store, world):
    schema, semantic = world

    def fake(model, key, sp, up):
        if model == config.GROQ_MODEL:
            raise RateLimited("429 from provider echoing sk-abcdefghijklmnopqrstuvwxyz and user question text")
        return sg.LLMResult(json.dumps(GOOD), 10, 5)
    monkeypatch.setattr(sg, "_call_groq", fake)
    out = sg.generate_sql("regions", schema, "k", semantic=semantic)
    assert out["success"] and out["fallback_used"] and out["provider_used"].endswith(config.GROQ_FALLBACK_MODEL)
    snap = fresh_store.snapshot(5)
    assert snap.counter(m.AI_PROVIDER_FAILURES, provider="groq", reason="rate_limited") == 1
    assert all("sk-" not in k for k in snap.counter_by(m.AI_PROVIDER_FAILURES, "reason"))


def test_open_circuit_skips_the_network_and_is_counted(monkeypatch, fresh_store):
    called = []
    monkeypatch.setattr(sg, "_call_groq", lambda *a: called.append(1) or sg.LLMResult("{}", 1, 1))
    br = ai_guard.get_breaker("groq")
    for _ in range(config.CIRCUIT_FAILURE_THRESHOLD):
        br.record_failure()
    with pytest.raises(sg.ProviderError):
        sg._run_provider_chain("s", "u", "k")
    assert not called and fresh_store.snapshot(5).counter(m.AI_CIRCUIT_SKIPS, provider="groq") >= 1
    assert ai_guard.states()["groq"] == "open"


def test_half_open_probe_closes_breaker_on_success():
    now = [100.0]
    b = ai_guard.CircuitBreaker("p", threshold=2, cooldown=10, clock=lambda: now[0])
    b.record_failure(); b.record_failure()
    assert b.state == ai_guard.OPEN and not b.allow()
    now[0] += 11
    assert b.allow() and not b.allow()                   # exactly one probe
    b.record_success()
    assert b.state == ai_guard.CLOSED and b.snapshot() == {"state": "closed", "consecutive_failures": 0}


def test_classifier_never_looks_at_the_message():
    class Timeout(Exception): pass
    class Boom(Exception): pass
    assert ai_guard.classify_provider_error(Timeout("alice@example.com")) == "timeout"
    assert ai_guard.classify_provider_error(Boom("anything")) == "error"
    assert ai_guard.classify_provider_error(RateLimited()) == "rate_limited"


def test_failed_repair_call_keeps_tokens_accounted_and_sql_unexecutable(monkeypatch, world):
    schema, semantic = world
    bad = dict(GOOD, sql="SELECT n.nope FROM nonexistent AS n")
    monkeypatch.setattr(sg, "_call_groq", lambda *a: sg.LLMResult(json.dumps(bad), 100, 20))
    spent = sg.ProviderAttempt(name="groq:m", usage={"input_tokens": 500, "output_tokens": 60, "estimated": False, "cost_cents": 0.2})

    def repair_fails(*a, **k):
        raise sg.StructuredOutputError("invalid twice", [spent])
    monkeypatch.setattr(sg, "_repair_sql_with_llm", repair_fails)
    out = sg.generate_sql("q", schema, "k", semantic=semantic)
    assert out["success"] is True and out["validation"]["is_valid"] is False        # never reported valid
    assert out["usage"]["input_tokens"] == 600 and out["usage"]["calls"] == 2        # initial call + tokens burned in the failed repair


# ---- budget reservation / rollback (API level) ----------------------------------------------------------------------
def _stub_gen(monkeypatch, cost=0.5):
    monkeypatch.setattr(api_module, "generate_sql", lambda **kw: fake_result(
        usage={"input_tokens": 1, "output_tokens": 1, "estimated": False, "cost_cents": cost}))


def _user_id(email):
    with SessionLocal() as db:
        return db.query(User).filter(User.email == email).one().id


def _spend(uid):
    with SessionLocal() as db:
        return get_or_create_daily_usage(db, uid).estimated_cost_cents


def test_settle_failure_after_a_successful_generation_does_not_turn_into_a_500(client, monkeypatch):
    headers, email = register_headers(client, "settle")
    sid = connect_csv(client, headers)
    _stub_gen(monkeypatch)

    def settle_boom(*a, **k):
        raise RuntimeError("ledger unavailable")
    monkeypatch.setattr(api_module, "_settle_ai_budget", settle_boom)
    r = client.post("/api/generate-sql", headers=headers, json={"session_id": sid, "question": "total", "api_key": "stub-key"})
    assert r.status_code == 200 and r.json()["usage_today"]["requests"] is None and r.json()["sql"]


def test_provider_failure_refunds_the_reservation_down_to_what_was_spent(client, monkeypatch):
    headers, email = register_headers(client, "refund")
    sid = connect_csv(client, headers)
    monkeypatch.setattr(api_module, "generate_sql", lambda **kw: {"success": False, "error": "down", "usage": None, "latency_ms": 1})
    before = _spend(_user_id(email))
    r = client.post("/api/generate-sql", headers=headers, json={"session_id": sid, "question": "total", "api_key": "stub-key"})
    assert r.status_code == 502 and _spend(_user_id(email)) == pytest.approx(before)


def test_global_cap_probe_reports_percentages_only(monkeypatch):
    monkeypatch.setattr(api_module, "GLOBAL_DAILY_AI_BUDGET_CENTS", 1000.0)
    monkeypatch.setattr(api_module, "redis_client", None)
    probe = api_module._ai_budget_probe()
    assert probe["enabled"] and set(probe) <= {"enabled", "known", "spent_pct", "cap_usd", "global_cap_reached"}


# ---- query-slot refund -----------------------------------------------------------------------------------------------
def _exec(client, headers, sid):
    return client.post("/api/execute-sql", headers=headers, json={"session_id": sid, "sql": "SELECT s.region FROM sales AS s"})


def _slots(uid):
    with SessionLocal() as db:
        return get_or_create_daily_usage(db, uid).query_count


@pytest.fixture()
def exec_world(client):
    headers, email = register_headers(client, "slot")
    return headers, connect_csv(client, headers), _user_id(email)


def test_successful_execution_keeps_exactly_one_slot(client, exec_world, fresh_store):
    headers, sid, uid = exec_world
    assert _exec(client, headers, sid).status_code == 200 and _slots(uid) == 1
    assert fresh_store.snapshot(5).counter(m.QUERY_SLOTS, outcome="reserved") == 1


def test_slot_refunded_when_the_executor_raises(client, exec_world, monkeypatch, fresh_store):
    headers, sid, uid = exec_world

    def boom(*a, **k):
        raise RuntimeError("executor crashed")
    monkeypatch.setattr(api_module, "execute_query", boom)
    with pytest.raises(RuntimeError, match="executor crashed"):
        _exec(client, headers, sid)
    assert _slots(uid) == 0 and fresh_store.snapshot(5).counter(m.QUERY_SLOTS, outcome="refunded") == 1


def test_slot_refunded_when_the_query_fails(client, exec_world, monkeypatch):
    headers, sid, uid = exec_world
    monkeypatch.setattr(api_module, "execute_query", lambda *a, **k: {"success": False, "error": "no such column"})
    assert _exec(client, headers, sid).status_code == 400 and _slots(uid) == 0


def test_slot_refunded_when_saving_the_result_conflicts(client, exec_world, monkeypatch):
    headers, sid, uid = exec_world
    real, calls = api_module.update_session, []

    def flaky(db, row, **fields):
        calls.append(1)
        if "query_result" in fields:
            raise SessionConflictError("conflict")
        return real(db, row, **fields)
    monkeypatch.setattr(api_module, "update_session", flaky)
    assert _exec(client, headers, sid).status_code == 409 and _slots(uid) == 0


def test_failing_refund_never_masks_the_original_error_and_is_recorded(client, exec_world, monkeypatch, fresh_store):
    headers, sid, uid = exec_world
    monkeypatch.setattr(api_module, "execute_query", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("executor crashed")))
    monkeypatch.setattr(api_module, "refund_query_slot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("refund failed")))
    with pytest.raises(RuntimeError, match="executor crashed"):
        _exec(client, headers, sid)
    assert fresh_store.snapshot(5).counter(m.QUERY_SLOTS, outcome="refund_failed") == 1


# ---- background jobs --------------------------------------------------------------------------------------------------
def _job(uid, status="queued", age_s=0, kind="dataset_ingestion", reserved=1000, code=None):
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    with SessionLocal() as db:
        j = Job(id=str(uuid.uuid4()), user_id=uid, kind=kind, status=status, error_code=code, created_at=ts, updated_at=ts,
                session_id=str(uuid.uuid4()), reserved_bytes=reserved)
        db.add(j); db.commit()
        return j.id


def _status(job_id):
    with SessionLocal() as db:
        j = db.query(Job).filter(Job.id == job_id).one()
        return j.status, j.error_code, j.result_ref


def test_stale_jobs_are_failed_and_release_their_quota(client):
    headers, email = register_headers(client, "reap")
    uid = _user_id(email)
    stale = _job(uid, "running", age_s=config.JOB_STALE_SECONDS + 60)
    fresh = _job(uid, "queued", age_s=30)
    with SessionLocal() as db:
        before = quotas.usage_for_user(db, uid)["datasets"]
    assert jobs.reap_stale_jobs() >= 1
    status, code, ref = _status(stale)
    assert (status, code) == ("failed", "JOB_STALE") and "did not complete" in ref
    assert _status(fresh)[0] == "queued"                                           # healthy backlog untouched
    with SessionLocal() as db:
        assert quotas.usage_for_user(db, uid)["datasets"] == before - 1


def test_reaped_job_is_not_resurrected_if_the_worker_finally_picks_it_up(client, monkeypatch):
    headers, email = register_headers(client, "resurrect")
    uid = _user_id(email)
    job_id = _job(uid, "failed", code=jobs.STALE_ERROR_CODE)
    ran = []
    monkeypatch.setattr(jobs, "_dispatch", lambda *a: ran.append(1))
    jobs._execute_job(job_id, {"object_ref": None, "ext": ".csv", "fname": "a.csv", "session_id": "x"})
    assert not ran and _status(job_id)[:2] == ("failed", "JOB_STALE")


def test_job_metrics_queue_wait_duration_and_outcomes(client, monkeypatch, fresh_store):
    headers, email = register_headers(client, "jobmet")
    uid = _user_id(email)
    fresh_store.reset()                    # registration itself ran (and counted) an auth_email job; count only ours
    ok_job = _job(uid, "queued", age_s=5, kind="auth_email", reserved=None)
    monkeypatch.setattr(jobs, "_run_email", lambda db, job, payload: {"sent": True})
    jobs._execute_job(ok_job, {})
    assert _status(ok_job)[0] == "succeeded"
    snap = fresh_store.snapshot(5)
    assert snap.counter(m.JOBS, kind="auth_email", outcome="succeeded") == 1
    assert snap.quantile(m.JOB_QUEUE_WAIT, 0.5, kind="auth_email") >= 2500                # ~5s old when started
    assert snap.hist_count(m.JOB_DURATION, kind="auth_email") == 1

    bad_job = _job(uid, "queued", kind="auth_email", reserved=None)
    monkeypatch.setattr(jobs, "MAX_ATTEMPTS", 1)
    monkeypatch.setattr(jobs, "_run_email", lambda *a: (_ for _ in ()).throw(RuntimeError("smtp down for victim@example.com")))
    jobs._execute_job(bad_job, {})
    status, code, ref = _status(bad_job)
    assert (status, code) == ("failed", "JOB_FAILED") and "victim" not in ref
    assert fresh_store.snapshot(5).counter(m.JOBS, kind="auth_email", outcome="failed") == 1


def test_invalid_user_upload_is_not_counted_against_the_job_slo(client, monkeypatch, fresh_store):
    headers, email = register_headers(client, "reject")
    job_id = _job(_user_id(email), "queued")
    monkeypatch.setattr(jobs, "_dispatch", lambda *a: (_ for _ in ()).throw(ValueError("File is empty")))
    jobs._execute_job(job_id, {"object_ref": None, "ext": ".csv", "fname": "a.csv", "session_id": "s"})
    snap = fresh_store.snapshot(5)
    assert snap.counter(m.JOBS, outcome="rejected") == 1 and snap.counter(m.JOBS, outcome="failed") == 0
