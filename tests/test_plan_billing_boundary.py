from datetime import datetime, timezone

from db import SessionLocal, Subscription, User
from entitlements import daily_limit
from billing_service import apply_verified_billing_event


def test_subscription_plan_does_not_authorize_queries_directly(client, auth_headers):
    # The existing authenticated user remains Free regardless of billing-state
    # rows unless the trusted billing boundary explicitly transitions User.plan.
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email.like("test-%@example.com")).order_by(User.created_at.desc()).first()
        assert user is not None
        subscription = Subscription(
            user_id=user.id,
            plan="pro",
            status="active",
            provider="test-provider",
        )
        db.add(subscription)
        db.commit()
        db.refresh(user)
        assert user.plan == "free"
        assert daily_limit(user.plan) == 10
    finally:
        db.close()


def test_verified_billing_service_is_the_controlled_plan_transition(client, auth_headers):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email.like("test-%@example.com")).order_by(User.created_at.desc()).first()
        assert user is not None
        apply_verified_billing_event(
            db,
            user_id=user.id,
            plan="pro",
            status="active",
            provider="test-provider",
            provider_subscription_id="sub-test-1",
            current_period_start=datetime.now(timezone.utc),
        )
        db.commit()
        db.refresh(user)
        subscription = db.query(Subscription).filter(Subscription.user_id == user.id).one()
        assert subscription.plan == "pro"
        assert user.plan == "pro"
        assert daily_limit(user.plan) == 50
    finally:
        db.close()
