"""Sprint 4 (found by the load test): bounded in-flight API requests per process, with fast 503 shedding.

Background: every request holds a pooled DB connection across several worker-thread hops, so admitting more concurrent requests
than threads produced a circular wait that only broke at the 10 s pool timeout (a burst of 503s). See config.API_ADMISSION_LIMIT.
The end-to-end proof (50/100/200 simultaneous users) is loadtests/burst_session.py + docs/03_EVALUATION.md (load testing); these tests pin the
mechanism."""
from __future__ import annotations

import asyncio

import httpx
import pytest

import api as api_module
import config
from observability import metrics as m
from tests.sprint4_helpers import fresh_store  # noqa: F401


@pytest.fixture()
def slow_routes(monkeypatch):
    async def slow():
        await asyncio.sleep(0.4)
        return {"ok": True}

    async def boom():
        raise RuntimeError("boom")
    api_module.app.add_api_route("/api/_s4_slow", slow, methods=["GET"])
    api_module.app.add_api_route("/api/_s4_boom2", boom, methods=["GET"])
    api_module._admission_state.update(loop=None, sem=None)
    yield
    api_module.app.router.routes[:] = [r for r in api_module.app.router.routes if not getattr(r, "path", "").startswith(("/api/_s4_slow", "/api/_s4_boom2"))]
    api_module._admission_state.update(loop=None, sem=None)


def _run(coro):
    return asyncio.run(coro)


async def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_module.app, raise_app_exceptions=False), base_url="http://t")


def test_excess_requests_are_shed_fast_with_503_and_retry_after(monkeypatch, slow_routes, fresh_store):
    monkeypatch.setattr(config, "API_ADMISSION_LIMIT", 2)
    monkeypatch.setattr(config, "API_ADMISSION_WAIT_SECONDS", 0.05)

    async def go():
        async with await _client() as c:
            return await asyncio.gather(*[c.get("/api/_s4_slow") for _ in range(5)])
    res = _run(go())
    codes = sorted(r.status_code for r in res)
    assert codes == [200, 200, 503, 503, 503]
    busy = [r for r in res if r.status_code == 503][0]
    assert busy.json()["error_code"] == "SERVER_BUSY" and busy.headers["Retry-After"] == "2" and busy.json()["request_id"]
    assert fresh_store.snapshot(5).counter(m.ADMISSION_REJECTED) == 3


def test_queued_requests_are_served_when_slots_free_up(monkeypatch, slow_routes):
    monkeypatch.setattr(config, "API_ADMISSION_LIMIT", 1)
    monkeypatch.setattr(config, "API_ADMISSION_WAIT_SECONDS", 5.0)

    async def go():
        async with await _client() as c:
            return await asyncio.gather(*[c.get("/api/_s4_slow") for _ in range(3)])
    assert [r.status_code for r in _run(go())] == [200, 200, 200]          # waited their turn, none shed


def test_probes_and_non_api_paths_bypass_admission(monkeypatch, slow_routes):
    monkeypatch.setattr(config, "API_ADMISSION_LIMIT", 1)
    monkeypatch.setattr(config, "API_ADMISSION_WAIT_SECONDS", 0.05)

    async def go():
        async with await _client() as c:
            busy = asyncio.create_task(c.get("/api/_s4_slow"))
            await asyncio.sleep(0.1)                                         # the single slot is now taken
            health = await c.get("/api/health")
            ready = await c.get("/api/ready")
            home = await c.get("/")
            shed = await c.get("/api/_s4_slow")
            return health.status_code, ready.status_code, home.status_code, shed.status_code, (await busy).status_code
    health, ready, home, shed, first = _run(go())
    assert health == 200 and ready in (200, 503) and home != 503 and shed == 503 and first == 200


def test_slot_is_released_when_the_handler_raises(monkeypatch, slow_routes):
    monkeypatch.setattr(config, "API_ADMISSION_LIMIT", 1)
    monkeypatch.setattr(config, "API_ADMISSION_WAIT_SECONDS", 0.2)

    async def go():
        async with await _client() as c:
            statuses = [(await c.get("/api/_s4_boom2")).status_code for _ in range(3)]
            return statuses, (await c.get("/api/_s4_slow")).status_code
    statuses, after = _run(go())
    assert statuses == [500, 500, 500] and after == 200                      # a crashing handler never leaks the slot


def test_default_limit_is_below_the_threadpool_so_threads_cannot_be_starved():
    assert 2 <= config.API_ADMISSION_LIMIT < config.THREADPOOL_MAX_WORKERS
