"""Workspace audit trail: append-only, privacy-safe, never fails the caller.

``record`` writes one ``audit_log`` row in its OWN database session, so it can neither commit nor roll back the
caller's unit of work, and a failure is logged and swallowed (an audit hiccup must not fail a login or an upload).
``details`` keeps only allow-listed keys with scalar values; strings are scrubbed (e-mail addresses, tokens) and capped.
Secrets, SQL, questions, data values and storage references are never accepted, whatever key they arrive under.

Append-only: the ORM refuses to flush an UPDATE or DELETE of an AuditLog instance. Rows disappear only through the
erasure paths in workspaces.py (workspace deletion, account deletion), which use bulk deletes by workspace.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import event
from sqlalchemy.orm import Session

from db import AuditLog, SessionLocal

logger = logging.getLogger("querysure.audit")

ACTOR_TYPES = ("user", "agent", "system")
ALLOWED_DETAIL_KEYS = frozenset({
    "name", "previous_name", "role", "previous_role", "member_id", "format", "tables", "rows", "bytes", "status",
    "row_count", "method", "kind", "reason", "count", "session_id", "dataset_id", "workspace_kind", "rehomed",
    "run_id", "connector", "inserted", "rejected", "error_code", "schema_changes", "version", "mode", "fallback_reason", "updated", "skipped", "interval_minutes",
    "chunks", "documents", "cited", "grounded", "intent", "tool", "steps", "step_id", "facts",
})
_MAX_STR = 200


class AppendOnlyViolation(RuntimeError):
    pass


@event.listens_for(Session, "before_flush")
def _refuse_audit_mutation(session, flush_context, instances):  # noqa: ARG001 - SQLAlchemy signature
    for obj in list(session.dirty) + list(session.deleted):
        if isinstance(obj, AuditLog):
            raise AppendOnlyViolation("audit_log rows are append-only")


def clean_details(details: Optional[dict]) -> Optional[dict]:
    if not details:
        return None
    from observability.redaction import scrub_text
    out: dict[str, Any] = {}
    for key, value in details.items():
        if key not in ALLOWED_DETAIL_KEYS:
            continue
        if value is None or isinstance(value, (bool, int, float)):
            out[key] = value
        elif isinstance(value, str):
            out[key] = scrub_text(value, _MAX_STR)
    return out or None


def _request_id() -> Optional[str]:
    try:
        from observability.context import current
        ctx = current()
        return ctx.request_id if ctx else None
    except Exception:  # noqa: BLE001
        return None


def record(workspace_id: Optional[str], action: str, *, actor_id: Optional[str], actor_type: str = "user",
           target_type: Optional[str] = None, target_id: Optional[str] = None, details: Optional[dict] = None) -> bool:
    """Append one audit entry. Returns False (and logs) instead of raising."""
    if not workspace_id or actor_type not in ACTOR_TYPES:
        return False
    db = None
    try:
        db = SessionLocal()
        db.add(AuditLog(workspace_id=workspace_id, actor_type=actor_type, actor_id=actor_id, action=action[:64],
                        target_type=(target_type or None) and target_type[:32], target_id=(target_id or None) and str(target_id)[:64],
                        details=clean_details(details), request_id=_request_id()))
        db.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - never fail the audited operation
        if db is not None:
            db.rollback()
        logger.warning("audit write failed for %s: %s", action, type(exc).__name__)
        return False
    finally:
        if db is not None:
            db.close()


def record_for_user(user_id: str, action: str, **kwargs) -> bool:
    """Audit an action that belongs to the user rather than a team workspace (sign-in): their personal workspace."""
    try:
        import workspaces
        ws_id = workspaces.personal_workspace_id(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit: personal workspace unavailable for %s: %s", action, type(exc).__name__)
        return False
    return record(ws_id, action, actor_id=user_id, **kwargs)
