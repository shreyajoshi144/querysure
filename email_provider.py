"""Small email-provider abstraction for authentication messages.

Production uses SMTP. Development uses a local file mailbox under
``.dev_mail`` so no real message is sent accidentally. Authentication
modules never log message bodies or tokens.
"""
from __future__ import annotations

import os
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol

from config import (
    APP_BASE_URL,
    EMAIL_FROM,
    EMAIL_PROVIDER,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USERNAME,
    IS_PRODUCTION,
)


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
    def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> None:
        if not all((SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, EMAIL_FROM)):
            raise RuntimeError("SMTP email provider is not fully configured")
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
            server_cm = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15, context=tls)
        else:
            server_cm = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
        with server_cm as server:
            if SMTP_PORT != 465:
                server.starttls(context=tls)
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(message)


class DevFileEmailProvider:
    """Local-only mailbox. Never contacts an external mail server."""

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
    if provider in {"console", "dev", "file"}:
        return DevFileEmailProvider()
    if provider in {"disabled", "none"} and not IS_PRODUCTION:
        return DevFileEmailProvider()
    raise RuntimeError(f"Unsupported EMAIL_PROVIDER: {EMAIL_PROVIDER!r}")


def verification_url(token: str) -> str:
    return f"{APP_BASE_URL.rstrip('/')}/#verify_token={token}"


def password_reset_url(token: str) -> str:
    return f"{APP_BASE_URL.rstrip('/')}/#reset_token={token}"
