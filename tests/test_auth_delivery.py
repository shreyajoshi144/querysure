from email_provider import SMTPEmailProvider, get_email_provider
from unittest.mock import patch

from tests.auth_test_utils import drain_queued_auth_email_for_email, latest_token_for_email


def test_development_email_provider_defaults_to_smtp():
    assert isinstance(get_email_provider(), SMTPEmailProvider)


def test_registration_email_reaches_smtp_capture(client):
    email = "smtp-capture@example.com"
    r = client.post("/api/auth/register", json={"email": email, "password": "StrongPassword123!"})
    assert r.status_code == 200
    drain_queued_auth_email_for_email(email)
    token = latest_token_for_email(email, kind="verification")
    assert token


def test_registration_email_delivery_failure_is_persisted_by_worker(client):
    email = "delivery-fail@example.com"
    with patch("email_provider.get_email_provider") as provider:
        provider.return_value.send.side_effect = RuntimeError("smtp unavailable")
        r = client.post("/api/auth/register", json={"email": email, "password": "StrongPassword123!"})
        assert r.status_code == 200
        job_id = drain_queued_auth_email_for_email(email)

    from db import Job, SessionLocal
    with SessionLocal() as db:
        job = db.query(Job).filter(Job.id == job_id).one()
        assert job.status == "failed"
        assert job.error_code == "JOB_FAILED"
        assert job.result_ref is not None
        assert "background operation could not be completed" in job.result_ref.lower()


def test_password_reset_response_does_not_claim_delivery(client):
    email = "reset-message@example.com"
    assert client.post("/api/auth/register", json={"email": email, "password": "StrongPassword123!"}).status_code == 200
    with patch("api.get_email_provider") as provider:
        provider.return_value.send.side_effect = RuntimeError("smtp unavailable")
        r = client.post("/api/auth/forgot-password", json={"email": email})
    assert r.status_code == 200
    message = r.json()["message"].lower()
    assert "sent" not in message
    assert "attempt" in message


def test_resend_response_does_not_claim_delivery(client):
    email = "resend-message@example.com"
    assert client.post("/api/auth/register", json={"email": email, "password": "StrongPassword123!"}).status_code == 200
    with patch("api.get_email_provider") as provider:
        provider.return_value.send.side_effect = RuntimeError("smtp unavailable")
        r = client.post("/api/auth/resend-verification", json={"email": email})
    assert r.status_code == 200
    message = r.json()["message"].lower()
    assert "sent" not in message
    assert "attempt" in message
