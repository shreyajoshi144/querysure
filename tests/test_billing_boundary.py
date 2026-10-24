from db import SessionLocal, Subscription, User
from billing_service import apply_verified_billing_event, get_billing_state
from entitlements import get_plan
from payment_provider import PaymentProvider


def test_payment_provider_is_abstract_and_has_no_implementation():
    assert PaymentProvider.__abstractmethods__ == {
        "provider_name",
        "create_customer",
        "create_checkout_session",
        "cancel_subscription",
        "get_subscription",
        "verify_event",
    }


def test_payment_provider_operations_have_no_default_implementation():
    class StubProvider(PaymentProvider):
        @property
        def provider_name(self):
            return "stub"

        def create_customer(self, *, user_id, email, **kwargs):
            return super().create_customer(user_id=user_id, email=email, **kwargs)

        def create_checkout_session(self, *, customer_id, plan, **kwargs):
            return super().create_checkout_session(customer_id=customer_id, plan=plan, **kwargs)

        def cancel_subscription(self, *, subscription_id, **kwargs):
            return super().cancel_subscription(subscription_id=subscription_id, **kwargs)

        def get_subscription(self, *, subscription_id, **kwargs):
            return super().get_subscription(subscription_id=subscription_id, **kwargs)

        def verify_event(self, payload, signature):
            return super().verify_event(payload, signature)

    provider = StubProvider()
    for operation, kwargs in (
        (provider.create_customer, {"user_id": "u", "email": "u@example.com"}),
        (provider.create_checkout_session, {"customer_id": "c", "plan": "pro"}),
        (provider.cancel_subscription, {"subscription_id": "s"}),
        (provider.get_subscription, {"subscription_id": "s"}),
        (provider.verify_event, {"payload": b"{}", "signature": None}),
    ):
        try:
            operation(**kwargs)
            assert False, "provider-neutral operation must remain unimplemented"
        except NotImplementedError:
            pass


def test_billing_endpoint_is_read_only_and_scoped(client, auth_headers):
    response = client.get("/api/billing", headers=auth_headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "inactive"
    assert data["plan"] == "free"
    assert "password" not in data
    assert "token" not in data


def _user_id(client, auth_headers):
    response = client.get("/api/auth/me", headers=auth_headers)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_subscription_plan_does_not_change_effective_entitlement(client, auth_headers):
    user_id = _user_id(client, auth_headers)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).one()
        subscription = Subscription(user_id=user.id, plan="pro", status="active", provider="future-provider")
        db.add(subscription)
        db.commit()
        db.refresh(user)
        assert user.plan == "free"
        assert get_plan(user.plan)["daily_query_limit"] == 10
    finally:
        db.close()


def test_only_trusted_billing_service_transitions_effective_plan(client, auth_headers):
    user_id = _user_id(client, auth_headers)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).one()
        apply_verified_billing_event(db, user_id=user.id, plan="pro", status="active", provider="future-provider")
        db.commit()
        db.refresh(user)
        assert user.plan == "pro"
        assert get_plan(user.plan)["daily_query_limit"] == 50
    finally:
        db.close()


def test_invalid_subscription_status_is_rejected_without_mutation(client, auth_headers):
    user_id = _user_id(client, auth_headers)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).one()
        try:
            apply_verified_billing_event(db, user_id=user.id, plan="pro", status="paid_by_client", provider="future-provider")
            assert False, "invalid status must be rejected"
        except ValueError as exc:
            assert "status" in str(exc).lower()
        assert user.plan == "free"
        assert db.query(Subscription).filter(Subscription.user_id == user.id).one_or_none() is None
    finally:
        db.rollback()
        db.close()


def test_billing_read_does_not_call_provider(monkeypatch, client, auth_headers):
    called = False

    class ForbiddenProvider:
        def __init__(self):
            nonlocal called
            called = True

    monkeypatch.setattr("payment_provider.PaymentProvider", ForbiddenProvider)
    response = client.get("/api/billing", headers=auth_headers)
    assert response.status_code == 200
    assert called is False


def test_client_supplied_plan_status_cannot_mutate_billing_or_entitlements(client, auth_headers):
    before = client.get("/api/billing", headers=auth_headers).json()
    response = client.post("/api/billing", headers=auth_headers, json={"plan": "pro", "status": "active"})
    assert response.status_code == 405
    after = client.get("/api/billing", headers=auth_headers).json()
    assert after == before


def test_billing_endpoint_requires_authentication(client):
    response = client.get("/api/billing")
    assert response.status_code == 401
