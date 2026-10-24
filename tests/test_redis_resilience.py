"""
IMP-3: a Redis outage AFTER startup used to be fatal in two independent
places -- the slowapi rate limiter (register/login/generate-sql all 500 on
every request) and the bespoke LLM-budget reservation in /api/generate-sql
(no error handling around redis_client calls at all). Both are tested here
against real failure paths, not reimplementations of the fix's own logic.
"""
from __future__ import annotations

from tests.auth_test_utils import verify_registered_email

import time
import uuid

import pytest
import redis
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

import api as api_module

UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"  # port 1: nothing listens there, fails fast


# ===================================================================
# 1) slowapi rate limiter: outage must degrade, not 500
# ===================================================================

def _build_mini_app(**limiter_kwargs) -> FastAPI:
    """A tiny app wired the exact same way api.py wires its real limiter
    (same storage_uri pattern, same per-route decorator style), isolated
    from api_module's global state so these tests can't interfere with it."""
    app = FastAPI()
    limiter = Limiter(key_func=get_remote_address, storage_uri=UNREACHABLE_REDIS, **limiter_kwargs)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    @app.get("/ping")
    @limiter.limit("5/minute")
    async def ping(request: Request):
        return {"ok": True}

    return app


def test_without_fallback_redis_outage_returns_500():
    """Pins the bug: this is api.py's OLD construction
    (`Limiter(key_func=..., storage_uri=REDIS_URL)`, no fallback flag)."""
    app = _build_mini_app()  # in_memory_fallback_enabled defaults to False
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/ping")
    assert r.status_code == 500


def test_with_fallback_redis_outage_degrades_instead_of_500():
    """The fix: same unreachable Redis, in_memory_fallback_enabled=True."""
    app = _build_mini_app(in_memory_fallback_enabled=True)
    with TestClient(app) as c:
        r = c.get("/ping")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_fallback_still_enforces_the_configured_limit():
    """Not just 'doesn't crash' -- the same threshold is enforced in-memory."""
    app = _build_mini_app(in_memory_fallback_enabled=True)
    with TestClient(app) as c:
        codes = [c.get("/ping").status_code for _ in range(7)]
    assert codes[:5] == [200] * 5
    assert 429 in codes[5:]


def test_production_limiter_is_actually_wired_with_the_fallback_flag():
    """Direct pin on api.py's real construction, so this fails immediately
    if the flag is ever removed/reverted there -- the tests above only prove
    slowapi's library behavior in general."""
    assert api_module.limiter._in_memory_fallback_enabled is True


# ===================================================================
# 2) /api/generate-sql LLM budget: Redis outage mid-flight
# ===================================================================

class _FlakyRedis:
    """Wraps a real redis-compatible client (fakeredis) and can be told to
    fail on command, so failures exercise api.py's actual except-branches
    against a real redis.exceptions error, not a generic Exception."""

    def __init__(self, real):
        self._real = real
        self.broken = False
        self.calls = 0

    def _maybe_raise(self):
        self.calls += 1
        if self.broken:
            raise redis.exceptions.ConnectionError("simulated outage")

    def incrbyfloat(self, *a, **k):
        self._maybe_raise()
        return self._real.incrbyfloat(*a, **k)

    def expire(self, *a, **k):
        self._maybe_raise()
        return self._real.expire(*a, **k)


@pytest.fixture()
def flaky_redis(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    proxy = _FlakyRedis(fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(api_module, "redis_client", proxy)
    monkeypatch.setattr(api_module, "_redis_down_until", 0.0)
    yield proxy
    monkeypatch.setattr(api_module, "_redis_down_until", 0.0)


@pytest.fixture()
def stub_llm(monkeypatch):
    def fake_generate_sql(**kwargs):
        return {"success": True, "sql": "SELECT 1", "explanation": "stub",
                "confidence": 90, "complexity": "simple", "provider_used": "stub"}
    monkeypatch.setattr(api_module, "generate_sql", fake_generate_sql)


def _register(client) -> dict:
    email = f"redis-{uuid.uuid4().hex[:10]}@example.com"
    pw = "TestPassword123!"
    assert client.post("/api/auth/register", json={"email": email, "password": pw}).status_code == 200
    verify_registered_email(client, email)
    r = client.post("/api/auth/login", json={"email": email, "password": pw})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _upload_and_connect(client, headers):
    import pandas as pd
    df = pd.DataFrame({"region": ["N", "S"], "amount": [1.0, 2.0]})
    r = client.post("/api/connect/upload", headers=headers,
                    files={"file": ("t.csv", df.to_csv(index=False).encode(), "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _ask(client, headers, sid, q="how much revenue"):
    return client.post("/api/generate-sql", headers=headers,
                       json={"session_id": sid, "question": q, "api_key": "stub-key"})


def test_generate_sql_survives_redis_down_at_reserve_time(client, flaky_redis, stub_llm):
    """Old code: an uncaught ConnectionError here was a bare 500 for every
    user, on the one route that actually spends money."""
    headers = _register(client)
    sid = _upload_and_connect(client, headers)
    flaky_redis.broken = True

    r = _ask(client, headers, sid)
    assert r.status_code == 200, r.text
    assert r.json()["sql"] == "SELECT 1"


def test_budget_still_enforced_through_db_fallback_when_redis_down(client, flaky_redis, stub_llm, monkeypatch):
    """Not just 'doesn't crash' -- falling back to the DB check must still
    actually enforce the daily budget, not silently allow unlimited spend."""
    import db as db_module

    monkeypatch.setattr(api_module, "DAILY_LLM_BUDGET_CENTS", 1.0)
    headers = _register(client)
    sid = _upload_and_connect(client, headers)
    flaky_redis.broken = True

    r1 = _ask(client, headers, sid, "q1")
    assert r1.status_code == 200, r1.text  # first call spends under the (tiny) budget

    r2 = _ask(client, headers, sid, "q2")
    assert r2.status_code == 429
    assert r2.json()["error_code"] == "BUDGET_EXCEEDED"


def test_circuit_breaker_skips_redis_on_repeated_calls_during_outage(client, flaky_redis, stub_llm):
    """A sustained outage shouldn't pay a fresh Redis connection attempt on
    every single request once we already know it's down."""
    headers = _register(client)
    sid = _upload_and_connect(client, headers)
    flaky_redis.broken = True

    _ask(client, headers, sid, "q1")
    calls_after_first = flaky_redis.calls
    assert calls_after_first > 0

    _ask(client, headers, sid, "q2")
    assert flaky_redis.calls == calls_after_first, "breaker should have skipped Redis entirely on the 2nd call"


def test_breaker_recovers_once_redis_is_healthy_again(client, flaky_redis, stub_llm, monkeypatch):
    headers = _register(client)
    sid = _upload_and_connect(client, headers)

    flaky_redis.broken = True
    _ask(client, headers, sid, "q1")  # trips the breaker
    assert api_module._redis_down_until > time.time()

    flaky_redis.broken = False
    monkeypatch.setattr(api_module, "_redis_down_until", 0.0)  # cooldown elapsed
    calls_before = flaky_redis.calls
    _ask(client, headers, sid, "q2")
    assert flaky_redis.calls > calls_before, "should have tried Redis again, not stayed on the DB path forever"
    assert api_module._redis_down_until == 0.0


def test_reconcile_failure_after_successful_reserve_does_not_break_the_response(client, flaky_redis, stub_llm):
    """Redis drops between the reserve and the reconcile (a narrower window
    than 'down for the whole request') -- the caller already got a real SQL
    result and must not see that turn into an error."""
    headers = _register(client)
    sid = _upload_and_connect(client, headers)

    real_incrbyfloat = flaky_redis.incrbyfloat
    state = {"n": 0}

    def flip_after_reserve(*a, **k):
        state["n"] += 1
        if state["n"] == 1:  # the reserve call succeeds
            return real_incrbyfloat(*a, **k)
        raise redis.exceptions.ConnectionError("simulated outage")  # reconcile fails

    flaky_redis.incrbyfloat = flip_after_reserve
    r = _ask(client, headers, sid)
    assert r.status_code == 200, r.text
    assert r.json()["sql"] == "SELECT 1"
