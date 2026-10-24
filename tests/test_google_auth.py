from __future__ import annotations

from unittest.mock import patch

from db import SessionLocal, User


def _fake_claims():
    return {
        "iss": "https://accounts.google.com",
        "sub": "google-subject-123",
        "email": "google-user@example.com",
        "email_verified": True,
    }


def test_google_config_is_public_and_does_not_expose_secret(client):
    with patch("api.GOOGLE_CLIENT_ID", "test-client-id"):
        r = client.get("/api/auth/google/config")
    assert r.status_code == 200
    assert r.json() == {"enabled": True, "client_id": "test-client-id"}
    assert "secret" not in r.text.lower()


def test_google_signin_verifies_id_token_and_creates_user(client):
    with patch("api.GOOGLE_CLIENT_ID", "test-client-id"),          patch("api.verify_google_id_token", return_value=_fake_claims()):
        r = client.post("/api/auth/google", json={"credential": "real-looking-token"})
    assert r.status_code == 200, r.text
    assert r.json()["token_type"] == "bearer"
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == "google-user@example.com").one()
        assert user.google_subject == "google-subject-123"
        assert user.email_verified is True


def test_google_existing_password_account_is_not_silently_linked(client):
    email = "existing@example.com"
    r = client.post("/api/auth/register", json={"email": email, "password": "StrongPassword123!"})
    assert r.status_code == 200
    with patch("api.GOOGLE_CLIENT_ID", "test-client-id"),          patch("api.verify_google_id_token", return_value={**_fake_claims(), "email": email, "sub": "existing-account-subject"}):
        r = client.post("/api/auth/google", json={"credential": "token"})
    assert r.status_code == 409
    assert r.json()["error_code"] == "GOOGLE_ACCOUNT_LINK_REQUIRED"


def test_google_invalid_token_is_rejected(client):
    with patch("api.GOOGLE_CLIENT_ID", "test-client-id"),          patch("api.verify_google_id_token", side_effect=ValueError("bad token")):
        r = client.post("/api/auth/google", json={"credential": "bad"})
    assert r.status_code == 401
    assert r.json()["error_code"] == "INVALID_GOOGLE_TOKEN"


def test_google_link_requires_authenticated_matching_account(client, auth_headers):
    # auth_headers belongs to test@example account; a mismatched Google email must not link.
    with patch("api.GOOGLE_CLIENT_ID", "test-client-id"),          patch("api.verify_google_id_token", return_value=_fake_claims()):
        r = client.post("/api/auth/google/link", headers=auth_headers, json={"credential": "token"})
    assert r.status_code == 403
    assert r.json()["error_code"] == "GOOGLE_EMAIL_MISMATCH"


def test_google_link_succeeds_for_authenticated_matching_email(client):
    email = "linkme@example.com"
    password = "StrongPassword123!"
    assert client.post("/api/auth/register", json={"email": email, "password": password}).status_code == 200
    from tests.auth_test_utils import latest_token_for_email
    token = latest_token_for_email(email, kind="verification")
    assert client.post("/api/auth/verify-email", params={"token": token}).status_code == 200
    login = client.post("/api/auth/login", json={"email": email, "password": password})
    headers = {"Authorization": "Bearer " + login.json()["access_token"]}
    claims = {**_fake_claims(), "email": email, "sub": "link-subject"}
    with patch("api.GOOGLE_CLIENT_ID", "test-client-id"),          patch("api.verify_google_id_token", return_value=claims):
        r = client.post("/api/auth/google/link", headers=headers, json={"credential": "token"})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).one()
        assert user.google_subject == "link-subject"


def test_google_id_token_verification_checks_signature_audience_and_issuer():
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import jwt
    import auth

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Google test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(minutes=10))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    claims = {
        "iss": "https://accounts.google.com",
        "aud": "test-client-id",
        "sub": "signed-google-subject",
        "email": "signed@example.com",
        "email_verified": True,
        "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
    }
    token = jwt.encode(claims, key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ), algorithm="RS256", headers={"kid": "test-kid"})

    with patch("auth.GOOGLE_CLIENT_ID", "test-client-id"),          patch("auth._google_certs_snapshot", return_value={"test-kid": cert_pem}):
        verified = auth.verify_google_id_token(token)
    assert verified["sub"] == "signed-google-subject"
    assert verified["email"] == "signed@example.com"

    bad = jwt.encode({**claims, "aud": "wrong-client-id"}, key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ), algorithm="RS256", headers={"kid": "test-kid"})
    with patch("auth.GOOGLE_CLIENT_ID", "test-client-id"),          patch("auth._google_certs_snapshot", return_value={"test-kid": cert_pem}):
        try:
            auth.verify_google_id_token(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("Wrong Google OAuth audience was accepted")
