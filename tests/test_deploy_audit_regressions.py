"""Regression tests for the Render deployment audit fixes."""
from __future__ import annotations

import ssl
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _services():
    return yaml.safe_load((ROOT / "render.yaml").read_text())["services"]


def test_jwt_secret_is_shared_from_web_service_not_regenerated_per_service():
    web = next(s for s in _services() if s["name"] == "querysure-api")
    web_jwt = next(e for e in web["envVars"] if e["key"] == "JWT_SECRET_KEY")
    assert web_jwt.get("generateValue") is True
    for svc in _services():
        if svc["name"] == "querysure-api" or svc["type"] == "keyvalue":
            continue
        jwt = next(e for e in svc["envVars"] if e["key"] == "JWT_SECRET_KEY")
        assert "generateValue" not in jwt, svc["name"]
        assert jwt["fromService"] == {"type": "web", "name": "querysure-api", "envVarKey": "JWT_SECRET_KEY"}


def test_web_service_has_health_check_and_every_service_pins_python():
    for svc in _services():
        if svc["type"] == "keyvalue":  # data store, not a Python runtime
            continue
        keys = {e["key"]: e.get("value") for e in svc["envVars"]}
        assert keys.get("PYTHON_VERSION"), svc["name"]
    web = next(s for s in _services() if s["name"] == "querysure-api")
    assert web["healthCheckPath"] == "/api/health"


class _FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.context = context
        self.starttls_context = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.starttls_context = context

    def login(self, *a):
        pass

    def send_message(self, msg):
        pass


def test_smtp_starttls_verifies_server_certificate(monkeypatch):
    import email_provider

    _FakeSMTP.instances.clear()
    monkeypatch.setattr(email_provider, "SMTP_PORT", 587)
    monkeypatch.setattr(email_provider.smtplib, "SMTP", _FakeSMTP)
    email_provider.SMTPEmailProvider().send(to="a@example.com", subject="s", text="t")
    ctx = _FakeSMTP.instances[-1].starttls_context
    assert ctx is not None and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_smtp_ssl_port_verifies_server_certificate(monkeypatch):
    import email_provider

    _FakeSMTP.instances.clear()
    monkeypatch.setattr(email_provider, "SMTP_PORT", 465)
    monkeypatch.setattr(email_provider.smtplib, "SMTP_SSL", _FakeSMTP)
    email_provider.SMTPEmailProvider().send(to="a@example.com", subject="s", text="t")
    ctx = _FakeSMTP.instances[-1].context
    assert ctx is not None and ctx.verify_mode == ssl.CERT_REQUIRED


def test_smtp_tls_context_verifies_with_ca_bundle():
    import ssl
    import email_provider

    ctx = email_provider._tls_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert ctx.cert_store_stats()["x509_ca"] > 0
