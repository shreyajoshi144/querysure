from __future__ import annotations

import re
from pathlib import Path
import uuid

from db import User, EmailVerificationToken, PasswordResetToken
from auth import hash_password
from tests.auth_test_utils import latest_token_for_email


def email():
    return f"auth-{uuid.uuid4().hex}@example.com"


def register(client, address=None, password="StrongPassword123!"):
    address = address or email()
    r = client.post("/api/auth/register", json={"email": address, "password": password})
    assert r.status_code == 200, r.text
    return address, password, r.json()


def test_registration_hashes_password_and_does_not_verify_automatically(client):
    address, password, body = register(client)
    assert body["email"] == address
    assert body["email_verified"] is False
    from db import SessionLocal
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == address).one()
        assert user.hashed_password != password
        assert user.hashed_password.startswith("$2")
        assert db.query(EmailVerificationToken).filter(EmailVerificationToken.user_id == user.id).count() == 1


def test_registration_normalizes_email_and_duplicate_is_rejected(client):
    address, _, _ = register(client, "MixedCase@Example.COM")
    r = client.post("/api/auth/register", json={"email": address.upper(), "password": "StrongPassword123!"})
    assert r.status_code == 409


def test_password_policy(client):
    for password in ["short", "alllowercase123!", "NoNumber!!!!!!", "NoSpecial1234", "é" * 80]:
        r = client.post("/api/auth/register", json={"email": email(), "password": password})
        assert r.status_code == 422


def test_login_requires_verified_email_then_succeeds(client):
    address, password, _ = register(client)
    r = client.post("/api/auth/login", json={"email": address, "password": password})
    assert r.status_code == 403
    token = latest_token_for_email(address, kind="verification")
    response = client.post("/api/auth/verify-email", params={"token": token})
    assert response.status_code == 200
    assert response.json() == {"success": True, "email_verified": True}
    from db import SessionLocal
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == address).one()
        assert user.email_verified is True
        verification = db.query(EmailVerificationToken).filter(EmailVerificationToken.user_id == user.id).one()
        assert verification.used_at is not None
    assert client.post("/api/auth/verify-email", params={"token": token}).status_code == 400
    r = client.post("/api/auth/login", json={"email": address, "password": password})
    assert r.status_code == 200
    assert r.json()["token_type"] == "bearer"


def test_verification_token_is_hashed_single_use_and_expiring(client):
    address, _, _ = register(client)
    token = latest_token_for_email(address, kind="verification")
    from db import SessionLocal
    from datetime import datetime, timezone, timedelta
    with SessionLocal() as db:
        row = db.query(EmailVerificationToken).filter(EmailVerificationToken.user_id == db.query(User).filter(User.email == address).one().id).one()
        assert row.token_hash != token
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    expired = client.post("/api/auth/verify-email", params={"token": token})
    assert expired.status_code == 400
    assert expired.json()["error_code"] == "EXPIRED_VERIFICATION_TOKEN"

    address2, _, _ = register(client)
    token2 = latest_token_for_email(address2, kind="verification")
    assert client.post("/api/auth/verify-email", params={"token": token2}).status_code == 200
    assert client.post("/api/auth/verify-email", params={"token": token2}).status_code == 400
    assert client.post("/api/auth/verify-email", params={"token": "not-a-real-token"}).status_code == 400


def test_verification_handles_sqlite_naive_expiry_without_500(client):
    address, _, _ = register(client)
    token = latest_token_for_email(address, kind="verification")
    from db import SessionLocal
    from datetime import datetime, timezone, timedelta
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == address).one()
        row = db.query(EmailVerificationToken).filter(EmailVerificationToken.user_id == user.id).one()
        # SQLite strips tzinfo when materializing DateTime(timezone=True).
        row.expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None)
        db.commit()
    response = client.post("/api/auth/verify-email", params={"token": token})
    assert response.status_code == 200, response.text
    assert response.json()["email_verified"] is True


def test_resend_verification_rotates_old_token(client):
    address, _, _ = register(client)
    old = latest_token_for_email(address, kind="verification")
    r = client.post("/api/auth/resend-verification", json={"email": address})
    assert r.status_code == 200
    new = latest_token_for_email(address, kind="verification")
    assert new != old
    assert client.post("/api/auth/verify-email", params={"token": old}).status_code == 400
    assert client.post("/api/auth/verify-email", params={"token": new}).status_code == 200


def test_me_contains_only_safe_account_fields(client):
    address, password, _ = register(client)
    token = latest_token_for_email(address, kind="verification")
    client.post("/api/auth/verify-email", params={"token": token})
    login = client.post("/api/auth/login", json={"email": address, "password": password})
    me = client.get("/api/auth/me", headers={"Authorization": "Bearer " + login.json()["access_token"]})
    assert me.status_code == 200
    body = me.json()
    assert set(body) == {"id", "email", "email_verified", "created_at", "plan"}
    assert "hashed_password" not in body


def test_forgot_password_is_generic_for_unknown_email(client):
    r = client.post("/api/auth/forgot-password", json={"email": email()})
    assert r.status_code == 200
    assert "If an account exists" in r.json()["message"]


def test_password_reset_changes_password_and_invalidates_old_jwt(client):
    address, old_password, _ = register(client)
    verify = latest_token_for_email(address, kind="verification")
    client.post("/api/auth/verify-email", params={"token": verify})
    login = client.post("/api/auth/login", json={"email": address, "password": old_password})
    old_jwt = login.json()["access_token"]

    client.post("/api/auth/forgot-password", json={"email": address})
    reset = latest_token_for_email(address, kind="reset")
    from db import SessionLocal
    from datetime import datetime, timezone, timedelta
    with SessionLocal() as db:
        row = db.query(PasswordResetToken).filter(PasswordResetToken.user_id == db.query(User).filter(User.email == address).one().id).one()
        assert row.token_hash != reset
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert client.post("/api/auth/reset-password", params={"token": reset}, json={"password": "ExpiredPassword123!"}).status_code == 400

    client.post("/api/auth/forgot-password", json={"email": address})
    reset = latest_token_for_email(address, kind="reset")
    new_password = "NewStrongPassword456!"
    r = client.post("/api/auth/reset-password", params={"token": reset}, json={"password": new_password})
    assert r.status_code == 200
    assert client.post("/api/auth/reset-password", params={"token": reset}, json={"password": old_password}).status_code == 400

    assert client.post("/api/auth/login", json={"email": address, "password": old_password}).status_code == 401
    assert client.post("/api/auth/login", json={"email": address, "password": new_password}).status_code == 200
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer " + old_jwt}).status_code == 401


def test_logout_preserves_stateless_jwt_contract(client):
    address, password, _ = register(client)
    verify = latest_token_for_email(address, kind="verification")
    client.post("/api/auth/verify-email", params={"token": verify})
    login = client.post("/api/auth/login", json={"email": address, "password": password})
    headers = {"Authorization": "Bearer " + login.json()["access_token"]}
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    # Stateless bearer compatibility: client-side logout discards the token.
    assert client.get("/api/auth/me", headers=headers).status_code == 200
