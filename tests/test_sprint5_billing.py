"""Sprint 5: checkout + webhook flow (signature, idempotency, replay, out-of-order, failure, entitlement)."""
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import config
import payment_provider
from billing_service import entitled_plan, reconcile_lapsed_subscriptions
from db import BillingEvent, CheckoutSession, Payment, SessionLocal, Subscription

WH_SECRET = "whsec_test_0123456789abcdef"
CK_SECRET = "cksec_test_0123456789abcdef"


@pytest.fixture()
def billing(monkeypatch):
    monkeypatch.setattr(config, "PAYMENT_PROVIDER", "generic")
    monkeypatch.setattr(config, "PAYMENT_WEBHOOK_SECRETS", (WH_SECRET,))
    monkeypatch.setattr(config, "PAYMENT_CHECKOUT_URL", "https://pay.example.test/checkout")
    monkeypatch.setattr(config, "PAYMENT_CHECKOUT_SIGNING_SECRET", CK_SECRET)
    monkeypatch.setattr(config, "PAYMENT_PORTAL_URL", "https://pay.example.test/portal")


def sign(body: bytes, secret=WH_SECRET, t=None) -> str:
    t = int(time.time()) if t is None else t
    mac = hmac.new(secret.encode(), str(t).encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def event(type_, data, *, eid=None, created=None):
    return {"id": eid or "evt_" + uuid.uuid4().hex[:12], "type": type_, "created": int(created or time.time()), "data": data}


def post(client, payload, *, secret=WH_SECRET, t=None, headers=None):
    body = json.dumps(payload).encode()
    h = {"X-Signature": sign(body, secret, t), "Content-Type": "application/json"}
    h.update(headers or {})
    return client.post("/api/billing/webhook/generic", content=body, headers=h)


def me(client, auth_headers):
    return client.get("/api/auth/me", headers=auth_headers).json()


def checkout(client, auth_headers, plan="pro"):
    r = client.post("/api/billing/checkout", headers=auth_headers, json={"plan": plan})
    return r


def sub_event(ref, *, sub=None, status="active", plan="pro", created=None, eid=None, end_in_days=30, type_="subscription.updated", cust=None):
    sub = sub or "sub_" + uuid.uuid4().hex[:10]
    cust = cust or "cus_" + uuid.uuid4().hex[:10]
    now = int(time.time())
    return event(type_, {
        "reference": ref, "customer_id": cust, "subscription_id": sub, "plan": plan, "status": status,
        "current_period_start": now, "current_period_end": now + end_in_days * 86400,
    }, created=created, eid=eid)


# ----------------------------------------------------------------------------------------------- checkout
def test_checkout_disabled_without_provider(client, auth_headers):
    r = checkout(client, auth_headers)
    assert r.status_code == 503 and r.json()["error_code"] == "BILLING_NOT_CONFIGURED"
    assert client.get("/api/billing", headers=auth_headers).json()["billing_enabled"] is False


def test_checkout_requires_auth(client, billing):
    assert client.post("/api/billing/checkout", json={"plan": "pro"}).status_code == 401


def test_checkout_creates_signed_link_and_never_changes_plan(client, auth_headers, billing):
    r = checkout(client, auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checkout_url"].startswith("https://pay.example.test/checkout?")
    assert "sig=" in body["checkout_url"] and "reference=" + body["checkout_id"] in body["checkout_url"]
    assert "@" not in body["checkout_url"]  # no e-mail leaves QuerySure in the link
    assert me(client, auth_headers)["plan"] == "free"
    # the checkout link signature really is over the other params (tamper => mismatch)
    from urllib.parse import parse_qsl, urlparse, urlencode
    params = dict(parse_qsl(urlparse(body["checkout_url"]).query))
    sig = params.pop("sig")
    expect = hmac.new(CK_SECRET.encode(), urlencode(sorted(params.items())).encode(), hashlib.sha256).hexdigest()
    assert sig == expect


def test_checkout_reuses_pending_session(client, auth_headers, billing):
    a = checkout(client, auth_headers).json()
    b = checkout(client, auth_headers).json()
    assert a["checkout_id"] == b["checkout_id"]


@pytest.mark.parametrize("plan", ["team", "business", "free", "enterprise", ""])
def test_checkout_rejects_non_purchasable_plans(client, auth_headers, billing, plan):
    r = checkout(client, auth_headers, plan)
    assert r.status_code == 422 and r.json()["error_code"] == "PLAN_NOT_PURCHASABLE"


# ----------------------------------------------------------------------------------------------- signature
def test_webhook_rejects_missing_bad_stale_and_forged_signatures(client, billing):
    body = json.dumps(event("subscription.updated", {})).encode()
    assert client.post("/api/billing/webhook/generic", content=body).status_code == 400
    assert client.post("/api/billing/webhook/generic", content=body, headers={"X-Signature": "garbage"}).status_code == 400
    assert client.post("/api/billing/webhook/generic", content=body, headers={"X-Signature": sign(body, "wrong-secret-0123456789")}).status_code == 400
    # valid MAC but outside the replay window
    assert client.post("/api/billing/webhook/generic", content=body, headers={"X-Signature": sign(body, t=int(time.time()) - 3600)}).status_code == 400
    # MAC computed over a different body (tampered payload)
    other = body.replace(b"{}", b'{"x":1}')
    assert client.post("/api/billing/webhook/generic", content=other, headers={"X-Signature": sign(body)}).status_code == 400


def test_webhook_unknown_provider_disabled_and_oversize(client, billing, monkeypatch):
    body = b"{}"
    assert client.post("/api/billing/webhook/other", content=body, headers={"X-Signature": sign(body)}).status_code == 404
    big = b"x" * (config.BILLING_WEBHOOK_MAX_BYTES + 10)
    assert client.post("/api/billing/webhook/generic", content=big, headers={"X-Signature": sign(big)}).status_code == 413
    monkeypatch.setattr(config, "PAYMENT_PROVIDER", "")
    assert client.post("/api/billing/webhook/generic", content=body, headers={"X-Signature": sign(body)}).status_code == 404


def test_webhook_accepts_rotated_secret(client, auth_headers, billing, monkeypatch):
    monkeypatch.setattr(config, "PAYMENT_WEBHOOK_SECRETS", ("new-secret-0123456789abcd", WH_SECRET))
    ref = checkout(client, auth_headers).json()["checkout_id"]
    assert post(client, sub_event(ref), secret="new-secret-0123456789abcd").json()["outcome"] == "processed"
    assert post(client, sub_event(ref, eid="evt_old_secret"), secret=WH_SECRET).status_code == 200


def test_unauthenticated_payload_cannot_choose_user_or_plan(client, auth_headers, billing):
    # user_id / email smuggled into a signed payload are ignored: identity comes only from our checkout reference.
    victim = me(client, auth_headers)["id"]
    payload = sub_event("not-a-real-reference")
    payload["data"]["user_id"] = victim
    payload["data"]["email"] = "x@example.com"
    r = post(client, payload)
    assert r.status_code == 503 and r.json()["outcome"] == "deferred"
    assert me(client, auth_headers)["plan"] == "free"


# ----------------------------------------------------------------------------------------------- happy path
def test_full_flow_activates_pro_only_after_signed_webhook(client, auth_headers, billing):
    ref = checkout(client, auth_headers).json()["checkout_id"]
    assert me(client, auth_headers)["plan"] == "free"
    r = post(client, sub_event(ref))
    assert r.status_code == 200 and r.json()["outcome"] == "processed"
    assert me(client, auth_headers)["plan"] == "pro"
    b = client.get("/api/billing", headers=auth_headers).json()
    assert b["status"] == "active" and b["plan"] == "pro" and b["billing_enabled"] is True
    assert b["portal_url"] == "https://pay.example.test/portal"
    usage = client.get("/api/account/usage", headers=auth_headers).json()
    assert usage["daily_limit"] == 50
    with SessionLocal() as db:
        assert db.query(CheckoutSession).filter(CheckoutSession.id == ref).one().status == "completed"
    # already subscribed -> no second checkout
    assert checkout(client, auth_headers).json()["error_code"] == "ALREADY_SUBSCRIBED"


# ----------------------------------------------------------------------------------------------- idempotency / replay
def test_duplicate_and_replayed_delivery_is_a_noop(client, auth_headers, billing):
    ref = checkout(client, auth_headers).json()["checkout_id"]
    evt = sub_event(ref, eid="evt_dup")
    assert post(client, evt).json()["outcome"] == "processed"
    for _ in range(3):
        r = post(client, evt)  # fresh timestamp each time = a legitimate provider retry
        assert r.status_code == 200 and r.json()["outcome"] == "duplicate"
    # an attacker replaying the *captured* request is stopped by the replay window
    body = json.dumps(evt).encode()
    old_sig = sign(body, t=int(time.time()) - 1000)
    assert client.post("/api/billing/webhook/generic", content=body, headers={"X-Signature": old_sig}).status_code == 400
    with SessionLocal() as db:
        assert db.query(BillingEvent).filter(BillingEvent.event_id == "evt_dup").count() == 1
    assert me(client, auth_headers)["plan"] == "pro"


def test_duplicate_does_not_resurrect_after_cancel(client, auth_headers, billing):
    ref = checkout(client, auth_headers).json()["checkout_id"]
    t0 = int(time.time())
    sid = "sub_" + uuid.uuid4().hex[:8]
    activate = sub_event(ref, sub=sid, eid="evt_a" + sid, created=t0 - 20)
    cancel = sub_event(ref, sub=sid, eid="evt_c" + sid, status="canceled", created=t0 - 10, type_="subscription.canceled")
    assert post(client, activate).json()["outcome"] == "processed"
    assert post(client, cancel).json()["outcome"] == "processed"
    assert me(client, auth_headers)["plan"] == "free"
    assert post(client, activate).json()["outcome"] == "duplicate"  # provider re-sends the old activation
    assert me(client, auth_headers)["plan"] == "free"


# ----------------------------------------------------------------------------------------------- out of order
def test_out_of_order_older_event_is_stale(client, auth_headers, billing):
    ref = checkout(client, auth_headers).json()["checkout_id"]
    t0 = int(time.time())
    sid = "sub_" + uuid.uuid4().hex[:8]
    newer_cancel = sub_event(ref, sub=sid, eid="evt_n" + sid, status="canceled", created=t0 - 5)
    older_active = sub_event(ref, sub=sid, eid="evt_o" + sid, status="active", created=t0 - 60)
    assert post(client, newer_cancel).json()["outcome"] == "processed"
    r = post(client, older_active)
    assert r.status_code == 200 and r.json()["outcome"] == "stale"
    assert me(client, auth_headers)["plan"] == "free"
    assert client.get("/api/billing", headers=auth_headers).json()["status"] == "canceled"


def test_same_timestamp_tie_prefers_restrictive_status(client, auth_headers, billing):
    ref = checkout(client, auth_headers).json()["checkout_id"]
    t = int(time.time()) - 10
    sid = "sub_" + uuid.uuid4().hex[:8]
    assert post(client, sub_event(ref, sub=sid, eid="e1" + sid, status="canceled", created=t)).json()["outcome"] == "processed"
    assert post(client, sub_event(ref, sub=sid, eid="e2" + sid, status="active", created=t)).json()["outcome"] == "stale"
    assert me(client, auth_headers)["plan"] == "free"


def test_late_cancel_of_old_subscription_cannot_cancel_new_one(client, auth_headers, billing):
    ref = checkout(client, auth_headers).json()["checkout_id"]
    t = int(time.time())
    assert post(client, sub_event(ref, sub="sub_old", eid="a", created=t - 100)).json()["outcome"] == "processed"
    assert post(client, sub_event(ref, sub="sub_new", eid="b", created=t - 50)).json()["outcome"] == "processed"
    r = post(client, sub_event(ref, sub="sub_old", status="canceled", eid="c", created=t - 10))
    assert r.json()["outcome"] == "ignored"
    assert me(client, auth_headers)["plan"] == "pro"
    assert client.get("/api/billing", headers=auth_headers).json()["provider_subscription_id"] == "sub_new"


def test_renewal_before_link_event_is_deferred_then_applied(client, auth_headers, billing):
    ref = checkout(client, auth_headers).json()["checkout_id"]
    t = int(time.time())
    # renewal for a subscription we have not linked yet (its checkout event is still in flight)
    late = "sub_late_" + uuid.uuid4().hex[:8]
    renewal = sub_event("", sub=late, eid="evt_renew", created=t - 5)
    renewal["data"].pop("reference")
    renewal["data"].pop("customer_id")
    r = post(client, renewal)
    assert r.status_code == 503 and r.headers.get("Retry-After")
    with SessionLocal() as db:
        assert db.query(BillingEvent).filter(BillingEvent.event_id == "evt_renew").one().status == "deferred"
    # the linking event arrives (also out of order relative to the renewal)
    link = event("checkout.completed", {"reference": ref, "customer_id": "cus_" + late, "subscription_id": late}, created=t - 30)
    assert post(client, link).status_code == 200
    assert me(client, auth_headers)["plan"] == "free"  # linking alone never grants access
    assert post(client, renewal).json()["outcome"] == "processed"  # provider retry of the deferred event
    assert me(client, auth_headers)["plan"] == "pro"
    with SessionLocal() as db:
        row = db.query(BillingEvent).filter(BillingEvent.event_id == "evt_renew").one()
        assert row.status == "processed" and row.attempts == 2


def test_unresolvable_old_event_is_acknowledged_not_retried_forever(client, auth_headers, billing):
    old = sub_event("nope", eid="evt_ancient", created=int(time.time()) - 3 * 86400)
    r = post(client, old)
    assert r.status_code == 200 and r.json()["outcome"] == "ignored"


# ----------------------------------------------------------------------------------------------- failure + retry
def test_processing_failure_rolls_back_and_retry_succeeds(client, auth_headers, billing, monkeypatch):
    ref = checkout(client, auth_headers).json()["checkout_id"]
    evt = sub_event(ref, eid="evt_fail")
    import billing_service
    real = billing_service.apply_verified_billing_event

    def boom(*a, **k):
        real(*a, **k)  # mutate first, then fail: proves the partial change is rolled back
        raise RuntimeError("database exploded")

    monkeypatch.setattr(billing_service, "apply_verified_billing_event", boom)
    r = post(client, evt)
    assert r.status_code == 500
    assert me(client, auth_headers)["plan"] == "free"
    with SessionLocal() as db:
        assert db.query(BillingEvent).filter(BillingEvent.event_id == "evt_fail").one().status == "failed"
        assert db.query(Subscription).filter(Subscription.provider_subscription_id == evt["data"]["subscription_id"]).count() == 0
    monkeypatch.setattr(billing_service, "apply_verified_billing_event", real)
    r = post(client, evt)
    assert r.status_code == 200 and r.json()["outcome"] == "processed"
    assert me(client, auth_headers)["plan"] == "pro"


def test_abandoned_claim_is_reclaimed_after_lease(client, auth_headers, billing):
    ref = checkout(client, auth_headers).json()["checkout_id"]
    evt = sub_event(ref, eid="evt_lease")
    with SessionLocal() as db:  # a worker claimed it and died: row stuck in "received"
        db.add(BillingEvent(provider="generic", event_id="evt_lease", event_type="subscription.updated", kind="subscription_updated",
                            status="received", received_at=datetime.now(timezone.utc), payload_sha256="x"))
        db.commit()
    assert post(client, evt).json()["outcome"] == "duplicate"  # still within the lease: another worker may be on it
    with SessionLocal() as db:
        row = db.query(BillingEvent).filter(BillingEvent.event_id == "evt_lease").one()
        row.received_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        db.commit()
    assert post(client, evt).json()["outcome"] == "processed"


def test_invalid_event_is_400(client, billing):
    assert post(client, {"id": "e", "type": "subscription.updated", "created": "yesterday", "data": {}}).status_code == 400
    assert post(client, event("subscription.updated", {"plan": "pro"})).status_code == 400  # missing status
    assert post(client, event("subscription.updated", {"plan": "gold", "status": "active"})).status_code == 400


def test_unknown_event_type_is_acknowledged(client, billing):
    r = post(client, event("customer.address.changed", {}))
    assert r.status_code == 200 and r.json()["outcome"] == "ignored"


# ----------------------------------------------------------------------------------------------- authorization of plans / accounts
def test_team_and_business_cannot_be_granted_by_webhook(client, auth_headers, billing, monkeypatch):
    ref = checkout(client, auth_headers).json()["checkout_id"]
    for plan in ("team", "business"):
        r = post(client, sub_event(ref, plan=plan, eid="evt_" + plan))
        assert r.status_code == 200 and r.json()["outcome"] == "rejected"
    assert me(client, auth_headers)["plan"] == "free"


def test_subscription_id_cannot_be_claimed_by_second_account(client, auth_headers, billing):
    from tests.auth_test_utils import verify_registered_email
    ref_a = checkout(client, auth_headers).json()["checkout_id"]
    assert post(client, sub_event(ref_a, sub="sub_shared", eid="a1")).json()["outcome"] == "processed"
    email = f"b-{uuid.uuid4().hex[:8]}@example.com"
    assert client.post("/api/auth/register", json={"email": email, "password": "TestPassword123!"}).status_code == 200
    verify_registered_email(client, email)
    tok = client.post("/api/auth/login", json={"email": email, "password": "TestPassword123!"}).json()["access_token"]
    hb = {"Authorization": "Bearer " + tok}
    ref_b = checkout(client, hb).json()["checkout_id"]
    r = post(client, sub_event(ref_b, sub="sub_shared", eid="b1", cust="cus_b"))
    assert r.json()["outcome"] == "rejected"
    assert me(client, hb)["plan"] == "free"


def test_checkout_reference_of_another_user_with_mismatched_subscription_is_rejected(client, auth_headers, billing):
    ref = checkout(client, auth_headers).json()["checkout_id"]
    assert post(client, sub_event(ref, sub="sub_mine", eid="m1", cust="cus_mine")).json()["outcome"] == "processed"
    # a reference belonging to one account combined with a subscription id on another is a conflict, not a merge
    from tests.auth_test_utils import verify_registered_email
    email = f"c-{uuid.uuid4().hex[:8]}@example.com"
    client.post("/api/auth/register", json={"email": email, "password": "TestPassword123!"})
    verify_registered_email(client, email)
    hb = {"Authorization": "Bearer " + client.post("/api/auth/login", json={"email": email, "password": "TestPassword123!"}).json()["access_token"]}
    ref_c = checkout(client, hb).json()["checkout_id"]
    r = post(client, sub_event(ref_c, sub="sub_mine", eid="m2", cust="cus_mine"))
    assert r.json()["outcome"] == "rejected"
    assert me(client, hb)["plan"] == "free"


# ----------------------------------------------------------------------------------------------- entitlement by status
@pytest.mark.parametrize("status", ["canceled", "inactive"])
def test_non_entitling_status_never_grants_plan(client, auth_headers, billing, status):
    ref = checkout(client, auth_headers).json()["checkout_id"]
    assert post(client, sub_event(ref, status=status)).json()["outcome"] == "processed"
    assert me(client, auth_headers)["plan"] == "free"


def test_past_due_keeps_access_within_grace_then_lapses(client, auth_headers, billing):
    ref = checkout(client, auth_headers).json()["checkout_id"]
    # period ended 1h ago, grace is 72h -> still entitled
    pd = sub_event(ref, status="past_due", end_in_days=-1 / 24)
    assert post(client, pd).json()["outcome"] == "processed"
    assert me(client, auth_headers)["plan"] == "pro"
    with SessionLocal() as db:
        s = db.query(Subscription).filter(Subscription.provider_subscription_id == pd["data"]["subscription_id"]).one()
        s.current_period_end = datetime.now(timezone.utc) - timedelta(hours=config.BILLING_GRACE_HOURS + 1)
        db.commit()
        assert reconcile_lapsed_subscriptions(db) == 1
    assert me(client, auth_headers)["plan"] == "free"
    assert client.get("/api/billing", headers=auth_headers).json()["status"] == "canceled"
    with SessionLocal() as db:
        assert reconcile_lapsed_subscriptions(db) == 0  # idempotent


def test_entitled_plan_unit():
    now = datetime.now(timezone.utc)
    assert entitled_plan("pro", "active", None, now=now) == "pro"
    assert entitled_plan("pro", "past_due", None, now=now) == "free"
    assert entitled_plan("pro", "active", now + timedelta(days=1), now=now) == "pro"
    assert entitled_plan("pro", "active", now - timedelta(hours=100), now=now, grace_hours=72) == "free"
    assert entitled_plan("pro", "canceled", now + timedelta(days=9), now=now) == "free"
    assert entitled_plan("nonsense", "active", None, now=now) == "free"


# ----------------------------------------------------------------------------------------------- payments ledger
def test_payments_ledger_ordering_and_privacy(client, auth_headers, billing):
    ref = checkout(client, auth_headers).json()["checkout_id"]
    t = int(time.time())
    se = sub_event(ref, eid="s" + uuid.uuid4().hex[:6], created=t - 100)
    assert post(client, se).json()["outcome"] == "processed"
    sid = se["data"]["subscription_id"]
    tag = uuid.uuid4().hex[:6]

    def pay(kind, eid, created, pid="pay_1"):
        pid = pid + tag
        eid = eid + tag
        return post(client, event("payment." + kind, {"payment_id": pid, "subscription_id": sid, "amount_minor": 500, "currency": "usd"},
                                  eid=eid, created=created))
    assert pay("succeeded", "p1", t - 50).json()["outcome"] == "processed"
    assert pay("refunded", "p2", t - 20).json()["outcome"] == "processed"
    assert pay("succeeded", "p3", t - 10).json()["outcome"] == "stale"      # refunded is sticky
    assert pay("failed", "p4", t - 90, pid="pay_2").json()["outcome"] == "processed"
    assert pay("failed", "p5", t - 5, pid="pay_9").status_code == 200
    with SessionLocal() as db:
        p1 = db.query(Payment).filter(Payment.provider_payment_id == "pay_1" + tag).one()
        assert p1.status == "refunded" and p1.amount_minor == 500 and p1.currency == "USD"
        assert db.query(Payment).filter(Payment.provider_payment_id == "pay_2" + tag).one().status == "failed"
    assert me(client, auth_headers)["plan"] == "pro"  # a failed payment alone does not flip the plan; subscription events do


def test_payment_for_unknown_subscription_is_deferred(client, billing):
    r = post(client, event("payment.succeeded", {"payment_id": "pay_x", "subscription_id": "sub_unknown", "amount_minor": 1, "currency": "USD"}))
    assert r.status_code == 503


def test_ledger_stores_no_raw_payload(client, auth_headers, billing):
    ref = checkout(client, auth_headers).json()["checkout_id"]
    payload = sub_event(ref, eid="evt_priv")
    payload["data"]["email"] = "secret-person@example.com"
    post(client, payload)
    with SessionLocal() as db:
        row = db.query(BillingEvent).filter(BillingEvent.event_id == "evt_priv").one()
        blob = json.dumps({c.name: str(getattr(row, c.name)) for c in BillingEvent.__table__.columns})
        assert "secret-person" not in blob and len(row.payload_sha256) == 64


# ----------------------------------------------------------------------------------------------- legacy surface unchanged
def test_billing_get_still_read_only_and_post_405(client, auth_headers, billing):
    assert client.post("/api/billing", headers=auth_headers, json={"plan": "pro", "status": "active"}).status_code == 405


def test_provider_abstraction_is_vendor_neutral():
    assert payment_provider.PaymentProvider.__abstractmethods__ == {
        "provider_name", "create_customer", "create_checkout_session", "cancel_subscription", "get_subscription", "verify_event"}
    class Other(payment_provider.PaymentProvider):
        provider_name = "other"
        def create_customer(self, **k): return {"customer_id": "c"}
        def create_checkout_session(self, **k): return payment_provider.CheckoutResult(url="https://x.test")
        def cancel_subscription(self, **k): return None
        def get_subscription(self, **k): return None
        def verify_event(self, payload, signature): raise payment_provider.WebhookVerificationError("no")
    payment_provider.register_provider("other", Other)
    try:
        assert isinstance(payment_provider.get_payment_provider("other"), Other)
    finally:
        payment_provider._FACTORIES.pop("other", None)
    with pytest.raises(payment_provider.ProviderNotConfigured):
        payment_provider.get_payment_provider("nonexistent")
