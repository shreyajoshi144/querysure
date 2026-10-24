"""Final hardening, item 4: quota enforcement is atomic under concurrent requests (SQLite + PostgreSQL)."""
from __future__ import annotations

import io
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import config
import jobs
import quotas
from db import Job, PipelineSession, SessionLocal, User
from tests.sprint3_helpers import register_headers

THREADS = 16


@pytest.fixture(autouse=True)
def _app_started(client):
    """The `client` fixture boots the app (creates the schema); direct-DB tests need it too."""
    return client


@pytest.fixture()
def limits(monkeypatch):
    def apply(**kw):
        monkeypatch.setitem(config.QUOTA_LIMITS, "free", {**config.QUOTA_LIMITS["free"], **kw})
    return apply


@pytest.fixture()
def pending_jobs(monkeypatch):
    """enqueue_job runs ingestion inline in dev; keep jobs 'queued' (in flight) so reservations are visible."""
    monkeypatch.setattr(jobs, "_execute_job", lambda *a, **k: None)
    monkeypatch.setattr(jobs, "_redis", lambda: None)


def _user():
    with SessionLocal() as db:
        u = User(email=f"q-{uuid.uuid4().hex[:10]}@example.com", hashed_password="x")
        db.add(u); db.commit(); db.refresh(u)
        return u.id


def _new_session(uid):
    with SessionLocal() as db:
        s = PipelineSession(id=str(uuid.uuid4()), user_id=uid)
        db.add(s); db.commit()
        return s.id


def _race(fn, n=THREADS):
    """Run fn(i) in n threads released simultaneously; returns list of results (exceptions captured)."""
    barrier, out = threading.Barrier(n), [None] * n

    def worker(i):
        try:
            barrier.wait()
            out[i] = fn(i)
        except BaseException as e:  # noqa: BLE001
            out[i] = e
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in ts]; [t.join() for t in ts]
    return out


def _as_user(uid):
    with SessionLocal() as db:
        return db.get(User, uid)


# ------------------------------------------------------------------ sessions
def test_concurrent_session_creation_never_exceeds_the_limit(limits):
    limits(max_sessions=5)
    uid = _user()

    def create(_):
        with SessionLocal() as db:
            return quotas.create_session_with_quota(db, db.get(User, uid)).id
    out = _race(create)
    ok = [r for r in out if isinstance(r, str)]
    denied = [r for r in out if isinstance(r, quotas.QuotaExceeded)]
    crashed = [r for r in out if isinstance(r, BaseException) and not isinstance(r, quotas.QuotaExceeded)]
    assert not crashed, crashed
    assert len(ok) == 5 and len(denied) == THREADS - 5
    with SessionLocal() as db:
        assert db.query(PipelineSession).filter(PipelineSession.user_id == uid).count() == 5


def test_concurrent_session_creation_via_the_api(client, limits):
    limits(max_sessions=4)
    headers, _ = register_headers(client)

    def hit(_):
        return client._inner.post("/api/session", headers=headers, json={}).status_code
    codes = _race(hit, 10)
    assert sorted(c for c in codes if isinstance(c, int)).count(200) == 4
    assert all(c in (200, 403) for c in codes), codes


def test_quota_lock_is_per_user_other_users_are_unaffected(limits):
    limits(max_sessions=1)
    a, b = _user(), _user()
    res = _race(lambda i: quotas.create_session_with_quota(SessionLocal(), _as_user(a if i % 2 else b)).id, 8)
    assert sum(isinstance(r, str) for r in res) == 2             # one per user, never 0 or 2 for either


# ------------------------------------------------------------------ uploads
def _upload(uid, size, session_id=None):
    with SessionLocal() as db:
        user = db.get(User, uid)
        sid = session_id or str(uuid.uuid4())
        if session_id is None:
            db.add(PipelineSession(id=sid, user_id=uid)); db.commit()
        quotas.lock_and_reserve_upload(db, user, size, sid)
        job = jobs.enqueue_job(db, uid, "dataset_ingestion", {"session_id": sid, "size": size, "object_ref": "x", "ext": ".csv", "fname": "a.csv"})
        return job.id


def test_concurrent_uploads_never_exceed_the_dataset_count(limits, pending_jobs):
    limits(max_datasets=3, max_dataset_bytes=10**12, max_sessions=100)
    uid = _user()
    out = _race(lambda i: _upload(uid, 10))
    assert not [r for r in out if isinstance(r, BaseException) and not isinstance(r, quotas.QuotaExceeded)], out
    assert sum(isinstance(r, str) for r in out) == 3
    with SessionLocal() as db:
        assert db.query(Job).filter(Job.user_id == uid, Job.kind == "dataset_ingestion").count() == 3


def test_concurrent_uploads_never_exceed_the_byte_quota(limits, pending_jobs):
    limits(max_datasets=100, max_dataset_bytes=1000, max_sessions=100)
    uid = _user()
    out = _race(lambda i: _upload(uid, 400))
    assert sum(isinstance(r, str) for r in out) == 2             # 400 + 400 <= 1000 < 1200
    denied = [r for r in out if isinstance(r, quotas.QuotaExceeded)]
    assert denied and all(d.quota == "storage" for d in denied)
    with SessionLocal() as db:
        assert db.query(Job).filter(Job.user_id == uid).with_entities(__import__("sqlalchemy").func.sum(Job.reserved_bytes)).scalar() == 800


def test_in_flight_uploads_count_even_before_ingestion_finishes(limits, pending_jobs):
    limits(max_datasets=1, max_dataset_bytes=10**12, max_sessions=100)
    uid = _user()
    _upload(uid, 10)                                               # accepted, still queued -> no session has a dataset yet
    with pytest.raises(quotas.QuotaExceeded) as e:
        _upload(uid, 10)
    assert e.value.quota == "datasets" and e.value.used == 1


def test_reuploading_into_the_same_session_is_a_replacement_not_a_second_dataset(limits, pending_jobs):
    limits(max_datasets=1, max_dataset_bytes=10**12, max_sessions=100)
    uid = _user()
    sid = _new_session(uid)
    _upload(uid, 10, session_id=sid)
    _upload(uid, 10, session_id=sid)                               # same target session: allowed


def test_finished_and_stale_jobs_stop_counting(limits, pending_jobs):
    limits(max_datasets=1, max_dataset_bytes=10**12, max_sessions=100)
    uid = _user()
    jid = _upload(uid, 10)
    with SessionLocal() as db:
        db.get(Job, jid).status = "succeeded"; db.commit()         # finished (its dataset is counted via the session instead)
    _upload(uid, 10)
    uid2 = _user()
    jid2 = _upload(uid2, 10)
    with SessionLocal() as db:
        db.get(Job, jid2).created_at = datetime.now(timezone.utc) - timedelta(hours=2); db.commit()   # dead job
    _upload(uid2, 10)


def test_rejected_upload_releases_the_lock_and_leaves_nothing_behind(limits, pending_jobs):
    limits(max_datasets=1, max_dataset_bytes=10**12, max_sessions=100)
    uid = _user()
    _upload(uid, 10)
    for _ in range(3):                                             # repeated rejections must not wedge the user's lock
        with pytest.raises(quotas.QuotaExceeded):
            _upload(uid, 10)
    with SessionLocal() as db:
        assert db.query(Job).filter(Job.user_id == uid).count() == 1


def test_concurrent_uploads_via_the_api_respect_the_quota(client, limits, monkeypatch):
    limits(max_datasets=2, max_dataset_bytes=10**12, max_sessions=100)
    headers, _ = register_headers(client)          # registration needs the real (inline) email job
    sids = [client._inner.post("/api/session", headers=headers, json={}).json()["session_id"] for _ in range(6)]
    monkeypatch.setattr(jobs, "_execute_job", lambda *a, **k: None)   # keep uploads in flight from here on
    monkeypatch.setattr(jobs, "_redis", lambda: None)

    def up(i):
        return client._inner.post("/api/connect/upload", headers=headers, data={"session_id": sids[i]},
                                  files={"file": ("a.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")}).status_code
    codes = _race(up, 6)
    assert sorted(c for c in codes if isinstance(c, int)).count(202) <= 2, codes
    assert all(c in (202, 403, 429) for c in codes), codes        # 429 = per-user in-flight limiter, also correct
