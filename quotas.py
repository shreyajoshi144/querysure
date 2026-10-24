"""Per-user session / storage quotas (Sprint 3), made concurrency-safe in the final hardening pass.

Why a stored ``pipeline_sessions.dataset_bytes`` and not a live S3/disk scan: it is the simplest correct option --
the byte count is known at ingestion time, works identically on the local and S3 backends, and a quota check is
one indexed GROUP BY instead of an S3 LIST per upload. Datasets shared by several sessions are counted once.

ATOMICITY. A quota is "count what exists, then add one" -- a classic check-then-act race. Every mutation that
consumes quota now runs as:   lock the owner's row  ->  count  ->  insert  ->  commit   in ONE transaction:
  * PostgreSQL: SELECT ... FOR UPDATE on the user's row (other transactions for the SAME user wait; other users
    are unaffected).
  * SQLite: a no-op UPDATE of the user's row takes the database write lock for the transaction.
Because the lock is held until the commit that inserts the session / job, two concurrent requests can never both
observe "one slot left". In-flight uploads are counted too (jobs.session_id / jobs.reserved_bytes), otherwise N
simultaneous uploads would all pass because none has finished ingesting yet.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, text
from sqlalchemy.orm import Session as OrmSession

import config
from db import Job, PipelineSession, User, _uuid_str

# An ingestion job older than this is considered dead and stops counting (retries are capped at 3 and the
# worker job timeout is 15 min, so a live job never legitimately outlasts this).
PENDING_UPLOAD_WINDOW = timedelta(hours=1)


class QuotaExceeded(Exception):
    def __init__(self, quota: str, limit: int, used: int, message: str):
        super().__init__(message)
        self.quota, self.limit, self.used, self.message = quota, limit, used, message

    def as_payload(self) -> dict:
        return {"success": False, "error": self.message, "error_code": "QUOTA_EXCEEDED",
                "quota": self.quota, "limit": self.limit, "used": self.used}


def _fmt_bytes(n: int) -> str:
    return f"{n / (1024 * 1024):.0f} MB" if n < 1024 ** 3 else f"{n / (1024 ** 3):.1f} GB"


def lock_user_for_quota(db: OrmSession, user_id: str) -> None:
    """Serialize quota-consuming transactions for one user. Held until the caller commits or rolls back."""
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("UPDATE users SET id = id WHERE id = :uid"), {"uid": user_id})
    else:
        db.query(User.id).filter(User.id == user_id).with_for_update().one()


def usage_for_user(db: OrmSession, user_id: str, exclude_session_id: str | None = None) -> dict:
    sessions = db.query(func.count(PipelineSession.id)).filter(PipelineSession.user_id == user_id).scalar() or 0
    q = db.query(PipelineSession.db_path, func.max(PipelineSession.dataset_bytes)).filter(
        PipelineSession.user_id == user_id,
        PipelineSession.db_path.isnot(None),
        PipelineSession.dataset_bytes.isnot(None),
    )
    if exclude_session_id:
        q = q.filter(PipelineSession.id != exclude_session_id)
    rows = q.group_by(PipelineSession.db_path).all()
    datasets = len(rows)
    nbytes = int(sum(int(b or 0) for _, b in rows))

    # uploads accepted but not yet ingested still occupy quota (the target session's own pending upload is a
    # replacement of the dataset being excluded above, so it is not double counted)
    pending = db.query(func.count(Job.id), func.coalesce(func.sum(Job.reserved_bytes), 0)).filter(
        Job.user_id == user_id, Job.kind == "dataset_ingestion", Job.status.in_(("queued", "running")),
        Job.created_at >= datetime.now(timezone.utc) - PENDING_UPLOAD_WINDOW,
    )
    if exclude_session_id:
        pending = pending.filter((Job.session_id.is_(None)) | (Job.session_id != exclude_session_id))
    p_count, p_bytes = pending.one()
    return {"sessions": int(sessions), "datasets": datasets + int(p_count or 0), "dataset_bytes": nbytes + int(p_bytes or 0)}


def reclaim_empty_sessions(db: OrmSession, user_id: str, needed: int, *, commit: bool = True) -> int:
    """Delete up to ``needed`` of the user's oldest EMPTY (no dataset) sessions idle longer than the reclaim
    window. They hold no storage; the frontend creates a fresh session per page visit, so without this a
    normal user would eventually hit the session cap just by reloading."""
    if needed <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=config.QUOTA_EMPTY_SESSION_RECLAIM_SECONDS)
    victims = (db.query(PipelineSession)
               .filter(PipelineSession.user_id == user_id, PipelineSession.db_path.is_(None),
                       PipelineSession.updated_at < cutoff)
               .order_by(PipelineSession.updated_at.asc()).limit(needed).all())
    for v in victims:
        db.delete(v)
    if victims:
        db.flush()
        if commit:
            db.commit()
    return len(victims)


def _enforce_session_limit(db: OrmSession, user, *, commit_reclaim: bool) -> None:
    limits = config.plan_quota(getattr(user, "plan", None))
    used = usage_for_user(db, user.id)["sessions"]
    if used >= limits["max_sessions"]:
        used -= reclaim_empty_sessions(db, user.id, used - limits["max_sessions"] + 1, commit=commit_reclaim)
    if used >= limits["max_sessions"]:
        raise QuotaExceeded("sessions", limits["max_sessions"], used,
                            f"You have reached your limit of {limits['max_sessions']} workspace sessions. "
                            "Delete an old session to create a new one.")


def check_session_quota(db: OrmSession, user) -> None:
    """Advisory, NON-atomic check (kept for callers that only want to peek). Use create_session_with_quota to
    actually create a session."""
    _enforce_session_limit(db, user, commit_reclaim=True)


def create_session_with_quota(db: OrmSession, user) -> PipelineSession:
    """Atomically: lock the owner, enforce the session limit, insert the session, commit."""
    try:
        lock_user_for_quota(db, user.id)
        _enforce_session_limit(db, user, commit_reclaim=False)
        row = PipelineSession(id=_uuid_str(), user_id=user.id)
        db.add(row)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    db.refresh(row)
    return row


def _enforce_upload_limit(db: OrmSession, user, incoming_bytes: int, session_id: str | None) -> None:
    limits = config.plan_quota(getattr(user, "plan", None))
    used = usage_for_user(db, user.id, exclude_session_id=session_id)
    if used["datasets"] + 1 > limits["max_datasets"]:
        raise QuotaExceeded("datasets", limits["max_datasets"], used["datasets"],
                            f"You have reached your limit of {limits['max_datasets']} stored datasets. "
                            "Delete a session to free a dataset slot.")
    if used["dataset_bytes"] + int(incoming_bytes) > limits["max_dataset_bytes"]:
        raise QuotaExceeded("storage", limits["max_dataset_bytes"], used["dataset_bytes"],
                            f"This upload would exceed your {_fmt_bytes(limits['max_dataset_bytes'])} storage limit "
                            f"({_fmt_bytes(used['dataset_bytes'])} already used). Delete a session to free space.")


def check_upload_quota(db: OrmSession, user, incoming_bytes: int, session_id: str | None) -> None:
    """Advisory, NON-atomic pre-check used before the (expensive) staging step so an obviously over-quota upload
    is rejected cheaply. The authoritative decision is lock_and_reserve_upload at enqueue time."""
    _enforce_upload_limit(db, user, incoming_bytes, session_id)


def lock_and_reserve_upload(db: OrmSession, user, incoming_bytes: int, session_id: str | None) -> None:
    """Authoritative upload quota check. Takes the owner lock and leaves the transaction OPEN: the caller must
    create the ingestion job (carrying ``session_id`` and ``reserved_bytes=incoming_bytes``) and commit in the
    same transaction -- enqueue_job does exactly that -- so no other upload can slip in between check and
    reservation. Raises QuotaExceeded (after rolling back, which releases the lock)."""
    try:
        lock_user_for_quota(db, user.id)
        _enforce_upload_limit(db, user, incoming_bytes, session_id)
    except BaseException:
        db.rollback()
        raise
