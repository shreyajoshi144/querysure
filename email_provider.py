"""Small email-provider abstraction for authentication messages.

Production uses SMTP, or an HTTPS e-mail API (Brevo, Resend) where outbound SMTP ports are blocked, as on Render's free
instances. Development uses a local file mailbox under ``.dev_mail`` so no real message is sent accidentally.
Authentication modules never log message bodies, recipients or tokens: only the provider, outcome and duration.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import socket
import ssl
import time
import urllib.error
import urllib.request
import uuid
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Protocol

from config import (
    APP_BASE_URL,
    EMAIL_API_KEY,
    EMAIL_FROM,
    EMAIL_PROVIDER,
    EMAIL_TIMEOUT_SECONDS,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USERNAME,
    IS_PRODUCTION,
)

logger = logging.getLogger("querysure.email")


class EmailDeliveryError(RuntimeError):
    """A failed send. ``transient`` failures (timeouts, 429, 5xx, connection errors) may succeed on retry; permanent ones
    (bad credentials, rejected sender, missing settings) never will, so the job runner does not retry them."""

    def __init__(self, message: str, *, transient: bool):
        super().__init__(message)
        self.transient = transient


class EmailProvider(Protocol):
    def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> None: ...


def _tls_context() -> ssl.SSLContext:
    """Verified TLS context. Prefer certifi's CA bundle when installed:
    python.org builds on macOS ship without system CAs, which makes every
    SMTP STARTTLS fail with SSLCertVerificationError. Verification stays on."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class SMTPEmailProvider:
    name = "smtp"

    def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> None:
        if not all((SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, EMAIL_FROM)):
            raise EmailDeliveryError("SMTP email provider is not fully configured", transient=False)
        try:
            self._send(to=to, subject=subject, text=text, html=html)
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
            raise EmailDeliveryError(f"SMTP rejected the message ({type(exc).__name__})", transient=False) from None
        except (smtplib.SMTPException, OSError) as exc:     # timeouts, refused/blocked ports, dropped connections
            raise EmailDeliveryError(f"SMTP delivery failed ({type(exc).__name__})", transient=True) from None

    def _send(self, *, to: str, subject: str, text: str, html: str | None) -> None:
        message = EmailMessage()
        message["From"] = EMAIL_FROM
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        if html:
            message.add_alternative(html, subtype="html")
        # Verify the SMTP server certificate + hostname. smtplib's default
        # (context=None) does NOT verify, which would let a network attacker
        # capture SMTP_PASSWORD and auth links.
        tls = _tls_context()
        if SMTP_PORT == 465:
            server_cm = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=EMAIL_TIMEOUT_SECONDS, context=tls)
        else:
            server_cm = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=EMAIL_TIMEOUT_SECONDS)
        with server_cm as server:
            if SMTP_PORT != 465:
                server.starttls(context=tls)
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(message)


class HTTPSEmailProvider:
    """Transactional e-mail over HTTPS (port 443), one bounded request, no retries here (the job runner decides)."""

    ENDPOINTS = {"brevo": "https://api.brevo.com/v3/smtp/email", "resend": "https://api.resend.com/emails"}

    def __init__(self, provider: str) -> None:
        if provider not in self.ENDPOINTS:
            raise EmailDeliveryError(f"Unsupported e-mail API {provider!r}", transient=False)
        self.name = provider

    def _request(self, *, to: str, subject: str, text: str, html: str | None):
        name, address = parseaddr(EMAIL_FROM)
        if self.name == "brevo":
            body = {"sender": {"email": address, **({"name": name} if name else {})}, "to": [{"email": to}],
                    "subject": subject, "textContent": text, **({"htmlContent": html} if html else {})}
            headers = {"api-key": EMAIL_API_KEY}
        else:
            body = {"from": EMAIL_FROM, "to": [to], "subject": subject, "text": text, **({"html": html} if html else {})}
            headers = {"Authorization": f"Bearer {EMAIL_API_KEY}"}
        headers.update({"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "QuerySure"})
        return urllib.request.Request(self.ENDPOINTS[self.name], data=json.dumps(body).encode("utf-8"),
                                      headers=headers, method="POST")

    def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> None:
        if not (EMAIL_API_KEY and EMAIL_FROM):
            raise EmailDeliveryError(f"{self.name} e-mail API is not fully configured", transient=False)
        req = self._request(to=to, subject=subject, text=text, html=html)
        try:
            with urllib.request.urlopen(req, timeout=EMAIL_TIMEOUT_SECONDS, context=_tls_context()) as resp:
                resp.read(2048)
        except urllib.error.HTTPError as exc:
            # 4xx (bad key, unverified sender, invalid recipient) will fail again; 429 and 5xx may not
            transient = exc.code == 429 or exc.code >= 500
            raise EmailDeliveryError(f"{self.name} API answered HTTP {exc.code}", transient=transient) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            raise EmailDeliveryError(f"{self.name} API unreachable ({type(exc).__name__})", transient=True) from None


class DevFileEmailProvider:
    """Local-only mailbox. Never contacts an external mail server."""

    name = "dev"

    def __init__(self) -> None:
        self.directory = Path(os.environ.get("DEV_EMAIL_DIR", ".dev_mail"))
        self.directory.mkdir(parents=True, exist_ok=True)

    def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> None:
        # This is deliberately a local development artifact, not an application log.
        path = self.directory / f"{uuid.uuid4().hex}.eml"
        body = f"To: {to}\nSubject: {subject}\n\n{text}\n"
        if html:
            body += f"\n--- HTML ---\n{html}\n"
        path.write_text(body, encoding="utf-8")


def get_email_provider() -> EmailProvider:
    provider = EMAIL_PROVIDER.lower()
    if provider == "smtp":
        return SMTPEmailProvider()
    if provider in HTTPSEmailProvider.ENDPOINTS:
        return HTTPSEmailProvider(provider)
    if provider in {"console", "dev", "file"}:
        return DevFileEmailProvider()
    if provider in {"disabled", "none"} and not IS_PRODUCTION:
        return DevFileEmailProvider()
    raise RuntimeError(f"Unsupported EMAIL_PROVIDER: {EMAIL_PROVIDER!r}")


def verification_url(token: str) -> str:
    return f"{APP_BASE_URL.rstrip('/')}/#verify_token={token}"


def password_reset_url(token: str) -> str:
    return f"{APP_BASE_URL.rstrip('/')}/#reset_token={token}"


def send_logged(provider: EmailProvider, *, to: str, subject: str, text: str, html: str | None = None) -> None:
    """Send and log provider, outcome and duration only (never the recipient, subject, link or body)."""
    started = time.monotonic()
    outcome = "failed"
    try:
        provider.send(to=to, subject=subject, text=text, html=html)
        outcome = "sent"
    except EmailDeliveryError as exc:
        outcome = "transient_failure" if exc.transient else "permanent_failure"
        raise
    finally:
        logger.info("auth email delivery", extra={"event": "auth_email_delivery", "provider": getattr(provider, "name", "?"),
                                                  "outcome": outcome,
                                                  "duration_ms": round((time.monotonic() - started) * 1000)})
