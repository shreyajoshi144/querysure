"""Sprint 5: server-side session revocation, email cooldown, account export and deletion."""
import json
import os
import uuid
from datetime import datetime, timedelta, timezone


import config
import storage
from auth import create_access_token, purge_dead_sessions
from db import (AuthSession, Job, PasswordResetToken, Payment, PipelineSession, Report, SessionLocal, Subscription,
                UsageDaily, User)
from tests.auth_test_utils import verify_registered_email

PW = "TestPassword123!"


def new_account(client):
    email = f"s5-{uuid.uuid4().hex[:10]}@example.com"
    assert client.post("/api/auth/register", json={"email": email, "password": PW}).status_code == 200
    verify_registered_email(client, email)
    return email


def login(client, email, password=PW):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def status(client, headers):
    return client.get("/api/auth/me", headers=headers).status_code


def claims(headers):
    from auth import decode_token_claims
    return decode_token_claims(headers["Authorization"].split(" ", 1)[1])


# ------------------------------------------------------------------------------------------------ sessions
def test_login_token_is_bound_to_a_server_session(client):
    h = login(client, new_account(client))
    sid = claims(h)["sid"]
    with SessionLocal() as db:
        row = db.get(AuthSession, sid)
        assert row is not None and row.revoked_at is None


def test_sign_out_revokes_only_this_token(client):
    email = new_account(client)
    a, b = login(client, email), login(client, email)
    assert status(client, a) == status(client, b) == 200
    assert client.post("/api/auth/sign-out", headers=a).status_code == 200
    assert status(client, a) == 401  # real revocation, not "client forgot it"
    assert status(client, b) == 200
    assert client.post("/api/auth/sign-out", headers=a).status_code == 401  # idempotent-safe: already dead


def test_logout_all_revokes_every_token_and_login_still_works(client):
    email = new_account(client)
    a, b = login(client, email), login(client, email)
    assert client.post("/api/auth/logout-all", headers=a).status_code == 200
    assert status(client, a) == status(client, b) == 401
    assert status(client, login(client, email)) == 200


def test_change_password_signs_out_other_devices_but_not_this_one(client):
    email = new_account(client)
    a, other = login(client, email), login(client, email)
    r = client.post("/api/auth/change-password", headers=a, json={"current_password": PW, "new_password": "BrandNewPassw0rd!"})
    assert r.status_code == 200
    fresh = {"Authorization": "Bearer " + r.json()["access_token"]}
    assert status(client, a) == status(client, other) == 401
    assert status(client, fresh) == 200


def test_password_reset_revokes_session_rows(client):
    from tests.auth_test_utils import latest_token_for_email
    email = new_account(client)
    h = login(client, email)
    client.post("/api/auth/forgot-password", json={"email": email})
    token = latest_token_for_email(email, kind="reset")
    assert client.post("/api/auth/reset-password", params={"token": token}, json={"password": "ResetPassw0rd!!"}).status_code == 200
    assert status(client, h) == 401
    with SessionLocal() as db:
        row = db.get(AuthSession, claims_unverified_sid(h))
        assert row.revoked_at is not None and row.revoked_reason == "password_reset"


def claims_unverified_sid(headers):
    import jwt
    return jwt.decode(headers["Authorization"].split(" ", 1)[1], options={"verify_signature": False})["sid"]


def test_expired_and_foreign_sessions_are_rejected(client):
    ea, eb = new_account(client), new_account(client)
    a, b = login(client, ea), login(client, eb)
    with SessionLocal() as db:
        row = db.get(AuthSession, claims_unverified_sid(a))
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert status(client, a) == 401
    # user A's identity with user B's session id must not authenticate
    uid_a = client.get("/api/auth/me", headers=b).json()["id"]
    with SessionLocal() as db:
        ua = db.query(User).filter(User.email == ea).one()
        forged = create_access_token(ua.id, auth_version=int(ua.auth_version or 0), session_id=claims_unverified_sid(b))
    assert uid_a != ua.id
    assert status(client, {"Authorization": "Bearer " + forged}) == 401


def test_tokens_without_session_id_follow_policy(client, monkeypatch):
    email = new_account(client)
    login(client, email)
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).one()
        legacy = {"Authorization": "Bearer " + create_access_token(u.id, auth_version=int(u.auth_version or 0))}
    monkeypatch.setattr(config, "AUTH_REQUIRE_SESSION_ID", False)
    assert status(client, legacy) == 200
    monkeypatch.setattr(config, "AUTH_REQUIRE_SESSION_ID", True)
    assert status(client, legacy) == 401  # production default


def test_active_session_cap_revokes_oldest(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_MAX_ACTIVE_SESSIONS", 2)
    email = new_account(client)
    first, second, third = login(client, email), login(client, email), login(client, email)
    assert status(client, first) == 401
    assert status(client, second) == status(client, third) == 200


def test_purge_dead_sessions(client):
    email = new_account(client)
    h = login(client, email)
    sid = claims_unverified_sid(h)
    with SessionLocal() as db:
        db.get(AuthSession, sid).revoked_at = datetime.now(timezone.utc) - timedelta(days=3)
        db.commit()
        assert purge_dead_sessions(db) >= 1
        assert db.get(AuthSession, sid) is None


def test_email_cooldown_blocks_mail_bombing(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_EMAIL_COOLDOWN_SECONDS", 60)
    email = new_account(client)
    for _ in range(4):
        r = client.post("/api/auth/forgot-password", json={"email": email})
        assert r.status_code == 200 and "If an account exists" in r.json()["message"]  # identical response either way
    with SessionLocal() as db:
        uid = db.query(User).filter(User.email == email).one().id
        assert db.query(PasswordResetToken).filter(PasswordResetToken.user_id == uid).count() == 1


# ------------------------------------------------------------------------------------------------ export
def test_export_contains_own_data_only_and_no_secrets(client):
    email, other = new_account(client), new_account(client)
    h = login(client, email)
    sess = client.post("/api/session", headers=h, json={}).json()["session_id"]
    with SessionLocal() as db:
        row = db.query(PipelineSession).filter(PipelineSession.id == sess).one()
        row.question, row.sql, row.db_path = "How many orders?", "SELECT 1", "/secret/path/internal.db"
        db.add(Report(user_id=row.user_id, session_id=sess, key="datasets/reports/INTERNAL/KEY", kind="pdf", size=3))
        db.commit()
    r = client.get("/api/account/export", headers=h)
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"] and r.headers["cache-control"] == "private, no-store"
    data = r.json()
    blob = json.dumps(data)
    assert data["account"]["email"] == email and other not in blob
    assert data["sessions"][0]["question"] == "How many orders?"
    for forbidden in ("hashed_password", "token_hash", "INTERNAL", "/secret/path", "$2b$", "auth_version"):
        assert forbidden not in blob
    assert client.get("/api/account/export").status_code == 401


# ------------------------------------------------------------------------------------------------ deletion
def delete(client, h, email, **body):
    return client.post("/api/account/delete", headers=h, json={"confirm_email": email, **body})


def test_delete_requires_reauthentication_and_confirmation(client):
    email = new_account(client)
    h = login(client, email)
    assert client.post("/api/account/delete", json={"confirm_email": email, "password": PW}).status_code == 401
    assert delete(client, h, email).status_code == 401                                   # no password
    assert delete(client, h, email, password="WrongPassword123!").status_code == 401
    assert delete(client, h, "someone-else@example.com", password=PW).status_code == 422
    assert status(client, h) == 200  # nothing was deleted


def test_delete_removes_everything_and_deidentifies_payments(client):
    email = new_account(client)
    h = login(client, email)
    sess = client.post("/api/session", headers=h, json={}).json()["session_id"]
    ds = os.path.join(storage.UPLOAD_DIR, f"s5-del-{uuid.uuid4().hex}.db")
    os.makedirs(storage.UPLOAD_DIR, exist_ok=True)
    open(ds, "wb").write(b"data")
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).one()
        uid = u.id
        db.query(PipelineSession).filter(PipelineSession.id == sess).one().db_path = ds
        db.add(UsageDaily(user_id=uid, day="2026-10-01", requests=1))
        db.add(Subscription(user_id=uid, plan="pro", status="canceled", provider="generic"))
        pid = "pay_del_" + uuid.uuid4().hex[:6]
        db.add(Payment(user_id=uid, provider="generic", provider_payment_id=pid, amount_minor=500, currency="USD",
                       status="succeeded", occurred_at=datetime.now(timezone.utc)))
        db.commit()
    r = delete(client, h, email, password=PW)
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert status(client, h) == 401
    assert not os.path.exists(ds)
    with SessionLocal() as db:
        assert db.query(User).filter(User.id == uid).count() == 0
        for model in (PipelineSession, UsageDaily, Subscription, AuthSession):
            assert db.query(model).filter(model.user_id == uid).count() == 0
        p = db.query(Payment).filter(Payment.provider_payment_id == pid).one()
        assert p.user_id is None and p.amount_minor == 500  # accounting row kept, de-identified
    # the e-mail can register again (nothing left behind)
    assert client.post("/api/auth/register", json={"email": email, "password": PW}).status_code == 200


def test_delete_blocked_by_live_subscription_until_cancelled(client, monkeypatch):
    monkeypatch.setattr(config, "PAYMENT_PROVIDER", "generic")
    monkeypatch.setattr(config, "PAYMENT_WEBHOOK_SECRETS", ("whsec_test_0123456789abcdef",))
    email = new_account(client)
    h = login(client, email)
    with SessionLocal() as db:
        uid = db.query(User).filter(User.email == email).one().id
        db.add(Subscription(user_id=uid, plan="pro", status="active", provider="generic", provider_subscription_id="sub_del_" + uuid.uuid4().hex[:6]))
        db.commit()
    r = delete(client, h, email, password=PW)
    assert r.status_code == 409 and r.json()["error_code"] == "SUBSCRIPTION_ACTIVE"
    assert status(client, h) == 200
    with SessionLocal() as db:
        db.query(Subscription).filter(Subscription.user_id == uid).one().status = "canceled"
        db.commit()
    assert delete(client, h, email, password=PW).status_code == 200


def test_delete_cancels_subscription_via_adapter_when_supported(client, monkeypatch):
    import payment_provider as pp
    cancelled = []

    class Cancels(pp.GenericHmacProvider):
        def cancel_subscription(self, *, subscription_id, **kw):
            cancelled.append(subscription_id)

    pp.register_provider("cancels", lambda: Cancels(webhook_secrets=("x" * 20,), name="cancels"))
    monkeypatch.setattr(config, "PAYMENT_PROVIDER", "cancels")
    try:
        email = new_account(client)
        h = login(client, email)
        sid = "sub_c_" + uuid.uuid4().hex[:6]
        with SessionLocal() as db:
            uid = db.query(User).filter(User.email == email).one().id
            db.add(Subscription(user_id=uid, plan="pro", status="active", provider="cancels", provider_subscription_id=sid))
            db.commit()
        assert delete(client, h, email, password=PW).status_code == 200
        assert cancelled == [sid]
    finally:
        pp._FACTORIES.pop("cancels", None)


def test_delete_blocked_while_jobs_running(client):
    email = new_account(client)
    h = login(client, email)
    with SessionLocal() as db:
        uid = db.query(User).filter(User.email == email).one().id
        db.add(Job(user_id=uid, kind="upload", status="running"))
        db.commit()
    r = delete(client, h, email, password=PW)
    assert r.status_code == 409 and r.json()["error_code"] == "JOBS_IN_PROGRESS"


def test_delete_fails_closed_when_object_storage_fails(client, monkeypatch):
    import account_service
    email = new_account(client)
    h = login(client, email)
    sess = client.post("/api/session", headers=h, json={}).json()["session_id"]
    with SessionLocal() as db:
        db.query(PipelineSession).filter(PipelineSession.id == sess).one().db_path = os.path.join(storage.UPLOAD_DIR, "x.db")
        db.commit()
    monkeypatch.setattr(account_service, "_delete_dataset_strict", lambda ref: (_ for _ in ()).throw(RuntimeError("s3 down")))
    r = delete(client, h, email, password=PW)
    assert r.status_code == 503 and r.json()["error_code"] == "DELETE_INCOMPLETE"
    assert status(client, h) == 200  # account intact and retryable
    with SessionLocal() as db:
        assert db.query(PipelineSession).filter(PipelineSession.id == sess).count() == 1


def test_delete_google_only_account_requires_fresh_google_credential(client, monkeypatch):
    import api as api_module
    from auth import _GOOGLE_ONLY_PREFIX
    email = new_account(client)
    h = login(client, email)
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).one()
        u.google_subject, u.hashed_password = "g-" + uuid.uuid4().hex, _GOOGLE_ONLY_PREFIX + "x"
        db.commit()
        subj = u.google_subject
    assert delete(client, h, email, password=PW).status_code == 401  # a password cannot stand in
    monkeypatch.setattr(api_module, "verify_google_id_token", lambda c: {"sub": "someone-else"})
    assert delete(client, h, email, google_credential="cred").status_code == 401
    monkeypatch.setattr(api_module, "verify_google_id_token", lambda c: {"sub": subj})
    assert delete(client, h, email, google_credential="cred").status_code == 200


def test_export_includes_populated_usage_and_ai_invocation_rows(client):
    from db import AIInvocation
    email = new_account(client)
    h = login(client, email)
    with SessionLocal() as db:
        uid = db.query(User).filter(User.email == email).one().id
        db.add(UsageDaily(user_id=uid, day="2026-10-01", requests=3, query_count=2, estimated_cost_cents=7))
        db.add(AIInvocation(user_id=uid, provider="groq", model="m", input_tokens=10, output_tokens=5,
                            estimated_cost_cents=1, success=True))
        db.commit()
    data = client.get("/api/account/export", headers=h).json()
    assert data["usage_daily"] == [{"day": "2026-10-01", "requests": 3, "queries": 2, "estimated_cost_cents": 7}]
    assert data["ai_invocations"][0]["model"] == "m" and data["ai_invocations"][0]["success"] is True
    assert "prompt" not in json.dumps(data["ai_invocations"]).lower()


def test_reapply_deletions_after_restore(client, tmp_path):
    """A restored dump resurrects deleted accounts; the script removes exactly the ones whose logged user_ref is listed."""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import reapply_deletions
    from observability.context import ref
    keep, drop = new_account(client), new_account(client)
    with SessionLocal() as db:
        keep_id = db.query(User).filter(User.email == keep).one().id
        drop_id = db.query(User).filter(User.email == drop).one().id
    refs = tmp_path / "refs.txt"
    refs.write_text(ref(drop_id) + "\n\n")
    assert reapply_deletions.main([str(refs), "--dry-run"]) == 0
    with SessionLocal() as db:
        assert db.query(User).filter(User.id == drop_id).count() == 1  # dry run changes nothing
    assert reapply_deletions.main([str(refs)]) == 0
    with SessionLocal() as db:
        assert db.query(User).filter(User.id == drop_id).count() == 0
        assert db.query(User).filter(User.id == keep_id).count() == 1
