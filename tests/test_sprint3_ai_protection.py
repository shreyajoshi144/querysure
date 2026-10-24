from __future__ import annotations

import json
import threading

import fakeredis
import pytest

import ai_guard
import api as api_module
import config
from modules import sql_generator as sg
from db import AIInvocation, SessionLocal, User, UsageDaily
from tests.sprint3_helpers import connect_csv, fake_result, generate, register_headers


@pytest.fixture()
def fake_redis(monkeypatch):
    r = fakeredis.FakeRedis(decode_responses=True)
    r.flushall()  # fakeredis instances share one in-process server; start every test clean
    monkeypatch.setattr(api_module, "redis_client", r)
    monkeypatch.setattr(api_module, "_redis_down_until", 0.0)
    return r


def _spy_generate(monkeypatch, cost=0.5):
    calls = []

    def fake(**kw):
        calls.append(1)
        return fake_result(usage={"input_tokens": 1, "output_tokens": 1, "estimated": False, "cost_cents": cost})
    monkeypatch.setattr(api_module, "generate_sql", fake)
    return calls


def _clear_today_usage():
    """usage_daily is shared across the whole test session; the DB-fallback global cap sums it."""
    with SessionLocal() as db:
        db.query(UsageDaily).delete()
        db.commit()


def _setup(client):
    headers, email = register_headers(client)
    return headers, email, connect_csv(client, headers)


# =========================================================== kill switch
def test_kill_switch_returns_clean_503_and_never_calls_a_provider(client, monkeypatch):
    calls = _spy_generate(monkeypatch)
    provider_calls = []
    monkeypatch.setattr(sg, "_call_groq", lambda *a: provider_calls.append(1))
    headers, _, sid = _setup(client)
    monkeypatch.setattr(config, "AI_KILL_SWITCH", True)
    r = generate(client, headers, sid)
    assert r.status_code == 503
    body = r.json()
    assert body["error_code"] == "AI_DISABLED" and body["success"] is False and "error" in body
    assert r.headers.get("retry-after")
    assert calls == [] and provider_calls == []
    # no spend, no invocation row for a blocked request
    monkeypatch.setattr(config, "AI_KILL_SWITCH", False)
    assert generate(client, headers, sid).status_code == 200  # flipping it back restores service


def test_kill_switch_env_flag_parsing():
    import subprocess, sys, os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for val, expected in [("1", "True"), ("true", "True"), ("ON", "True"), ("0", "False"), ("", "False"), ("no", "False")]:
        out = subprocess.run([sys.executable, "-c", "import config; print(config.AI_KILL_SWITCH)"],
                             env={**os.environ, "AI_KILL_SWITCH": val}, capture_output=True, text=True, cwd=root)
        assert out.stdout.strip() == expected, (val, out.stderr)


# =========================================================== global daily cap
def test_global_cap_blocks_with_503_redis_path_and_refunds(client, fake_redis, monkeypatch):
    calls = _spy_generate(monkeypatch, cost=0.5)
    headers, _, sid = _setup(client)
    est = api_module._estimate_cost_cents_pre_call("total amount by region", {})
    monkeypatch.setattr(api_module, "GLOBAL_DAILY_AI_BUDGET_CENTS", 3.0)  # ~ a handful of requests
    key = api_module._global_budget_key()
    fake_redis.set(key, "2.9999")  # nearly exhausted by "other users"
    r = generate(client, headers, sid)
    assert r.status_code == 503 and r.json()["error_code"] == "AI_CAPACITY_REACHED"
    assert int(r.headers["retry-after"]) > 0 and calls == []
    assert float(fake_redis.get(key)) == pytest.approx(2.9999)          # reservation was refunded
    # the user's own counter was never charged either
    assert fake_redis.get(api_module._budget_key(_user_id(client, headers))) in (None, "0", "0.0")


def _user_id(client, headers):
    return client.get("/api/auth/me", headers=headers).json().get("id") or client.get("/api/auth/me", headers=headers).json()["user"]["id"]


def test_global_reserve_then_reconcile_to_actual_cost(client, fake_redis, monkeypatch):
    _spy_generate(monkeypatch, cost=0.5)
    monkeypatch.setattr(api_module, "GLOBAL_DAILY_AI_BUDGET_CENTS", 1000.0)
    headers, _, sid = _setup(client)
    assert generate(client, headers, sid).status_code == 200
    assert float(fake_redis.get(api_module._global_budget_key())) == pytest.approx(0.5)  # estimate replaced by actual


def test_global_cap_is_shared_across_users(client, fake_redis, monkeypatch):
    # The pre-call reservation is an estimate (a few cents with real retrieval context); each call
    # then reconciles to its real cost (20c). Cap 50c: users 1-3 pass the check, user 4 is blocked.
    _spy_generate(monkeypatch, cost=20.0)
    monkeypatch.setattr(api_module, "GLOBAL_DAILY_AI_BUDGET_CENTS", 50.0)
    results = []
    for _ in range(4):
        headers, _, sid = _setup(client)       # four different users
        results.append(generate(client, headers, sid).status_code)
    assert results[:2] == [200, 200] and results[-1] == 503
    assert float(fake_redis.get(api_module._global_budget_key())) <= 60.0 + 1e-6   # overshoot bounded by one request


def test_user_over_own_budget_gives_global_slot_back(client, fake_redis, monkeypatch):
    _spy_generate(monkeypatch, cost=0.5)
    monkeypatch.setattr(api_module, "GLOBAL_DAILY_AI_BUDGET_CENTS", 1000.0)
    monkeypatch.setattr(api_module, "DAILY_LLM_BUDGET_CENTS", 0.0001)
    headers, _, sid = _setup(client)
    r = generate(client, headers, sid)
    assert r.status_code == 429 and r.json()["error_code"] == "BUDGET_EXCEEDED"
    assert float(fake_redis.get(api_module._global_budget_key()) or 0) == pytest.approx(0.0, abs=1e-9)


def test_global_cap_db_fallback_when_redis_absent(client, monkeypatch):
    monkeypatch.setattr(api_module, "redis_client", None)
    _spy_generate(monkeypatch, cost=20.0)
    _clear_today_usage()
    monkeypatch.setattr(api_module, "GLOBAL_DAILY_AI_BUDGET_CENTS", 30.0)
    codes = []
    for _ in range(3):
        h, _ = register_headers(client)
        codes.append(generate(client, h, connect_csv(client, h)))
    assert [c.status_code for c in codes] == [200, 200, 503]   # 0 <30, 20 <30, 40 >=30
    assert codes[2].json()["error_code"] == "AI_CAPACITY_REACHED" and int(codes[2].headers["retry-after"]) > 0


def test_global_cap_db_fallback_when_redis_goes_down(client, monkeypatch):
    class Dead:
        def __getattr__(self, name):
            def boom(*a, **k):
                raise api_module._redis_lib.exceptions.ConnectionError("down")
            return boom
    monkeypatch.setattr(api_module, "redis_client", Dead())
    monkeypatch.setattr(api_module, "_redis_down_until", 0.0)
    _spy_generate(monkeypatch, cost=20.0)
    _clear_today_usage()
    monkeypatch.setattr(api_module, "GLOBAL_DAILY_AI_BUDGET_CENTS", 15.0)
    h1, _ = register_headers(client)
    assert generate(client, h1, connect_csv(client, h1)).status_code == 200
    h2, _ = register_headers(client)
    assert generate(client, h2, connect_csv(client, h2)).status_code == 503


def test_global_cap_zero_disables_it(client, fake_redis, monkeypatch):
    _spy_generate(monkeypatch, cost=0.5)
    monkeypatch.setattr(api_module, "GLOBAL_DAILY_AI_BUDGET_CENTS", 0.0)
    headers, _, sid = _setup(client)
    assert generate(client, headers, sid).status_code == 200
    assert fake_redis.get(api_module._global_budget_key()) is None


def test_global_reserve_is_atomic_under_concurrency(fake_redis, monkeypatch):
    monkeypatch.setattr(api_module, "GLOBAL_DAILY_AI_BUDGET_CENTS", 10.0)
    wins = []

    def worker():
        wins.append(api_module._reserve_global_atomic(1.0))
    ts = [threading.Thread(target=worker) for _ in range(40)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sum(wins) == 10                                    # never more than the cap allows
    assert float(fake_redis.get(api_module._global_budget_key())) == pytest.approx(10.0)


# =========================================================== per-plan budget multiplier
def test_multipliers_derive_from_entitlements_without_touching_it():
    assert config.plan_budget_multiplier("free") == 1.0
    assert config.plan_budget_multiplier("pro") == 5.0       # 50 / 10 daily queries
    assert config.plan_budget_multiplier("team") == 10.0     # 100 / 10
    assert config.plan_budget_multiplier("business") == config.LLM_UNLIMITED_PLAN_MULTIPLIER
    assert config.plan_budget_multiplier("???") == 1.0 and config.plan_budget_multiplier(None) == 1.0


def test_multiplier_env_override():
    import subprocess, sys, os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ok = subprocess.run([sys.executable, "-c", "import config; print(config.plan_budget_multiplier('pro'))"],
                        env={**os.environ, "PLAN_BUDGET_MULTIPLIERS_JSON": '{"pro": 2.5}'}, capture_output=True, text=True, cwd=root)
    assert ok.stdout.strip() == "2.5"
    bad = subprocess.run([sys.executable, "-c", "import config"], env={**os.environ, "PLAN_BUDGET_MULTIPLIERS_JSON": '{"pro": -1}'},
                         capture_output=True, text=True, cwd=root)
    assert bad.returncode != 0 and "PLAN_BUDGET_MULTIPLIERS_JSON" in bad.stderr


def _set_plan(email, plan):
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).one()
        u.plan = plan
        db.commit()


def test_paid_plan_gets_larger_daily_budget(client, fake_redis, monkeypatch):
    _spy_generate(monkeypatch, cost=20.0)
    monkeypatch.setattr(api_module, "GLOBAL_DAILY_AI_BUDGET_CENTS", 0.0)
    monkeypatch.setattr(api_module, "DAILY_LLM_BUDGET_CENTS", 30.0)   # free cap 30c, pro cap 150c (x5)
    results = {}
    for plan in ("free", "pro"):
        headers, email = register_headers(client, prefix=plan)
        _set_plan(email, plan)
        sid = connect_csv(client, headers)
        results[plan] = [generate(client, headers, sid, f"q{i}").status_code for i in range(5)]
    assert results["free"][:2] == [200, 200] and results["free"][-1] == 429
    assert results["pro"] == [200] * 5


def test_budget_reported_to_client_reflects_plan(client, fake_redis, monkeypatch):
    _spy_generate(monkeypatch, cost=0.1)
    headers, email = register_headers(client)
    _set_plan(email, "team")
    sid = connect_csv(client, headers)
    body = generate(client, headers, sid).json()
    assert body["usage_today"]["budget_usd"] == pytest.approx(config.DAILY_LLM_BUDGET_USD * 10)
    assert client.get("/api/usage", headers=headers).json()["budget_usd"] == pytest.approx(config.DAILY_LLM_BUDGET_USD * 10)


# =========================================================== circuit breaker
class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def test_breaker_state_machine():
    clk = Clock()
    b = ai_guard.CircuitBreaker("x", threshold=3, cooldown=30, clock=clk)
    assert b.state == "closed" and b.allow()
    b.record_failure(); b.record_failure()
    assert b.state == "closed"
    b.record_success()                      # success resets the consecutive count
    b.record_failure(); b.record_failure()
    assert b.state == "closed"
    b.record_failure()                      # 3rd consecutive
    assert b.state == "open" and not b.allow()
    clk.t += 29.9
    assert b.state == "open" and not b.allow()
    clk.t += 0.2                            # cooldown elapsed -> half-open
    assert b.state == "half_open"
    assert b.allow() is True                # the single probe
    assert b.allow() is False               # others still blocked while probing
    b.record_failure()                      # probe failed -> open again, fresh cooldown
    assert b.state == "open" and not b.allow()
    clk.t += 31
    assert b.allow() is True
    b.record_success()                      # probe succeeded -> closed
    assert b.state == "closed" and b.allow() and b.allow()


def test_breaker_thresholds_are_env_driven():
    import subprocess, sys, os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run([sys.executable, "-c", "import config, ai_guard; b=ai_guard.CircuitBreaker('x'); print(b.threshold, b.cooldown)"],
                         env={**os.environ, "CIRCUIT_FAILURE_THRESHOLD": "2", "CIRCUIT_COOLDOWN_SECONDS": "7"},
                         capture_output=True, text=True, cwd=root)
    assert out.stdout.split() == ["2", "7.0"], out.stderr


def _chain_world(monkeypatch, groq_ok, openai_ok=True):
    monkeypatch.setattr(config, "CIRCUIT_FAILURE_THRESHOLD", 2)
    monkeypatch.setattr(config, "CIRCUIT_COOLDOWN_SECONDS", 30.0)
    monkeypatch.setattr(sg, "GROQ_FALLBACK_MODEL", "")
    monkeypatch.setattr(sg, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(sg, "ANTHROPIC_API_KEY", "")
    calls = {"groq": 0, "openai": 0}
    state = {"groq_ok": groq_ok}

    def groq(*a):
        calls["groq"] += 1
        if not state["groq_ok"]:
            raise RuntimeError("groq 500")
        return sg.LLMResult("from-groq", 1, 1)

    def openai(*a):
        calls["openai"] += 1
        return sg.LLMResult("from-openai", 1, 1)
    monkeypatch.setattr(sg, "_call_groq", groq)
    monkeypatch.setattr(sg, "_call_openai", openai)
    return calls, state


def test_open_breaker_skips_provider_without_network_call_and_falls_back(monkeypatch):
    calls, state = _chain_world(monkeypatch, groq_ok=False)
    for _ in range(2):                                             # trips groq (threshold 2)
        text, prov, att = sg._run_provider_chain("s", "u", "gk")
        assert prov.startswith("openai")
    assert calls["groq"] == 2 and ai_guard.get_breaker("groq").state == "open"
    text, prov, att = sg._run_provider_chain("s", "u", "gk")
    assert prov.startswith("openai") and calls["groq"] == 2       # groq NOT called again
    assert att[0].error == "circuit_open"
    assert ai_guard.get_breaker("openai").state == "closed"      # breakers are per provider


def test_half_open_probe_recovers_the_provider(monkeypatch):
    calls, state = _chain_world(monkeypatch, groq_ok=False)
    clk = Clock()
    monkeypatch.setitem(ai_guard._registry, "groq", ai_guard.CircuitBreaker("groq", clock=clk))
    for _ in range(2):
        sg._run_provider_chain("s", "u", "gk")
    assert ai_guard.get_breaker("groq").state == "open"
    clk.t += 31
    state["groq_ok"] = True                                       # provider healed
    text, prov, _ = sg._run_provider_chain("s", "u", "gk")
    assert prov.startswith("groq") and text == "from-groq"        # the probe went to groq and succeeded
    assert ai_guard.get_breaker("groq").state == "closed"


def test_failed_probe_reopens(monkeypatch):
    calls, state = _chain_world(monkeypatch, groq_ok=False)
    clk = Clock()
    monkeypatch.setitem(ai_guard._registry, "groq", ai_guard.CircuitBreaker("groq", clock=clk))
    for _ in range(2):
        sg._run_provider_chain("s", "u", "gk")
    clk.t += 31
    before = calls["groq"]
    sg._run_provider_chain("s", "u", "gk")                        # probe fails
    assert calls["groq"] == before + 1 and ai_guard.get_breaker("groq").state == "open"
    sg._run_provider_chain("s", "u", "gk")
    assert calls["groq"] == before + 1                            # blocked again for a full cooldown


def test_all_breakers_open_fails_through_generation_failed_contract(client, monkeypatch):
    monkeypatch.setattr(config, "CIRCUIT_FAILURE_THRESHOLD", 1)
    monkeypatch.setattr(sg, "GROQ_FALLBACK_MODEL", "")
    monkeypatch.setattr(sg, "OPENAI_API_KEY", "")
    monkeypatch.setattr(sg, "ANTHROPIC_API_KEY", "")
    n = []
    monkeypatch.setattr(sg, "_call_groq", lambda *a: (n.append(1), (_ for _ in ()).throw(RuntimeError("x")))[1])
    headers, _, sid = _setup(client)
    assert generate(client, headers, sid).status_code == 502
    r = generate(client, headers, sid)
    assert r.status_code == 502 and r.json()["error_code"] == "GENERATION_FAILED"
    assert len(n) == 1                                            # second request never hit the network
