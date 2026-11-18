"""Interval scheduler for connection-backed datasets (Sprint 5). Run it as its own always-on process (``python scheduler.py``, the
compose ``scheduler`` service) or, in development, as a thread inside the API (SCHEDULER_ENABLED=true). It needs an always-on
process, so it is NOT available on Render's free plan (the web service sleeps; there is no free background worker).

Safety properties
  * A due schedule is claimed with one conditional UPDATE of its ``next_run_at``, so any number of scheduler processes fire it once.
  * A run is never doubled: a dataset whose previous run is still active is skipped, and the database allows only one active run
    per dataset anyway.
  * State lives in the database. After downtime a schedule fires once when the scheduler is back, not once per missed interval.
  * Runs act as the user who set the schedule: if they leave the workspace, lose analyst, or the connection is disabled or deleted,
    the schedule disables itself and records why. After SCHEDULER_MAX_CONSECUTIVE_FAILURES failed runs in a row it disables too.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import audit
import config
import ingest
import workspaces as ws_service
from db import Connection, Dataset, DatasetSchedule, Job, SessionLocal, User

logger = logging.getLogger("querysure.scheduler")
HEARTBEAT_FILE = os.environ.get("SCHEDULER_HEARTBEAT_FILE", "/tmp/qs-scheduler.alive")   # touched every tick; the compose healthcheck reads it
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _disable(db, s: DatasetSchedule, reason: str, workspace_id: Optional[str] = None) -> None:
    s.enabled, s.disabled_reason = False, reason
    db.commit()
    ws_id = workspace_id
    if ws_id is None:
        ds = db.get(Dataset, s.dataset_id)
        ws_id = ds.workspace_id if ds else None
    audit.record(ws_id, "schedule.disable", actor_id=None, actor_type="system", target_type="dataset", target_id=s.dataset_id,
                 details={"reason": reason, "dataset_id": s.dataset_id})


def reconcile(db) -> None:
    """Resolve the outcome of the jobs earlier ticks started, so failures are counted even when a worker ran them."""
    for s in db.query(DatasetSchedule).filter(DatasetSchedule.last_status == "queued", DatasetSchedule.last_job_id.isnot(None)).all():
        job = db.get(Job, s.last_job_id)
        if job is not None and job.status not in ("succeeded", "failed"):
            continue                                                  # still queued / running / retrying
        if job is not None and job.status == "succeeded":
            s.last_status, s.consecutive_failures = "succeeded", 0
            db.commit()
        else:
            s.last_status, s.consecutive_failures = "failed", (s.consecutive_failures or 0) + 1
            db.commit()
            if s.consecutive_failures >= config.SCHEDULER_MAX_CONSECUTIVE_FAILURES:
                _disable(db, s, "too_many_failures")


def tick(now: Optional[datetime] = None) -> int:
    """Fire every due schedule once. Returns how many jobs were started."""
    import jobs
    db = SessionLocal()
    fired = 0
    try:
        now = now or _now()
        reconcile(db)
        due = (db.query(DatasetSchedule).filter(DatasetSchedule.enabled.is_(True), DatasetSchedule.next_run_at <= now)
               .order_by(DatasetSchedule.next_run_at).limit(50).all())
        for s in due:
            claimed = (db.query(DatasetSchedule)
                       .filter(DatasetSchedule.id == s.id, DatasetSchedule.enabled.is_(True), DatasetSchedule.next_run_at == s.next_run_at)
                       .update({DatasetSchedule.next_run_at: now + timedelta(minutes=s.interval_minutes),
                                DatasetSchedule.last_run_at: now}, synchronize_session=False))
            db.commit()
            if not claimed:
                continue                                              # another scheduler process fired it
            db.refresh(s)
            ds = db.get(Dataset, s.dataset_id)
            if ds is None:
                _disable(db, s, "dataset_gone")
                continue
            conn = db.get(Connection, ds.connection_id) if ds.connection_id else None
            if conn is None or conn.status != "active" or not ds.source_table:
                _disable(db, s, "connection_unavailable", ds.workspace_id)
                continue
            user = db.get(User, s.created_by)
            member = ws_service.membership(db, ds.workspace_id, s.created_by) if user is not None else None
            if user is None or not user.is_active:
                _disable(db, s, "owner_inactive", ds.workspace_id)
                continue
            if member is None or ws_service.ROLE_RANK.get(member.role, 0) < ws_service.ROLE_RANK["analyst"]:
                _disable(db, s, "owner_access_lost", ds.workspace_id)
                continue
            if ds.status != "ready" or ingest.active_run(db, ds.id):
                s.last_status = "skipped"
                db.commit()
                continue
            try:
                job = jobs.enqueue_job(db, user.id, "dataset_ingestion",
                                       {"connection_id": conn.id, "workspace_id": ds.workspace_id, "table": ds.source_table,
                                        "mode": s.mode, "dataset_id": ds.id}, None)
            except Exception as exc:  # noqa: BLE001 - one bad schedule must not stop the others
                logger.warning("scheduled sync could not be started: %s", type(exc).__name__)
                db.rollback()
                s.last_status, s.consecutive_failures = "failed", (s.consecutive_failures or 0) + 1
                db.commit()
                continue
            s.last_job_id, s.last_status = job.id, "queued"
            db.commit()
            fired += 1
        reconcile(db)                                                 # a job that ran inline is already finished
        return fired
    finally:
        db.close()


def run_forever(stop: threading.Event, tick_seconds: Optional[float] = None) -> None:
    interval = tick_seconds or config.SCHEDULER_TICK_SECONDS
    logger.info("scheduler started", extra={"event": "scheduler_started", "tick_seconds": interval})
    while not stop.is_set():
        try:
            fired = tick()
            if fired:
                logger.info("scheduled syncs started", extra={"event": "scheduler_fired", "count": fired})
        except Exception as exc:  # noqa: BLE001 - the loop must survive a database blip
            logger.warning("scheduler tick failed: %s", type(exc).__name__)
        try:
            with open(HEARTBEAT_FILE, "w") as fh:
                fh.write(str(_now().timestamp()))
        except OSError:
            pass
        stop.wait(interval)
    logger.info("scheduler stopped", extra={"event": "scheduler_stopped"})


def start_in_process() -> Optional[threading.Thread]:
    """Development convenience (SCHEDULER_ENABLED=true): the same loop in a daemon thread of the API process."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread
    _stop.clear()
    _thread = threading.Thread(target=run_forever, args=(_stop,), name="qs-scheduler", daemon=True)
    _thread.start()
    return _thread


def stop_in_process() -> None:
    _stop.set()


def main() -> None:
    from observability.logging_setup import configure_logging
    configure_logging()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    run_forever(stop)


if __name__ == "__main__":
    main()
