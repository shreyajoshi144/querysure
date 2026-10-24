from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import Base, PipelineSession, SessionConflictError, create_session, get_or_create_session, update_session, UsageDaily, refund_query_slot


def test_unknown_and_missing_sessions_are_not_created(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sessions.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    row = create_session(db, "user-a")
    assert get_or_create_session(db, "user-a", row.id).id == row.id
    with pytest.raises(Exception) as exc:
        get_or_create_session(db, "user-a", "missing")
    assert "Session not found" in str(exc.value)
    db.close()


def test_optimistic_update_rejects_same_field_conflict(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sessions.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    a, b = Session(), Session()
    row = create_session(a, "user-a")
    stale = b.query(PipelineSession).filter_by(id=row.id).one()
    update_session(a, row, question="first")
    with pytest.raises(SessionConflictError):
        update_session(b, stale, question="second")
    a.close(); b.close()


def test_optimistic_update_merges_non_conflicting_fields(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sessions.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    a, b = Session(), Session()
    row = create_session(a, "user-a")
    stale = b.query(PipelineSession).filter_by(id=row.id).one()
    update_session(a, row, question="first")
    merged = update_session(b, stale, sql="SELECT 1")
    assert merged.question == "first"
    assert merged.sql == "SELECT 1"
    a.close(); b.close()


def test_query_slot_refund_never_goes_below_zero(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'usage.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    from db import reserve_query_slot
    assert reserve_query_slot(db, "user-a", 1) is not None
    assert refund_query_slot(db, "user-a") is True
    assert refund_query_slot(db, "user-a") is False
    row = db.query(UsageDaily).filter_by(user_id="user-a").one()
    assert row.query_count == 0
    db.close()


def test_answer_trace_route_is_read_only(client, auth_headers):
    from db import SessionLocal, PipelineSession
    db = SessionLocal()
    before = db.query(PipelineSession).count()
    db.close()
    r = client.get("/api/answer-trace", headers=auth_headers)
    assert r.status_code == 404
    assert r.json()["error_code"] == "SESSION_NOT_FOUND"
    db = SessionLocal()
    after = db.query(PipelineSession).count()
    db.close()
    assert after == before


def test_expensive_route_concurrency_limit_returns_429(client, auth_headers, monkeypatch):
    import api as api_module
    monkeypatch.setattr(api_module, "_inflight_acquire", lambda key, limit: False)
    # The middleware awaits this function, so use an async replacement.
    async def denied(key, limit):
        return False
    monkeypatch.setattr(api_module, "_inflight_acquire", denied)
    r = client.post("/api/generate-sql", headers=auth_headers, json={"session_id": "missing", "question": "x"})
    assert r.status_code == 429
    assert r.json()["error_code"] == "CONCURRENCY_LIMIT"


def test_async_handlers_have_no_known_blocking_application_calls():
    import ast
    from pathlib import Path
    tree = ast.parse(Path("api.py").read_text())
    forbidden = {"hashpw", "checkpw", "urlopen", "send", "generate_sql", "validate_sql", "execute_query", "load_database", "build_semantic_layer", "build_vector_store", "retrieve_context", "assess_reliability", "generate_insights", "generate_report"}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    name = call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id if isinstance(call.func, ast.Name) else ""
                    if name in forbidden:
                        # Blocking application calls are permitted only when
                        # nested under run_in_threadpool; login is the only
                        # route that needs async request parsing.
                        parent_text = ast.get_source_segment(Path("api.py").read_text(), call) or ""
                        assert name not in forbidden or "run_in_threadpool" in parent_text


def test_pool_exhaustion_returns_server_busy_503(client, auth_headers, monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import QueuePool
    import db as db_module
    import api as api_module
    from auth import decode_token

    engine = create_engine(
        f"sqlite:///{tmp_path / 'pool.db'}",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.05,
    )
    db_module.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    user_id, _ = decode_token(auth_headers["Authorization"].split(" ", 1)[1])
    holder = Session()
    holder.add(db_module.User(id=user_id, email=f"pool-{user_id}@example.com", hashed_password="x", email_verified=True, auth_version=0))
    holder.commit()
    held = engine.connect()
    monkeypatch.setattr(db_module, "SessionLocal", Session)
    try:
        response = client.post("/api/session", headers=auth_headers, json={})
        assert response.status_code == 503
        assert response.json()["error_code"] == "SERVER_BUSY"
        assert response.headers.get("retry-after") == "5"
    finally:
        held.close()
        holder.close()


def test_inflight_limiter_atomic_local_backend():
    import asyncio
    import api as api_module
    key = "test:inflight:local"
    api_module._inflight_local.clear()
    api_module._async_redis = None
    acquired, backend = asyncio.run(api_module._inflight_acquire(key, 1))
    assert acquired and backend == "local"
    acquired2, backend2 = asyncio.run(api_module._inflight_acquire(key, 1))
    assert not acquired2 and backend2 == "local"
    asyncio.run(api_module._inflight_release(key, backend))
    assert api_module._inflight_local.get(key, 0) == 0
    asyncio.run(api_module._inflight_release(key, backend))
    assert api_module._inflight_local.get(key, 0) == 0


def test_inflight_limiter_redis_atomic_lease_and_recovery(monkeypatch):
    """Same intent as before (atomic limit, 300s TTL, release frees the slot, outage -> local fallback), now
    exercised against fakeredis running the REAL Lua scripts instead of a hand-written copy of the old
    INCR/DECR script, which no longer exists (leases are token-owned sorted-set members)."""
    import asyncio
    import fakeredis
    import api as api_module

    async def scenario():
        fake = fakeredis.FakeAsyncRedis(decode_responses=True)
        monkeypatch.setattr(api_module, "_async_redis", fake)
        key = "test:inflight:redis"
        a, backend = await api_module._inflight_acquire(key, 1)
        assert a and backend == "redis"
        assert 295_000 <= await fake.pttl(key) <= 300_000
        b, _ = await api_module._inflight_acquire(key, 1)
        assert not b and await fake.zcard(key) == 1
        await api_module._inflight_release(key, backend)
        assert await fake.exists(key) == 0
        return fake, key
    fake, key = asyncio.run(scenario())
    # A Redis outage falls back to the local backend; release follows the same lease backend.
    class Down:
        async def eval(self, *args):
            raise RuntimeError("redis down")
    monkeypatch.setattr(api_module, "_async_redis", Down())
    a, backend = asyncio.run(api_module._inflight_acquire(key, 1))
    assert a and backend == "local"
    asyncio.run(api_module._inflight_release(key, backend))
    assert api_module._inflight_local.get(key, 0) == 0


# The real concurrent-write test is collected only in the Postgres CI job. This
# keeps the SQLite suite at zero skips while ensuring the Postgres job exercises
# the actual transaction/row-lock behavior.
if __import__("os").environ.get("TEST_DATABASE_URL", "").startswith("postgresql"):
    def test_postgres_concurrent_writers_no_lost_update():
        import threading
        from db import SessionLocal, create_session, PipelineSession, update_session, SessionConflictError

        from db import User
        seed = SessionLocal()
        owner = User(email=f"pg-writer-{__import__('uuid').uuid4().hex[:8]}@example.com", hashed_password="x")
        seed.add(owner); seed.commit(); seed.refresh(owner)
        row = create_session(seed, owner.id)  # real FK target: Postgres enforces user_id -> users.id
        sid = row.id
        seed.close()
        barrier = threading.Barrier(2)
        results = []

        def writer(field, value):
            db = SessionLocal()
            try:
                stale = db.query(PipelineSession).filter_by(id=sid).one()
                barrier.wait(timeout=10)
                try:
                    updated = update_session(db, stale, **{field: value})
                    results.append(("ok", field, updated.version))
                except SessionConflictError:
                    results.append(("conflict", field, None))
            finally:
                db.close()

        t1 = threading.Thread(target=writer, args=("question", "writer-a"))
        t2 = threading.Thread(target=writer, args=("question", "writer-b"))
        t1.start(); t2.start(); t1.join(15); t2.join(15)
        assert len(results) == 2
        assert sum(r[0] == "ok" for r in results) == 1
        assert sum(r[0] == "conflict" for r in results) == 1

        db = SessionLocal()
        final = db.query(PipelineSession).filter_by(id=sid).one()
        assert final.question in {"writer-a", "writer-b"}
        assert final.version == 1
        db.delete(final); db.commit(); db.close()
