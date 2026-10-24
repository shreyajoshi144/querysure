"""Billing-state boundary.

Subscription is billing state only. Query authorization never reads Subscription.plan; the effective product plan remains
User.plan and is resolved exclusively through entitlements.py. The ONLY code that moves User.plan because of money is
``apply_verified_billing_event`` (and ``reconcile_lapsed_subscriptions``, which can only move it DOWN to free).

Webhook processing guarantees (see docs/BILLING.md):

* **Idempotent.** Every provider event id is recorded once in ``billing_events`` (UNIQUE provider+event id). A duplicate or
  replayed delivery is acknowledged and changes nothing.
* **Order-safe.** Providers deliver at-least-once and out of order. An event older than the newest one already applied to a
  subscription is ignored; on an exact timestamp tie the more restrictive status wins.
* **Never trusts the payload for identity.** An event is tied to a user only via a server-created checkout reference or a
  provider subscription/customer id QuerySure already stored.
* **Fail-safe.** A processing error rolls the whole change back and leaves the ledger row ``failed`` so the provider's retry
  is processed; an event that cannot be tied to anyone *yet* is answered 503 for a while (its linking event may simply not
  have arrived), then ignored.
* **Entitlement follows status, not the plan label.** A cancelled / inactive / lapsed subscription entitles nothing.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import config
from db import BillingEvent, CheckoutSession, Payment, SessionLocal, Subscription, User
from entitlements import DEFAULT_PLAN, PLAN_CONFIG, set_effective_plan
from payment_provider import PaymentProvider, PaymentProviderError, VerifiedPaymentEvent

logger = logging.getLogger("querysure.billing")

SUBSCRIPTION_STATUSES = frozenset({
    "inactive",
    "active",
    "past_due",
    "canceled",
})
# Statuses that can entitle a paid plan (and for how long: see entitled_plan).
ENTITLED_STATUSES = frozenset({"active", "past_due"})
# Plans a customer can buy. Team and Business exist as entitlement tiers only: there is no organization / seat model, so
# neither can be purchased and no provider event can grant them through the webhook path.
PURCHASABLE_PLANS = frozenset({"pro"})
# Tie-break when two events carry the same timestamp: the more restrictive state wins.
_STATUS_RANK = {"inactive": 0, "active": 1, "past_due": 2, "canceled": 3}
_CLAIM_LEASE_SECONDS = 300


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | None) -> datetime | None:
    """Aware-UTC view of a stored datetime (SQLite hands back naive values for DateTime(timezone=True))."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def validate_subscription_status(status: str | None) -> str:
    """Return a canonical billing status or reject untrusted/unknown state."""
    normalized = (status or "inactive").strip().lower()
    if normalized not in SUBSCRIPTION_STATUSES:
        raise ValueError("Unknown subscription status")
    return normalized


def entitled_plan(
    plan: str | None,
    status: str | None,
    current_period_end: datetime | None,
    *,
    now: datetime | None = None,
    grace_hours: int | None = None,
) -> str:
    """The plan a subscription entitles RIGHT NOW (``free`` if none).

    ``active`` and ``past_due`` entitle the plan until ``current_period_end`` plus a grace window (so a failed or late
    renewal does not cut access instantly, but nothing entitles forever). An ``active`` row with no known period end is
    trusted (a verified event said so); a ``past_due`` row with no known period end is not.
    """
    normalized_plan = (plan or DEFAULT_PLAN).lower()
    normalized_status = (status or "inactive").lower()
    if normalized_status not in ENTITLED_STATUSES or normalized_plan not in PLAN_CONFIG:
        return DEFAULT_PLAN
    end = _utc(current_period_end)
    if end is None:
        return normalized_plan if normalized_status == "active" else DEFAULT_PLAN
    grace = timedelta(hours=config.BILLING_GRACE_HOURS if grace_hours is None else grace_hours)
    if (now or _now()) > end + grace:
        return DEFAULT_PLAN
    return normalized_plan


def get_billing_state(db: Session, user_id: str) -> dict:
    """Return billing state only; effective entitlements remain User.plan."""
    subscription = (
        db.query(Subscription)
        .filter(Subscription.user_id == user_id)
        .one_or_none()
    )
    if subscription is None:
        return {"status": "inactive", "plan": "free", "provider": None,
                "provider_customer_id": None, "provider_subscription_id": None,
                "current_period_start": None, "current_period_end": None, "cancel_at_period_end": False}
    return {
        "status": validate_subscription_status(subscription.status),
        "plan": subscription.plan,
        "provider": subscription.provider,
        "provider_customer_id": subscription.provider_customer_id,
        "provider_subscription_id": subscription.provider_subscription_id,
        "current_period_start": subscription.current_period_start,
        "current_period_end": subscription.current_period_end,
        "cancel_at_period_end": bool(subscription.cancel_at_period_end),
    }


def apply_verified_billing_event(
    db: Session,
    *,
    user_id: str,
    plan: str,
    status: str,
    provider: str,
    provider_customer_id: str | None = None,
    provider_subscription_id: str | None = None,
    current_period_start: datetime | None = None,
    current_period_end: datetime | None = None,
    cancel_at_period_end: bool | None = None,
    occurred_at: datetime | None = None,
    now: datetime | None = None,
) -> Subscription:
    """Apply a *verified* billing event and transition the effective plan.

    This is intentionally not exposed as an API endpoint. The webhook path calls it only after the adapter authenticated
    the delivery and ``process_verified_event`` resolved the user from server-held references. Client-supplied plan values
    must never reach this boundary directly. Optional fields that are ``None`` leave the stored value untouched.
    """
    normalized_plan = (plan or DEFAULT_PLAN).lower()
    if normalized_plan not in PLAN_CONFIG:
        raise ValueError("Unknown billing plan")
    normalized_status = validate_subscription_status(status)
    if not provider:
        raise ValueError("Billing provider is required")

    # Locking the user row serializes concurrent events for one account (and creation of its Subscription row).
    user = db.query(User).filter(User.id == user_id).with_for_update().one_or_none()
    if user is None:
        raise ValueError("User not found")

    subscription = (
        db.query(Subscription)
        .filter(Subscription.user_id == user_id)
        .one_or_none()
    )
    if subscription is None:
        subscription = Subscription(user_id=user_id)
        db.add(subscription)

    subscription.plan = normalized_plan
    subscription.status = normalized_status
    subscription.provider = provider
    if provider_customer_id is not None:
        subscription.provider_customer_id = provider_customer_id
    if provider_subscription_id is not None:
        subscription.provider_subscription_id = provider_subscription_id
    if current_period_start is not None:
        subscription.current_period_start = current_period_start
    if current_period_end is not None:
        subscription.current_period_end = current_period_end
    if cancel_at_period_end is not None:
        subscription.cancel_at_period_end = bool(cancel_at_period_end)
    if occurred_at is not None:
        subscription.provider_event_at = occurred_at
    subscription.updated_at = datetime.now(timezone.utc)

    # This is the only controlled bridge from verified billing state to the product entitlement source of truth.
    set_effective_plan(
        user,
        entitled_plan(normalized_plan, normalized_status, subscription.current_period_end, now=now),
    )
    db.flush()
    return subscription


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------
class CheckoutError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def create_checkout(
    db: Session,
    user: User,
    plan: str,
    provider: PaymentProvider,
    *,
    return_url: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Start a checkout for ``plan``. Creates (or reuses) a server-side CheckoutSession whose id is the only reference
    the provider ever sees; the entitlement changes later, from the verified webhook, never from this call."""
    now = now or _now()
    plan = (plan or "").strip().lower()
    if plan not in PURCHASABLE_PLANS:
        raise CheckoutError("PLAN_NOT_PURCHASABLE", "That plan cannot be purchased online.", 422)

    subscription = db.query(Subscription).filter(Subscription.user_id == user.id).one_or_none()
    if (
        subscription is not None
        and subscription.plan == plan
        and entitled_plan(subscription.plan, subscription.status, subscription.current_period_end, now=now) == plan
    ):
        raise CheckoutError("ALREADY_SUBSCRIBED", "You already have an active subscription to this plan.", 409)

    checkout = (
        db.query(CheckoutSession)
        .filter(
            CheckoutSession.user_id == user.id,
            CheckoutSession.plan == plan,
            CheckoutSession.provider == provider.provider_name,
            CheckoutSession.status == "pending",
            CheckoutSession.expires_at > now,
        )
        .order_by(CheckoutSession.created_at.desc())
        .first()
    )
    if checkout is None:
        # Opportunistically expire this user's stale attempts so the table does not grow without bound.
        db.query(CheckoutSession).filter(
            CheckoutSession.user_id == user.id, CheckoutSession.status == "pending", CheckoutSession.expires_at <= now
        ).update({CheckoutSession.status: "expired"}, synchronize_session=False)
        checkout = CheckoutSession(
            user_id=user.id, plan=plan, provider=provider.provider_name,
            expires_at=now + timedelta(minutes=config.BILLING_CHECKOUT_TTL_MINUTES),
        )
        db.add(checkout)
        db.commit()
        db.refresh(checkout)

    try:
        customer_id = subscription.provider_customer_id if subscription is not None else None
        if not customer_id:
            created = provider.create_customer(user_id=user.id, email=user.email)
            customer_id = created.get("customer_id") if isinstance(created, dict) else getattr(created, "customer_id", created)
            customer_id = str(customer_id) if customer_id else None
        result = provider.create_checkout_session(
            customer_id=customer_id, plan=plan, reference=checkout.id,
            return_url=return_url, expires_at=_utc(checkout.expires_at),
        )
    except PaymentProviderError as exc:
        logger.warning("checkout creation failed: %s", type(exc).__name__)
        raise CheckoutError("BILLING_PROVIDER_ERROR", "The payment provider could not start checkout. Try again shortly.", 502) from exc

    if customer_id and (subscription is None or not subscription.provider_customer_id):
        if subscription is None:
            subscription = Subscription(user_id=user.id, plan=DEFAULT_PLAN, status="inactive", provider=provider.provider_name)
            db.add(subscription)
        subscription.provider_customer_id = customer_id
    checkout.provider_session_id = getattr(result, "provider_session_id", None)
    db.commit()
    return {
        "checkout_id": checkout.id,
        "checkout_url": result.url,
        "plan": plan,
        "expires_at": _utc(checkout.expires_at).isoformat(),
    }


# ---------------------------------------------------------------------------
# Webhook event processing
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EventResult:
    """``outcome``: processed | duplicate | ignored | stale | rejected | deferred."""

    outcome: str
    detail: str | None = None
    user_id: str | None = None


def payload_digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _claim_event(db: Session, event: VerifiedPaymentEvent, digest: str, now: datetime) -> BillingEvent | None:
    """Take exclusive processing rights to this event id (committed, so concurrent deliveries see it).

    Returns the ledger row, or None when the event is already handled / being handled (a duplicate). A claim that was never
    finished (worker killed mid-flight) becomes reclaimable after a lease; failed and deferred events are reclaimable at once.
    """
    row = BillingEvent(
        provider=event.provider, event_id=event.event_id, event_type=event.event_type[:96], kind=event.kind,
        status="received", occurred_at=event.occurred_at, received_at=now, payload_sha256=digest,
    )
    db.add(row)
    try:
        db.commit()
        return row
    except IntegrityError:
        db.rollback()
    cutoff = now - timedelta(seconds=_CLAIM_LEASE_SECONDS)
    claimed = db.execute(
        sa_update(BillingEvent)
        .where(
            BillingEvent.provider == event.provider,
            BillingEvent.event_id == event.event_id,
            (BillingEvent.status.in_(("failed", "deferred")))
            | ((BillingEvent.status == "received") & (BillingEvent.received_at < cutoff)),
        )
        .values(status="received", received_at=now, attempts=BillingEvent.attempts + 1)
    )
    db.commit()
    if claimed.rowcount != 1:
        return None
    return db.query(BillingEvent).filter(
        BillingEvent.provider == event.provider, BillingEvent.event_id == event.event_id
    ).one()


def _mark_failed(ledger_id: str) -> None:
    session = SessionLocal()
    try:
        session.query(BillingEvent).filter(BillingEvent.id == ledger_id).update(
            {BillingEvent.status: "failed", BillingEvent.outcome: "processing_error"}, synchronize_session=False
        )
        session.commit()
    except Exception:  # noqa: BLE001 - the original error is what matters; never mask it
        session.rollback()
        logger.exception("could not record billing event failure")
    finally:
        session.close()


def _unresolved(event: VerifiedPaymentEvent, now: datetime) -> EventResult:
    """Cannot tie the event to an account (yet). Ask for a retry while the linking event may still be in flight."""
    if now - event.occurred_at < timedelta(hours=config.BILLING_UNRESOLVED_RETRY_HOURS):
        return EventResult("deferred", "unresolved_reference")
    return EventResult("ignored", "unresolved_reference")


def _resolve(db: Session, event: VerifiedPaymentEvent) -> tuple[str | None, CheckoutSession | None, str | None]:
    """(user_id, checkout, conflict). The user comes only from server-held references."""
    checkout = None
    user_id = None
    if event.checkout_reference:
        checkout = db.get(CheckoutSession, event.checkout_reference)
        if checkout is not None and checkout.provider != event.provider:
            return None, None, "reference_conflict"
        user_id = checkout.user_id if checkout is not None else None
    subscription = None
    if event.provider_subscription_id:
        subscription = db.query(Subscription).filter(
            Subscription.provider_subscription_id == event.provider_subscription_id
        ).one_or_none()
    if subscription is None and event.provider_customer_id:
        subscription = db.query(Subscription).filter(
            Subscription.provider == event.provider,
            Subscription.provider_customer_id == event.provider_customer_id,
        ).first()
    if subscription is not None:
        if user_id is not None and subscription.user_id != user_id:
            return None, None, "reference_conflict"
        user_id = subscription.user_id
    return user_id, checkout, None


def _apply_subscription_event(
    db: Session, event: VerifiedPaymentEvent, user_id: str, checkout: CheckoutSession | None, now: datetime
) -> EventResult:
    plan = event.plan or (checkout.plan if checkout is not None else None)
    if plan not in PURCHASABLE_PLANS:
        return EventResult("rejected", "plan_not_purchasable", user_id)
    try:
        status = validate_subscription_status(event.status)
    except ValueError:
        return EventResult("rejected", "invalid_status", user_id)

    current = db.query(Subscription).filter(Subscription.user_id == user_id).one_or_none()
    if event.provider_subscription_id:
        owner = db.query(Subscription).filter(
            Subscription.provider_subscription_id == event.provider_subscription_id, Subscription.user_id != user_id
        ).first()
        if owner is not None:
            return EventResult("rejected", "subscription_owned_by_another_account", user_id)
    last = _utc(current.provider_event_at) if current is not None else None
    if (
        current is not None and current.provider_subscription_id and event.provider_subscription_id
        and current.provider_subscription_id != event.provider_subscription_id
    ):
        # An event about a different subscription than the one on file. Only a NEWER active subscription replaces it;
        # a late cancellation of the old one must never cancel the new one.
        if not (status == "active" and (last is None or event.occurred_at > last)):
            return EventResult("ignored", "other_subscription", user_id)
    elif last is not None:
        if event.occurred_at < last:
            return EventResult("stale", "older_than_applied_state", user_id)
        if event.occurred_at == last and current is not None and _STATUS_RANK[status] < _STATUS_RANK.get(current.status, 0):
            return EventResult("stale", "older_than_applied_state", user_id)

    apply_verified_billing_event(
        db, user_id=user_id, plan=plan, status=status, provider=event.provider,
        provider_customer_id=event.provider_customer_id, provider_subscription_id=event.provider_subscription_id,
        current_period_start=event.current_period_start, current_period_end=event.current_period_end,
        cancel_at_period_end=event.cancel_at_period_end, occurred_at=event.occurred_at, now=now,
    )
    if checkout is not None and checkout.status == "pending" and status == "active":
        checkout.status = "completed"
        checkout.completed_at = now
    return EventResult("processed", None, user_id)


def _link_checkout(db: Session, event: VerifiedPaymentEvent, user_id: str, checkout: CheckoutSession, now: datetime) -> EventResult:
    """checkout.completed that does not (yet) say the subscription is active: just remember the provider's ids."""
    subscription = db.query(Subscription).filter(Subscription.user_id == user_id).with_for_update().one_or_none()
    if event.provider_subscription_id:
        owner = db.query(Subscription).filter(
            Subscription.provider_subscription_id == event.provider_subscription_id, Subscription.user_id != user_id
        ).first()
        if owner is not None:
            return EventResult("rejected", "subscription_owned_by_another_account", user_id)
    if subscription is None:
        subscription = Subscription(user_id=user_id, plan=DEFAULT_PLAN, status="inactive", provider=event.provider)
        db.add(subscription)
    if event.provider_customer_id and not subscription.provider_customer_id:
        subscription.provider_customer_id = event.provider_customer_id
    if event.provider_subscription_id and not subscription.provider_subscription_id:
        subscription.provider_subscription_id = event.provider_subscription_id
    if checkout.status == "pending":
        checkout.status = "completed"
        checkout.completed_at = now
    return EventResult("processed", "linked_only", user_id)


def _apply_payment_event(db: Session, event: VerifiedPaymentEvent, user_id: str) -> EventResult:
    """Record the payment in the ledger. Payments never change entitlements: the subscription events do."""
    status = event.payment_status
    payment = db.query(Payment).filter(
        Payment.provider == event.provider, Payment.provider_payment_id == event.payment_id
    ).with_for_update().one_or_none()
    if payment is None:
        db.add(Payment(
            user_id=user_id, provider=event.provider, provider_payment_id=event.payment_id,
            provider_subscription_id=event.provider_subscription_id, amount_minor=event.amount_minor,
            currency=event.currency, status=status, occurred_at=event.occurred_at,
        ))
        return EventResult("processed", None, user_id)
    if _utc(payment.occurred_at) > event.occurred_at or (payment.status == "refunded" and status != "refunded"):
        return EventResult("stale", "older_than_applied_state", user_id)
    payment.status = status
    payment.occurred_at = event.occurred_at
    if event.amount_minor is not None:
        payment.amount_minor = event.amount_minor
    if event.currency:
        payment.currency = event.currency
    return EventResult("processed", None, user_id)


def _dispatch(db: Session, event: VerifiedPaymentEvent, now: datetime) -> EventResult:
    if event.kind == "ignored":
        return EventResult("ignored", "unhandled_event_type")
    user_id, checkout, conflict = _resolve(db, event)
    if conflict:
        return EventResult("rejected", conflict)
    if user_id is None:
        return _unresolved(event, now)
    if event.kind == "subscription_updated":
        return _apply_subscription_event(db, event, user_id, checkout, now)
    if event.kind == "checkout_completed":
        if event.status:
            return _apply_subscription_event(db, event, user_id, checkout, now)
        if checkout is None:
            return EventResult("ignored", "no_checkout_reference", user_id)
        return _link_checkout(db, event, user_id, checkout, now)
    if event.payment_status:
        return _apply_payment_event(db, event, user_id)
    return EventResult("ignored", "unhandled_event_type", user_id)


_LEDGER_STATUS = {"processed": "processed", "ignored": "ignored", "stale": "ignored", "rejected": "rejected", "deferred": "deferred"}


def process_verified_event(
    db: Session, event: VerifiedPaymentEvent, *, payload_sha256: str, now: datetime | None = None
) -> EventResult:
    """Idempotently apply one AUTHENTICATED provider event. Raises on an internal error (after marking it ``failed`` so the
    provider's retry is processed); the caller answers 5xx for that and for ``deferred``, 2xx for everything else."""
    now = now or _now()
    ledger = _claim_event(db, event, payload_sha256, now)
    if ledger is None:
        return EventResult("duplicate")
    ledger_id = ledger.id
    try:
        result = _dispatch(db, event, now)
        ledger = db.query(BillingEvent).filter(BillingEvent.id == ledger_id).one()
        ledger.status = _LEDGER_STATUS[result.outcome]
        ledger.outcome = (result.detail or result.outcome)[:64]
        ledger.user_id = result.user_id
        ledger.processed_at = None if result.outcome == "deferred" else now
        db.commit()
        return result
    except Exception:
        db.rollback()
        _mark_failed(ledger_id)
        raise


# ---------------------------------------------------------------------------
# Reconciliation (run periodically: scripts/maintenance.py)
# ---------------------------------------------------------------------------
def reconcile_lapsed_subscriptions(db: Session, *, now: datetime | None = None) -> int:
    """Fail closed: a paid subscription whose period (plus grace) has ended with no renewal event stops entitling.

    Marks the subscription ``canceled`` and returns the user to ``free``. It never grants anything, and a later genuine
    provider event with a newer timestamp can still re-activate the account. Returns the number of accounts downgraded."""
    now = now or _now()
    cutoff = now - timedelta(hours=config.BILLING_GRACE_HOURS)
    rows = (
        db.query(Subscription)
        .filter(
            Subscription.status.in_(tuple(ENTITLED_STATUSES)),
            Subscription.current_period_end.isnot(None),
            Subscription.current_period_end < cutoff,
        )
        .all()
    )
    downgraded = 0
    for subscription in rows:
        user = db.query(User).filter(User.id == subscription.user_id).with_for_update().one_or_none()
        if user is None:
            continue
        subscription.status = "canceled"
        subscription.updated_at = now
        if (user.plan or DEFAULT_PLAN) != DEFAULT_PLAN:
            set_effective_plan(user, DEFAULT_PLAN)
        downgraded += 1
    db.commit()
    return downgraded
