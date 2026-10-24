"""Regression tests for the token-owned Redis concurrency lease (final hardening, item 1)."""
from __future__ import annotations

import asyncio

import fakeredis
import pytest

import api as api_module

KEY = "querysure:inflight:u1:generate-sql"


@pytest.fixture()
def redis_env(monkeypatch):
    holder = {}

    async def make():
        r = fakeredis.FakeAsyncRedis(decode_responses=True)
        await r.flushall()
        monkeypatch.setattr(api_module, "_async_redis", r)
        holder["r"] = r
        return r
    return make


def run(coro):
    return asyncio.run(coro)


def test_limit_is_enforced_and_each_lease_has_a_unique_token(redis_env):
    async def go():
        r = await redis_env()
        leases = [await api_module._inflight_acquire(KEY, 2) for _ in range(3)]
        assert [ok for ok, _ in leases] == [True, True, False]
        t1, t2 = leases[0][1].token, leases[1][1].token
        assert t1 and t2 and t1 != t2
        assert set(await r.zrange(KEY, 0, -1)) == {t1, t2}
    run(go())


def test_release_removes_only_the_owners_token(redis_env):
    async def go():
        r = await redis_env()
        _, a = await api_module._inflight_acquire(KEY, 2)
        _, b = await api_module._inflight_acquire(KEY, 2)
        await api_module._inflight_release(KEY, a)
        assert await r.zrange(KEY, 0, -1) == [b.token]
        await api_module._inflight_release(KEY, a)          # double release: no-op, b untouched
        assert await r.zrange(KEY, 0, -1) == [b.token]
        ok, _ = await api_module._inflight_acquire(KEY, 2)  # exactly one slot is free again
        assert ok
        ok2, _ = await api_module._inflight_acquire(KEY, 2)
        assert not ok2
    run(go())


def test_stale_release_after_expiry_cannot_free_a_newer_holders_slot(redis_env):
    """The old INCR/DECR counter let a late release decrement a different request's slot."""
    async def go():
        r = await redis_env()
        _, old = await api_module._inflight_acquire(KEY, 1)
        await r.zadd(KEY, {old.token: 0})                    # force the old lease to be expired
        ok, new = await api_module._inflight_acquire(KEY, 1)  # prunes the expired lease, admits new holder
        assert ok
        await api_module._inflight_release(KEY, old)          # the old request's late release arrives
        assert await r.zrange(KEY, 0, -1) == [new.token]      # new holder's slot is intact
        ok2, _ = await api_module._inflight_acquire(KEY, 1)
        assert not ok2                                         # limit still enforced
    run(go())


def test_lost_release_is_reclaimed_by_expiry_not_stranded_forever(redis_env):
    async def go():
        r = await redis_env()
        ok, lease = await api_module._inflight_acquire(KEY, 1)
        assert ok
        # the release never happens (crash / dropped reply). Slot is still held...
        assert not (await api_module._inflight_acquire(KEY, 1))[0]
        # ...until the lease expires on its own
        await r.zadd(KEY, {lease.token: 0})
        assert (await api_module._inflight_acquire(KEY, 1))[0]
    run(go())


def test_key_itself_expires_if_nothing_else_touches_it(redis_env):
    async def go():
        r = await redis_env()
        await api_module._inflight_acquire(KEY, 1)
        ttl = await r.pttl(KEY)
        assert 0 < ttl <= api_module._INFLIGHT_TTL * 1000
    run(go())


def test_release_retries_once_on_a_dropped_reply(monkeypatch, redis_env):
    async def go():
        r = await redis_env()
        _, lease = await api_module._inflight_acquire(KEY, 1)
        real_eval, calls = r.eval, {"n": 0}

        async def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("reply lost")
            return await real_eval(*a, **k)
        monkeypatch.setattr(r, "eval", flaky)
        await api_module._inflight_release(KEY, lease)
        assert calls["n"] == 2 and await r.exists(KEY) == 0
    run(go())


def test_lost_acquire_reply_does_not_strand_a_redis_slot_and_falls_back_locally(monkeypatch, redis_env):
    """Redis ran the acquire script, but the reply never arrived. We must not leave that token holding a slot."""
    async def go():
        r = await redis_env()
        real_eval, calls = r.eval, {"n": 0}

        async def lossy(script, numkeys, key, *args):
            calls["n"] += 1
            result = await real_eval(script, numkeys, key, *args)
            if calls["n"] == 1:                              # the acquire: applied, then reply lost
                raise ConnectionError("reply lost")
            return result
        monkeypatch.setattr(r, "eval", lossy)
        api_module._inflight_local.clear()
        ok, lease = await api_module._inflight_acquire(KEY, 1)
        assert ok and lease == "local"                       # served via the bounded local fallback
        assert await r.exists(KEY) == 0                      # the orphaned Redis token was removed
    run(go())


def test_release_without_a_token_never_touches_anything(redis_env):
    async def go():
        r = await redis_env()
        _, lease = await api_module._inflight_acquire(KEY, 1)
        await api_module._inflight_release(KEY, "redis")     # legacy/plain backend string: no ownership proof
        assert await r.zrange(KEY, 0, -1) == [lease.token]
    run(go())


def test_concurrent_acquires_never_exceed_the_limit(redis_env):
    async def go():
        r = await redis_env()
        results = await asyncio.gather(*[api_module._inflight_acquire(KEY, 5) for _ in range(40)])
        assert sum(1 for ok, _ in results if ok) == 5
        assert await r.zcard(KEY) == 5
        for ok, lease in results:
            if ok:
                await api_module._inflight_release(KEY, lease)
        assert await r.exists(KEY) == 0
    run(go())


def test_local_backend_is_token_owned_too(monkeypatch):
    monkeypatch.setattr(api_module, "_async_redis", None)
    api_module._inflight_local.clear()

    async def go():
        ok1, a = await api_module._inflight_acquire(KEY, 1)
        ok2, b = await api_module._inflight_acquire(KEY, 1)
        assert ok1 and not ok2
        await api_module._inflight_release(KEY, b)           # the REJECTED request's release must not free a's slot
        assert not (await api_module._inflight_acquire(KEY, 1))[0]
        await api_module._inflight_release(KEY, a)
        assert (await api_module._inflight_acquire(KEY, 1))[0]
    run(go())
    api_module._inflight_local.clear()


def test_middleware_releases_the_lease_when_the_handler_raises(client, monkeypatch):
    """End-to-end: a failing request still frees its slot (finally + shield), so the next one is admitted."""
    api_module._inflight_local.clear()
    monkeypatch.setattr(api_module, "_async_redis", None)
    from tests.sprint3_helpers import register_headers
    headers, _ = register_headers(client)
    for _ in range(4):                                       # limit is small; leaked slots would trip CONCURRENCY_LIMIT
        r = client._inner.post("/api/execute-sql", headers=headers, json={"session_id": "nope", "sql": "SELECT 1"})
        assert r.status_code != 429
    assert not api_module._inflight_local
