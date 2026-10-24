"""Privacy-safe operational event + incident tracking (Sprint 4).

Events are *facts about the system* ("circuit opened for provider groq", "global AI budget at 83%"). They are written with:
  * an allow-list of detail keys (``ALLOWED_DETAIL_KEYS``) and short, charset-restricted scalar values, so no prompt, SQL,
    dataset name, user id or e-mail can be smuggled in by a careless call site;
  * a short-lived, separate DB session, off the request path (bounded queue + one writer thread), so an event can never
    block, fail or roll back a user request. If the queue is full or the DB is down the event is dropped (and counted),
    the log line still exists.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from observability import metrics

logger = logging.getLogger("querysure.ops")

ALLOWED_DETAIL_KEYS = frozenset({
    "provider", "reason", "count", "threshold", "value", "window_minutes", "component", "state", "from_state", "to_state",
    "pct", "limit", "depth", "oldest_age_s", "workers", "utilization_pct", "status", "slo", "sli", "target", "burn_short",
    "burn_long", "route", "kind", "outcome", "samples", "p95_ms", "p99_ms", "error_rate_pct", "spent_pct", "providers_open",
    "providers_total", "fingerprint", "exc_type", "reaped", "released", "model", "attempts", "error_code",
})
_SAFE_STR = re.compile(r"^[A-Za-z0-9_.:/\- ]{0,80}$")
SEVERITIES = ("info", "warning", "critical")


def sanitize_detail(detail: Optional[dict]) -> dict:
    """Keep only allow-listed keys with number / bool / short safe-string values."""
    clean: dict = {}
    for key, value in (detail or {}).items():
        if key not in ALLOWED_DETAIL_KEYS:
            continue
        if isinstance(value, bool) or value is None:
            clean[key] = value
        elif isinstance(value, (int, float)):
            clean[key] = round(float(value), 3) if isinstance(value, float) else value
        elif isinstance(value, str) and _SAFE_STR.match(value):
            clean[key] = value
    return clean


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _insert_event(kind: str, severity: str, component: str, detail: dict) -> None:
    from db import OpsEvent, SessionLocal
    db = SessionLocal()
    try:
        db.add(OpsEvent(kind=kind[:64], severity=severity, component=component[:32], detail=detail or None))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        raise
    finally:
        db.close()


_queue: "queue.Queue[tuple]" = queue.Queue(maxsize=500)
_writer: Optional[threading.Thread] = None
_writer_pid: Optional[int] = None
_writer_lock = threading.Lock()
dropped = 0


def _drain() -> None:
    while True:
        item = _queue.get()
        try:
            _insert_event(*item)
        except Exception:  # noqa: BLE001
            logger.warning("could not persist ops event", extra={"event": "ops_event_persist_failed"})
        finally:
            _queue.task_done()


def _ensure_writer() -> None:
    """Start the writer thread; restart it in a forked child (RQ's work horse forks per job and threads do not survive fork)."""
    global _writer, _writer_pid
    if _writer is not None and _writer_pid == os.getpid():
        return
    with _writer_lock:
        if _writer is None or _writer_pid != os.getpid():
            _writer = threading.Thread(target=_drain, name="qs-ops-events", daemon=True)
            _writer_pid = os.getpid()
            _writer.start()


def record_event(kind: str, *, severity: str = "info", component: str = "app", detail: Optional[dict] = None,
                 sync: bool = False) -> None:
    """Log + count + persist one operational event. Never raises."""
    global dropped
    try:
        severity = severity if severity in SEVERITIES else "info"
        clean = sanitize_detail(detail)
        level = {"info": logging.INFO, "warning": logging.WARNING, "critical": logging.ERROR}[severity]
        logger.log(level, "ops event: %s", kind, extra={"event": kind, "severity": severity, "component": component, **clean})
        metrics.record_ops_event(kind)
        if sync:
            _insert_event(kind, severity, component, clean)
            return
        _ensure_writer()
        try:
            _queue.put_nowait((kind, severity, component, clean))
        except queue.Full:
            dropped += 1
    except Exception:  # noqa: BLE001
        logger.debug("record_event failed", exc_info=True)


def flush(timeout: float = 5.0) -> None:
    """Wait until queued events are written (tests / shutdown)."""
    done = threading.Event()

    def _wait():
        _queue.join()
        done.set()
    threading.Thread(target=_wait, daemon=True).start()
    done.wait(timeout)


# ---------------------------------------------------------------------------------------------------------------
# Incidents: one open row per key (partial unique index), resolved when the condition clears.
# ---------------------------------------------------------------------------------------------------------------
def open_or_touch_incident(db, key: str, *, alert: str, severity: str, component: str, detail: Optional[dict] = None):
    """Return ``(incident, created)``. Safe under concurrent evaluators: the partial unique index makes the loser's INSERT
    fail, and it then touches the winner's row."""
    from sqlalchemy.exc import IntegrityError

    from db import OpsIncident
    clean = sanitize_detail(detail)
    row = db.query(OpsIncident).filter(OpsIncident.incident_key == key, OpsIncident.status == "open").first()
    if row is not None:
        row.last_seen_at = _now()
        row.occurrences = int(row.occurrences or 0) + 1
        row.severity = severity
        row.detail = clean or row.detail
        db.commit()
        return row, False
    row = OpsIncident(incident_key=key[:96], alert=alert[:64], severity=severity, component=component[:32], status="open",
                      opened_at=_now(), last_seen_at=_now(), occurrences=1, detail=clean or None)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        row = db.query(OpsIncident).filter(OpsIncident.incident_key == key, OpsIncident.status == "open").first()
        if row is not None:
            row.last_seen_at = _now()
            db.commit()
        return row, False
    return row, True


def resolve_stale_incidents(db, active_keys: set, resolve_after_seconds: int) -> list:
    """Resolve open incidents whose key is not active and that were last seen at least ``resolve_after_seconds`` ago."""
    from db import OpsIncident
    cutoff = _now() - timedelta(seconds=resolve_after_seconds)
    resolved = []
    for row in db.query(OpsIncident).filter(OpsIncident.status == "open").all():
        last_seen = row.last_seen_at if row.last_seen_at.tzinfo else row.last_seen_at.replace(tzinfo=timezone.utc)
        if row.incident_key not in active_keys and last_seen <= cutoff:
            row.status = "resolved"
            row.resolved_at = _now()
            resolved.append(row)
    if resolved:
        db.commit()
    return resolved


def incident_to_dict(row) -> dict:
    def iso(v):
        return v.isoformat() if v else None
    return {"id": row.id, "alert": row.alert, "key": row.incident_key, "severity": row.severity, "component": row.component,
            "status": row.status, "opened_at": iso(row.opened_at), "last_seen_at": iso(row.last_seen_at),
            "resolved_at": iso(row.resolved_at), "occurrences": row.occurrences, "detail": row.detail or {}}


def list_incidents(db, status: Optional[str] = None, limit: int = 50) -> list:
    from db import OpsIncident
    q = db.query(OpsIncident)
    if status in ("open", "resolved"):
        q = q.filter(OpsIncident.status == status)
    return [incident_to_dict(r) for r in q.order_by(OpsIncident.opened_at.desc()).limit(max(1, min(limit, 200))).all()]


def list_events(db, limit: int = 100, kind: Optional[str] = None) -> list:
    from db import OpsEvent
    q = db.query(OpsEvent)
    if kind:
        q = q.filter(OpsEvent.kind == kind)
    rows = q.order_by(OpsEvent.created_at.desc()).limit(max(1, min(limit, 500))).all()
    return [{"id": r.id, "at": r.created_at.isoformat() if r.created_at else None, "kind": r.kind, "severity": r.severity,
             "component": r.component, "detail": r.detail or {}} for r in rows]


def prune(db, retention_days: int) -> int:
    """Delete events and resolved incidents older than the retention window. Returns rows deleted."""
    from db import OpsEvent, OpsIncident
    cutoff = _now() - timedelta(days=retention_days)
    n = db.query(OpsEvent).filter(OpsEvent.created_at < cutoff).delete(synchronize_session=False)
    n += db.query(OpsIncident).filter(OpsIncident.status == "resolved", OpsIncident.resolved_at < cutoff).delete(synchronize_session=False)
    db.commit()
    return n
