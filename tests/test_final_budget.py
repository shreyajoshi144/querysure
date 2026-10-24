"""Final hardening, items 2 + 3: AI budget enforcement never fails open, and every reservation is rolled back
on partial failure."""
from __future__ import annotations

import threading

import fakeredis
import pytest
import redis

import api as api_module
from db import SessionLocal, UsageDaily, User, get_or_create_daily_usage, reserve_daily_budget, settle_daily_budget
from tests.sprint3_helpers import connect_csv, fake_result, generate, register_headers


@pytest.fixture()
def fr(monkeypatch):
    r = fakeredis.FakeRedis(decode_responses=True)
    r.flushall()
    monkeypatch.setattr(api_module, "redis_client", r)
    monkeypatch.setattr(api_module, "_redis_down_until", 0.0)
    return r


class Flaky:
    """Proxy that can fail chosen calls with a real redis error. `fail_on` is a set of 1-based call numbers."""
    def __init__(self, real):
        self.real, self.n, self.fail_on, self.always = real, 0, set(), False

    def _go(self, name, *a, **k):
        self.n += 1
        if self.always or self.n in self.fail_on:
            raise redis.exceptions.ConnectionError("simulated")
        return getattr(self.real, name)(*a, **k)

    def incrbyfloat(self, *a, **k): return self._go("incrbyfloat", *a, **k)
    def expire(self, *a, **k): return self._go("expire", *a, **k)
    def get(self, *a, **k): return self.real.get(*a, **k)


def _spy(monkeypatch, cost=0.5):
    calls = []

    def fake(**kw):
        calls.append(1)
        return fake_result(usage={"input_tokens": 1, "output_tokens": 1, "estimated": False, "cost_cents": cost})
    monkeypatch.setattr(api_module, "generate_sql", fake)
    return calls


def _uid(email):
    with SessionLocal() as db:
        return db.query(User).filter(User.email == email).one().id


def _db_cost(uid):
    with SessionLocal() as db:
        row = db.query(UsageDaily).filter(UsageDaily.user_id == uid, UsageDaily.day == api_module.datetime.now(api_module.timezone.utc).strftime("%Y-%m-%d")).first()
        return (row.estimated_cost_cents, row.requests) if row else (0.0, 0)


def _setup(client):
    h, email = register_headers(client)
    return h, email, connect_csv(client, h)


# ------------------------------------------------------------------ item 2: never fail open
def test_no_redis_at_all_budget_is_still_enforced_atomically_in_the_db(client, monkeypatch):
    monkeypatch.setattr(api_module, "redis_client", None)
    calls = _spy(monkeypatch, cost=20.0)
    monkeypatch.setattr(api_module, "DAILY_LLM_BUDGET_CENTS", 30.0)
    monkeypatch.setattr(api_module, "GLOBAL_DAILY_AI_BUDGET_CENTS", 0.0)
    h, email, sid = _setup(client)
    codes = [generate(client, h, sid, f"q{i}").status_code for i in range(4)]
    assert codes == [200, 200, 429, 429] and len(calls) == 2         # provider NOT called once the cap is hit
    assert _db_cost(_uid(email)) == (pytest.approx(40.0), 2)


def test_redis_down_mid_flight_falls_to_db_ledger_not_to_unmetered(client, fr, monkeypatch):
    flaky = Flaky(fr); flaky.always = True
    monkeypatch.setattr(api_module, "redis_client", flaky)
    calls = _spy(monkeypatch, cost=20.0)
    monkeypatch.setattr(api_module, "DAILY_LLM_BUDGET_CENTS", 30.0)
    monkeypatch.setattr(api_module, "GLOBAL_DAILY_AI_BUDGET_CENTS", 0.0)
    h, email, sid = _setup(client)
    assert [generate(client, h, sid, f"q{i}").status_code for i in range(3)] == [200, 200, 429]
    assert len(calls) == 2


def test_redis_flapping_does_not_reset_or_split_the_budget(client, fr, monkeypatch):
    """Spend recorded while Redis was healthy still counts when Redis later dies (DB ledger holds real spend)."""
    flaky = Flaky(fr)
    monkeypatch.setattr(api_module, "redis_client", flaky)
    calls = _spy(monkeypatch, cost=20.0)
    monkeypatch.setattr(api_module, "DAILY_LLM_BUDGET_CENTS", 30.0)
    monkeypatch.setattr(api_module, "GLOBAL_DAILY_AI_BUDGET_CENTS", 0.0)
    h, email, sid = _setup(client)
    assert generate(client, h, sid, "a").status_code == 200           # via Redis: 20c spent
    assert generate(client, h, sid, "b").status_code == 200           # via Redis: 40c (admitted at 20 < 30)
    flaky.always = True                                                # Redis dies
    r = generate(client, h, sid, "c")
    assert r.status_code == 429 and len(calls) == 2                    # DB ledger already knows 40c >= 30c


def test_db_reservation_is_atomic_under_concurrency():
    """40 threads race for a 10c budget with 1c reservations: the conditional UPDATE admits while cost < limit,
    so exactly 10 win and the ledger never exceeds limit."""
    import uuid
    with SessionLocal() as db:
        u = User(email=f"race-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x")
        db.add(u); db.commit(); uid = u.id
    wins, barrier = [], threading.Barrier(20)
    get_or_create_daily_usage(SessionLocal(), uid)                     # pre-create so threads only race on the UPDATE

    def worker():
        db = SessionLocal()
        try:
            barrier.wait()
            for _ in range(2):
                wins.append(reserve_daily_budget(db, uid, 1.0, 10.0))
        finally:
            db.close()
    ts = [threading.Thread(target=worker) for _ in range(20)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sum(wins) == 10
    assert _db_cost(uid)[0] == pytest.approx(10.0)


def test_database_failure_during_reservation_fails_closed_before_any_provider_call(client, monkeypatch):
    monkeypatch.setattr(api_module, "redis_client", None)
    calls = _spy(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(api_module, "reserve_daily_budget", boom)
    h, _, sid = _setup(client)
    with pytest.raises(RuntimeError, match="db down"):                 # surfaced as a 500 in prod; TestClient re-raises
        client._inner.post("/api/generate-sql", headers=h, json={"session_id": sid, "question": "q", "api_key": "k"})
    assert calls == []                                                 # ZERO provider spend


# ------------------------------------------------------------------ item 3: rollback safety
def _counters(fr, email):
    return (float(fr.get(api_module._budget_key(_uid(email))) or 0.0), float(fr.get(api_module._global_budget_key()) or 0.0))


def test_user_reserve_failure_rolls_back_the_global_reservation_and_uses_db_ledger(client, fr, monkeypatch):
    """Global INCR ok, user INCR raises -> the global reservation must be refunded (it used to leak)."""
    flaky = Flaky(fr)
    monkeypatch.setattr(api_module, "redis_client", flaky)
    monkeypatch.setattr(api_module, "GLOBAL_DAILY_AI_BUDGET_CENTS", 1000.0)
    calls = _spy(monkeypatch, cost=0.5)
    h, email, sid = _setup(client)
    flaky.fail_on = {3}        # global incr=1, global expire=2, user incr=3 -> boom
    r = generate(client, h, sid)
    assert r.status_code == 200 and len(calls) == 1
    user_c, global_c = _counters(fr, email)
    assert global_c == pytest.approx(0.0, abs=1e-9)                    # the partial global reservation was rolled back
    assert user_c == pytest.approx(0.0, abs=1e-9)
    assert _db_cost(_uid(email)) == (pytest.approx(0.5), 1)            # request metered exactly once, in the DB ledger


def test_expire_failure_takes_back_the_increment_and_leaves_no_immortal_key(client, fr, monkeypatch):
    flaky = Flaky(fr)
    monkeypatch.setattr(api_module, "redis_client", flaky)
    monkeypatch.setattr(api_module, "GLOBAL_DAILY_AI_BUDGET_CENTS", 0.0)
    flaky.fail_on = {2}        # user incr ok, user expire fails
    with pytest.raises(redis.exceptions.ConnectionError):
        api_module._reserve_budget_atomic("u-x", 5.0, 100.0)
    assert float(fr.get(api_module._budget_key("u-x")) or 0.0) == pytest.approx(0.0)


def test_user_over_budget_refunds_global_and_never_calls_provider(client, fr, monkeypatch):
    calls = _spy(monkeypatch)
    monkeypatch.setattr(api_module, "GLOBAL_DAILY_AI_BUDGET_CENTS", 1000.0)
    monkeypatch.setattr(api_module, "DAILY_LLM_BUDGET_CENTS", 0.0001)
    h, email, sid = _setup(client)
    assert generate(client, h, sid).status_code == 429 and calls == []
    assert _counters(fr, email) == (pytest.approx(0.0, abs=1e-9), pytest.approx(0.0, abs=1e-9))


def test_provider_crash_rolls_back_redis_reservation(client, fr, monkeypatch):
    monkeypatch.setattr(api_module, "GLOBAL_DAILY_AI_BUDGET_CENTS", 1000.0)

    def crash(**kw):
        raise RuntimeError("provider exploded")
    monkeypatch.setattr(api_module, "generate_sql", crash)
    h, email, sid = _setup(client)
    with pytest.raises(RuntimeError, match="provider exploded"):
        client._inner.post("/api/generate-sql", headers=h, json={"session_id": sid, "question": "q", "api_key": "k"})
    assert _counters(fr, email) == (pytest.approx(0.0, abs=1e-9), pytest.approx(0.0, abs=1e-9))


def test_provider_crash_rolls_back_db_reservation(client, monkeypatch):
    monkeypatch.setattr(api_module, "redis_client", None)

    def crash(**kw):
        raise RuntimeError("provider exploded")
    monkeypatch.setattr(api_module, "generate_sql", crash)
    h, email, sid = _setup(client)
    with pytest.raises(RuntimeError, match="provider exploded"):
        client._inner.post("/api/generate-sql", headers=h, json={"session_id": sid, "question": "q", "api_key": "k"})
    assert _db_cost(_uid(email)) == (pytest.approx(0.0, abs=1e-9), 0)  # reservation fully returned, no phantom request


def test_failed_generation_refunds_reservation_down_to_real_spend_in_both_modes(client, fr, monkeypatch):
    monkeypatch.setattr(api_module, "GLOBAL_DAILY_AI_BUDGET_CENTS", 1000.0)
    monkeypatch.setattr(api_module, "generate_sql", lambda **k: {"success": False, "error": "x", "error_type": "invalid_json",
                                                                 "usage": {"cost_cents": 0.25}})
    h, email, sid = _setup(client)
    assert generate(client, h, sid).status_code == 502
    assert _counters(fr, email) == (pytest.approx(0.25), pytest.approx(0.25))     # only the billed 0.25c remains
    monkeypatch.setattr(api_module, "redis_client", None)
    h2, email2, sid2 = _setup(client)
    assert generate(client, h2, sid2).status_code == 502
    assert _db_cost(_uid(email2))[0] == pytest.approx(0.25)


def test_settle_daily_budget_composes_and_never_goes_negative():
    import uuid
    with SessionLocal() as db:
        u = User(email=f"s-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x")
        db.add(u); db.commit(); uid = u.id
        assert reserve_daily_budget(db, uid, 5.0, 100.0)
        row = settle_daily_budget(db, uid, 5.0, 0.0, count_request=False)       # full rollback
        assert row.estimated_cost_cents == pytest.approx(0.0) and row.requests == 0
        assert reserve_daily_budget(db, uid, 5.0, 100.0)
        row = settle_daily_budget(db, uid, 5.0, 7.5, count_request=True)        # actual > estimate
        assert row.estimated_cost_cents == pytest.approx(7.5) and row.requests == 1
