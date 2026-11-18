"""Account export and deletion (privacy).

Export returns ONLY the caller's own data and never credentials, token hashes, storage keys or other users' data.

Deletion is fail-closed: it refuses while paid billing is still live (an orphaned subscription would keep charging a deleted
account) or background jobs are running, removes every stored object (datasets, reports) BEFORE the database rows so a
failure leaves a retryable account rather than undeletable orphans, then removes the rows in one transaction. Payment
ledger rows are kept for accounting but de-identified (user reference cleared). Logs only ever carry pseudonymous refs.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from sqlalchemy.orm import Session

import report_storage
import storage
from billing_service import ENTITLED_STATUSES
from db import (
    AICall, AIInvocation, AuthSession, BillingEvent, CheckoutSession, Dataset, EmailVerificationToken, Job, PasswordResetToken,
    Payment, PipelineSession, QueryHistory, Report, Subscription, UsageDaily, User, Workspace, WorkspaceMember,
)
from payment_provider import PaymentProviderError, get_payment_provider

logger = logging.getLogger("querysure.account")


class AccountError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def export_account(db: Session, user: User) -> dict:
    """Machine-readable copy of the caller's own data. Dataset contents and query result rows are not included (they are the
    user's own uploads / derived from them and can be large); questions, SQL, reports metadata, usage and billing are."""
    sub = db.query(Subscription).filter(Subscription.user_id == user.id).one_or_none()
    sessions = db.query(PipelineSession).filter(PipelineSession.user_id == user.id).order_by(PipelineSession.created_at).all()
    return {
        "format_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "account": {
            "id": user.id, "email": user.email, "email_verified": bool(user.email_verified), "plan": user.plan,
            "google_linked": bool(user.google_subject), "created_at": _iso(user.created_at),
        },
        "billing": {
            "subscription": None if sub is None else {
                "plan": sub.plan, "status": sub.status, "provider": sub.provider,
                "current_period_start": _iso(sub.current_period_start), "current_period_end": _iso(sub.current_period_end),
                "cancel_at_period_end": bool(sub.cancel_at_period_end),
            },
            "payments": [
                {"amount_minor": p.amount_minor, "currency": p.currency, "status": p.status, "occurred_at": _iso(p.occurred_at)}
                for p in db.query(Payment).filter(Payment.user_id == user.id).order_by(Payment.occurred_at)
            ],
        },
        "usage_daily": [
            {"day": u.day, "requests": u.requests, "queries": u.query_count, "estimated_cost_cents": u.estimated_cost_cents}
            for u in db.query(UsageDaily).filter(UsageDaily.user_id == user.id).order_by(UsageDaily.day)
        ],
        "sessions": [
            {"id": s.id, "created_at": _iso(s.created_at), "updated_at": _iso(s.updated_at), "question": s.question,
             "sql": s.sql, "dataset_bytes": s.dataset_bytes}
            for s in sessions
        ],
        "reports": [
            {"id": r.id, "kind": r.kind, "size_bytes": r.size, "created_at": _iso(r.created_at),
             "download": f"/api/reports/{r.id}/download"}
            for r in db.query(Report).filter(Report.user_id == user.id).order_by(Report.created_at)
        ],
        "query_history": [
            {"id": h.id, "created_at": _iso(h.created_at), "question": h.question, "sql": h.sql, "status": h.status,
             "row_count": h.row_count}
            for h in db.query(QueryHistory).filter(QueryHistory.user_id == user.id).order_by(QueryHistory.created_at)
        ],
        "ai_invocations": [
            {"created_at": _iso(i.created_at), "provider": i.provider, "model": i.model, "input_tokens": i.input_tokens,
             "output_tokens": i.output_tokens, "estimated_cost_cents": i.estimated_cost_cents, "success": bool(i.success)}
            for i in db.query(AIInvocation).filter(AIInvocation.user_id == user.id).order_by(AIInvocation.created_at)
        ],
        "ai_calls": [
            {"created_at": _iso(c.created_at), "trace_id": c.trace_id, "task_type": c.task_type, "provider": c.provider,
             "model": c.model, "outcome": c.outcome, "error_category": c.error_category, "latency_ms": c.latency_ms,
             "input_tokens": c.input_tokens, "output_tokens": c.output_tokens, "estimated_cost_cents": c.estimated_cost_cents}
            for c in db.query(AICall).filter(AICall.user_id == user.id).order_by(AICall.created_at)
        ],
        "workspaces": [
            {"id": w.id, "name": w.name, "kind": w.kind, "role": m.role, "joined_at": _iso(m.created_at)}
            for w, m in db.query(Workspace, WorkspaceMember).join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .filter(WorkspaceMember.user_id == user.id).order_by(Workspace.created_at)
        ],
        "datasets": [
            {"id": d.id, "workspace_id": d.workspace_id, "name": d.name, "file_format": d.file_format, "bytes": d.bytes,
             "table_count": d.table_count, "row_count": d.row_count, "created_at": _iso(d.created_at)}
            for d in db.query(Dataset).filter(Dataset.created_by == user.id).order_by(Dataset.created_at)
        ],
    }


def _delete_dataset_strict(ref: str) -> None:
    if storage.is_workspace_ref(ref):
        storage.delete_workspace_tree(ref)   # every version and raw file of the dataset
        return
    if not storage.is_managed(ref):
        return
    if storage.is_remote(ref):
        _, key = storage._parse_remote(ref)
        if storage.BACKEND == "s3":
            storage._client().delete_object(Bucket=storage.BUCKET, Key=key)
        cache = storage._cache_path(key)
        if os.path.exists(cache):
            os.remove(cache)
    elif os.path.exists(ref):
        os.remove(ref)


def _delete_report_strict(key: str) -> None:
    if report_storage._is_remote():
        storage._client().delete_object(Bucket=storage.BUCKET, Key=key)
        return
    path = report_storage._local_path(key)
    if os.path.isfile(path):
        os.remove(path)


def _cancel_live_subscription(sub: Subscription | None) -> None:
    if sub is None or sub.status not in ENTITLED_STATUSES:
        return
    blocked = AccountError(
        "SUBSCRIPTION_ACTIVE",
        "Cancel your subscription with the payment provider first, then delete your account.", 409,
    )
    if not sub.provider_subscription_id:
        raise blocked
    try:
        provider = get_payment_provider(sub.provider)
        if provider is None:
            raise blocked
        provider.cancel_subscription(subscription_id=sub.provider_subscription_id)
    except PaymentProviderError as exc:
        raise blocked from exc


def delete_account(db: Session, user: User) -> str:
    """Permanently delete ``user`` and everything they own. Returns the deleted user's id. Raises AccountError."""
    uid = user.id
    if db.query(Job.id).filter(Job.user_id == uid, Job.status.in_(("queued", "running", "retrying"))).first():
        raise AccountError("JOBS_IN_PROGRESS", "Background work is still running. Try again in a few minutes.", 409)
    _cancel_live_subscription(db.query(Subscription).filter(Subscription.user_id == uid).one_or_none())

    sessions = db.query(PipelineSession).filter(PipelineSession.user_id == uid).all()
    session_ids = [s.id for s in sessions]
    dataset_refs = {s.db_path for s in sessions if s.db_path}
    dataset_refs |= {d.storage_ref for d in db.query(Dataset.storage_ref).filter(Dataset.created_by == uid)}
    report_keys = [r.key for r in db.query(Report).filter(Report.user_id == uid)]
    try:
        for ref in dataset_refs:
            _delete_dataset_strict(ref)
        for key in report_keys:
            _delete_report_strict(key)
    except Exception as exc:  # noqa: BLE001 - leave the account intact and retryable rather than orphan objects
        logger.error("account deletion aborted: object storage failure (%s)", type(exc).__name__)
        raise AccountError("DELETE_INCOMPLETE", "We could not remove all stored files. Nothing was deleted; please retry.", 503) from exc

    import workspaces
    workspaces.erase_user(db, uid)  # first: it may create other members' personal workspaces in its own session
    # Ledger rows survive de-identified; everything else goes.
    db.query(Payment).filter(Payment.user_id == uid).update({Payment.user_id: None}, synchronize_session=False)
    db.query(BillingEvent).filter(BillingEvent.user_id == uid).update({BillingEvent.user_id: None}, synchronize_session=False)
    for model in (QueryHistory, Report, Job, AICall, AIInvocation, UsageDaily, PipelineSession, CheckoutSession, Subscription, AuthSession,
                  EmailVerificationToken, PasswordResetToken):
        db.query(model).filter(model.user_id == uid).delete(synchronize_session=False)
    db.query(User).filter(User.id == uid).delete(synchronize_session=False)
    db.commit()

    try:
        from modules import clear_cache_key
        for sid in session_ids:
            clear_cache_key(sid)
    except Exception:  # noqa: BLE001 - in-process cache only
        pass
    try:
        from observability.context import ref as hash_ref
        logger.info("account deleted", extra={"event": "account_deleted", "user_ref": hash_ref(uid)})
    except Exception:  # noqa: BLE001
        logger.info("account deleted", extra={"event": "account_deleted"})
    return uid
