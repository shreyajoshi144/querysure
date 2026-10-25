"""Sign-up never waits for the mail server, e-mail calls are bounded, and only transient failures are retried.

Root cause (Render free plan): verification e-mails were only sent by the background worker (a paid Render service), and
over SMTP, whose ports Render blocks on free instances; each blocked SMTP step waited 15 s and a failed e-mail was retried
three times with sleeps, even for permanent errors. No network is used here: urlopen and smtplib are patched.
"""
from __future__ import annotations

import io
import json
import logging
import smtplib
import socket
import subprocess
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

import email_provider as ep
import jobs


@pytest.fixture()
def api_mail(monkeypatch):
    monkeypatch.setattr(ep, "EMAIL_API_KEY", "test-api-key-not-real")
    monkeypatch.setattr(ep, "EMAIL_FROM", "QuerySure <noreply@example.com>")
    monkeypatch.setattr(ep, "EMAIL_TIMEOUT_SECONDS", 7.0)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ------------------------------------------------------------------------------------------------- HTTPS providers
@pytest.mark.parametrize("provider, url, header", [("brevo", "https://api.brevo.com/v3/smtp/email", "Api-key"),
                                                    ("resend", "https://api.resend.com/emails", "Authorization")])
def test_https_provider_sends_one_bounded_request(api_mail, provider, url, header):
    seen = {}

    def fake_urlopen(req, timeout=None, context=None):
        seen.update(url=req.full_url, timeout=timeout, headers=dict(req.headers), body=json.loads(req.data))
        return _Resp(b'{"id":"x"}')
    with patch.object(ep.urllib.request, "urlopen", side_effect=fake_urlopen) as op:
        ep.HTTPSEmailProvider(provider).send(to="new.user@example.com", subject="Verify", text="link")
    assert op.call_count == 1                                 # no retries inside the provider
    assert seen["url"] == url and seen["url"].startswith("https://")
    assert seen["timeout"] == 7.0                             # bounded by EMAIL_TIMEOUT_SECONDS
    assert header in seen["headers"]
    body = json.dumps(seen["body"])
    assert "new.user@example.com" in body and "noreply@example.com" in body


@pytest.mark.parametrize("code, transient", [(401, False), (400, False), (403, False), (429, True), (500, True), (503, True)])
def test_http_errors_are_classified(api_mail, code, transient):
    err = urllib.error.HTTPError("https://api.brevo.com", code, "x", {}, io.BytesIO(b""))
    with patch.object(ep.urllib.request, "urlopen", side_effect=err):
        with pytest.raises(ep.EmailDeliveryError) as exc:
            ep.HTTPSEmailProvider("brevo").send(to="a@example.com", subject="s", text="t")
    assert exc.value.transient is transient
    assert "test-api-key-not-real" not in str(exc.value)


@pytest.mark.parametrize("error", [socket.timeout("timed out"), urllib.error.URLError("unreachable"), TimeoutError()])
def test_network_failures_are_transient(api_mail, error):
    with patch.object(ep.urllib.request, "urlopen", side_effect=error):
        with pytest.raises(ep.EmailDeliveryError) as exc:
            ep.HTTPSEmailProvider("resend").send(to="a@example.com", subject="s", text="t")
    assert exc.value.transient is True


def test_missing_api_key_is_a_permanent_failure(monkeypatch):
    monkeypatch.setattr(ep, "EMAIL_API_KEY", "")
    with pytest.raises(ep.EmailDeliveryError) as exc:
        ep.HTTPSEmailProvider("brevo").send(to="a@example.com", subject="s", text="t")
    assert exc.value.transient is False


# ------------------------------------------------------------------------------------------------------------ SMTP
@pytest.fixture()
def smtp_config(monkeypatch):
    for name, value in {"SMTP_HOST": "smtp.example.com", "SMTP_USERNAME": "u", "SMTP_PASSWORD": "p",
                        "EMAIL_FROM": "noreply@example.com", "SMTP_PORT": 587, "EMAIL_TIMEOUT_SECONDS": 4.0}.items():
        monkeypatch.setattr(ep, name, value)


def test_smtp_uses_the_bounded_timeout_and_a_blocked_port_is_transient(smtp_config):
    with patch.object(ep.smtplib, "SMTP", side_effect=socket.timeout("timed out")) as smtp:
        with pytest.raises(ep.EmailDeliveryError) as exc:
            ep.SMTPEmailProvider().send(to="a@example.com", subject="s", text="t")
    assert smtp.call_args.kwargs["timeout"] == 4.0
    assert exc.value.transient is True


def test_smtp_bad_credentials_are_permanent(smtp_config):
    server = MagicMock()
    server.__enter__.return_value = server
    server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    with patch.object(ep.smtplib, "SMTP", return_value=server):
        with pytest.raises(ep.EmailDeliveryError) as exc:
            ep.SMTPEmailProvider().send(to="a@example.com", subject="s", text="t")
    assert exc.value.transient is False


# ------------------------------------------------------------------------------------------------------ job retries
def _email_job(db_session_user):
    from db import Job, SessionLocal
    import uuid
    from datetime import datetime, timezone
    with SessionLocal() as db:
        job = Job(id=str(uuid.uuid4()), user_id=db_session_user, kind="auth_email", status="queued",
                  created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
        db.add(job)
        db.commit()
        return job.id


def _user_id(client, email):
    assert client.post("/api/auth/register", json={"email": email, "password": "StrongPassword123!"}).status_code == 200
    from db import SessionLocal, User
    with SessionLocal() as db:
        return db.query(User).filter(User.email == email.lower()).one().id      # addresses are stored lower-case


@pytest.mark.parametrize("transient, calls", [(False, 1), (True, jobs.MAX_ATTEMPTS)])
def test_only_transient_email_failures_are_retried(client, transient, calls):
    job_id = _email_job(_user_id(client, f"retry-{transient}@example.com"))
    provider = MagicMock(name="provider")
    provider.name = "brevo"
    provider.send.side_effect = ep.EmailDeliveryError("x", transient=transient)
    with patch("email_provider.get_email_provider", return_value=provider), patch.object(jobs.time, "sleep") as sleep:
        jobs._execute_job(job_id, {"to": "a@example.com", "subject": "s", "text": "t"})
    assert provider.send.call_count == calls
    assert sleep.call_count == calls - 1
    from db import Job, SessionLocal
    with SessionLocal() as db:
        assert db.query(Job).filter(Job.id == job_id).one().status == "failed"


# ---------------------------------------------------------------------------------------------- non-blocking signup
class _Tasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))


def test_background_dispatch_returns_before_any_email_is_sent(client, monkeypatch):
    import config
    monkeypatch.setattr(config, "AUTH_EMAIL_DELIVERY", "background")
    uid = _user_id(client, "bg-dispatch@example.com")
    provider = MagicMock(name="provider")
    provider.name = "brevo"
    tasks = _Tasks()
    from db import Job, SessionLocal
    with patch("email_provider.get_email_provider", return_value=provider):
        with SessionLocal() as db:
            assert jobs.dispatch_auth_email(db, uid, {"to": "a@example.com", "subject": "s", "text": "t"}, tasks) == "background"
        assert provider.send.call_count == 0                  # the request returns before the mail server is contacted
        fn, args, _ = tasks.tasks[0]
        fn(*args)                                             # what FastAPI runs after the response
    assert provider.send.call_count == 1
    with SessionLocal() as db:
        assert db.query(Job).filter(Job.id == args[0]).one().status == "succeeded"


@pytest.mark.parametrize("alive, expected", [(False, "background"), (True, "queue")])
def test_auto_delivery_uses_the_worker_only_when_one_is_alive(client, monkeypatch, alive, expected):
    import config
    monkeypatch.setattr(config, "AUTH_EMAIL_DELIVERY", "auto")
    monkeypatch.setattr(jobs, "worker_alive", lambda: alive)
    uid = _user_id(client, f"auto-{alive}@example.com")
    with patch.object(jobs, "enqueue_job") as enq:
        from db import SessionLocal
        with SessionLocal() as db:
            assert jobs.dispatch_auth_email(db, uid, {"to": "a@example.com", "subject": "s", "text": "t"}, _Tasks()) == expected
    assert enq.called is (expected == "queue")


def test_signup_logs_timings_but_never_the_address_or_link(client, monkeypatch, caplog):
    import config
    monkeypatch.setattr(config, "AUTH_EMAIL_DELIVERY", "background")
    email = "private-person@example.com"
    with caplog.at_level(logging.INFO):
        r = client.post("/api/auth/register", json={"email": email, "password": "StrongPassword123!"})
    assert r.status_code == 200
    timing = [rec for rec in caplog.records if getattr(rec, "event", None) == "auth_signup_timing"]
    assert timing and timing[0].delivery == "background" and timing[0].total_ms >= timing[0].hash_ms >= 0
    delivered = [rec for rec in caplog.records if getattr(rec, "event", None) == "auth_email_delivery"]
    assert delivered and delivered[0].outcome in {"sent", "transient_failure", "permanent_failure"}
    for rec in caplog.records:
        blob = rec.getMessage() + json.dumps({k: str(v) for k, v in rec.__dict__.items()})
        assert email not in blob and "verify_token" not in blob


def test_an_unknown_delivery_mode_is_refused():
    r = subprocess.run([sys.executable, "-c", "import config"], capture_output=True, text=True,
                       env={"ENVIRONMENT": "development", "AUTH_EMAIL_DELIVERY": "sometimes", "PATH": "/usr/bin:/bin"})
    assert r.returncode != 0 and "AUTH_EMAIL_DELIVERY" in r.stderr
