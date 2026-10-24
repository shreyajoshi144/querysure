"""
Shared pytest fixtures.

Env vars are set at MODULE import time (not inside a fixture) because
config.py reads os.environ and enforces its production guards the moment
it is first imported -- by the time a fixture function body runs, api.py
(which imports config, db, auth at module level) may already have been
imported by pytest's collection phase. Setting them here, before any
`import api`/`import config` anywhere in the test session, guarantees the
dev-mode fallbacks (SQLite DB, no Redis requirement, no CORS guard) are in
effect for every test.

This intentionally exercises the app the same way local `python start.py`
does: ENVIRONMENT=development, SQLite, in-memory rate limiting. It does
NOT exercise the production guards (Postgres-required, Redis-required,
explicit CORS) -- those are config.py startup checks, not endpoint
behavior, and are covered by inspection in the deployment report instead
of by these tests.
"""
from __future__ import annotations

from tests.auth_test_utils import verify_registered_email

import os
import socketserver
import smtplib
import sys
import tempfile
import threading
import time

# The sample database is a build artifact (git-ignored, created by start.py / the Dockerfile / render.yaml). A fresh
# clone has no retail.db, and several API tests connect the sample, so build it once before any test imports api.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.exists(os.path.join(_REPO_ROOT, "retail.db")):
    import subprocess
    subprocess.run([sys.executable, "create_retail.py"], cwd=_REPO_ROOT, check=True, capture_output=True)


class _SMTPMessageStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._messages = []
        self._next_sequence = 0

    def clear(self):
        with self._lock:
            self._messages.clear()
            self._next_sequence = 0

    def add(self, sender, recipients, data):
        with self._lock:
            self._next_sequence += 1
            self._messages.append({
                "sequence": self._next_sequence,
                "captured_at": time.monotonic_ns(),
                "sender": sender,
                "recipients": tuple(recipients),
                "data": data,
            })

    def messages(self):
        with self._lock:
            return list(self._messages)


SMTP_CAPTURE = _SMTPMessageStore()


class _SMTPTestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 QuerySure test SMTP\r\n")
        sender = None
        recipients = []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            command, _, argument = line.partition(" ")
            command = command.upper()
            if command in {"EHLO", "HELO"}:
                self.wfile.write(b"250-localhost\r\n250-STARTTLS\r\n250-AUTH PLAIN LOGIN\r\n250 SIZE 10485760\r\n")
            elif command == "STARTTLS":
                # The test suite patches smtplib.SMTP.starttls so this is a
                # protocol-level acknowledgement only; no real TLS is needed
                # for the isolated local capture socket.
                self.wfile.write(b"220 2.0.0 Ready to start TLS\r\n")
            elif command == "AUTH":
                self.wfile.write(b"235 2.7.0 Authentication successful\r\n")
            elif command == "MAIL":
                sender = argument[5:].strip("<>") if argument.upper().startswith("FROM:") else argument
                recipients = []
                self.wfile.write(b"250 2.1.0 OK\r\n")
            elif command == "RCPT":
                recipient = argument[3:].strip("<>") if argument.upper().startswith("TO:") else argument
                recipients.append(recipient)
                self.wfile.write(b"250 2.1.5 OK\r\n")
            elif command == "DATA":
                self.wfile.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                chunks = []
                while True:
                    data_line = self.rfile.readline()
                    if data_line == b".\r\n":
                        break
                    if data_line.startswith(b".."):
                        data_line = data_line[1:]
                    chunks.append(data_line)
                SMTP_CAPTURE.add(sender, recipients, b"".join(chunks).decode("utf-8", errors="replace"))
                self.wfile.write(b"250 2.0.0 accepted\r\n")
            elif command == "RSET":
                sender = None
                recipients = []
                self.wfile.write(b"250 2.0.0 OK\r\n")
            elif command == "NOOP":
                self.wfile.write(b"250 2.0.0 OK\r\n")
            elif command == "QUIT":
                self.wfile.write(b"221 2.0.0 Bye\r\n")
                return
            else:
                self.wfile.write(b"250 2.0.0 OK\r\n")


class _ThreadedSMTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


smtp_server = _ThreadedSMTPServer(("127.0.0.1", 0), _SMTPTestHandler)
smtp_thread = threading.Thread(target=smtp_server.serve_forever, name="querysure-test-smtp", daemon=True)
smtp_thread.start()

# Keep the real SMTPEmailProvider and its socket/send_message path under test,
# but prevent the local capture server from needing a TLS certificate. The
# production provider still performs STARTTLS exactly as before.

def _test_starttls(self, *, context=None):
    self.ehlo_or_helo_if_needed()
    return self.docmd("STARTTLS")

smtplib.SMTP.starttls = _test_starttls

TEST_DIR = tempfile.mkdtemp(prefix="querysure_test_")
os.environ["ENVIRONMENT"] = "development"
os.environ["EMAIL_PROVIDER"] = "smtp"
os.environ["SMTP_HOST"] = "127.0.0.1"
os.environ["SMTP_PORT"] = str(smtp_server.server_address[1])
os.environ["SMTP_USERNAME"] = "test-user"
os.environ["SMTP_PASSWORD"] = "test-password"
os.environ["EMAIL_FROM"] = "QuerySure <no-reply@example.test>"
os.environ["APP_BASE_URL"] = "http://localhost:5000"
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", f"sqlite:///{TEST_DIR}/test.db")
os.environ["JWT_SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["CORS_ALLOWED_ORIGINS"] = "*"
os.environ.setdefault("GROQ_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import api as api_module  # noqa: E402

# The register/login routes are rate-limited (3/min, 5/min) keyed by client
# IP. TestClient requests all share one synthetic client IP, so without
# this, a handful of tests each registering their own user would trip the
# real production rate limit and fail with 429s that have nothing to do
# with what's being tested. Disabling the limiter is slowapi's documented
# way to do this in tests; it does not change the route logic itself.
api_module.limiter.enabled = False


@pytest.fixture(autouse=True)
def clear_smtp_capture():
    SMTP_CAPTURE.clear()
    yield
    SMTP_CAPTURE.clear()


class CompatClient:
    """Keep legacy tests readable while exercising the new 202 job API.

    Product behavior remains 202; tests that historically expected the final
    synchronous payload transparently resolve an inline/dev job. New tests can
    call raw_post() to assert the real 202 response.
    """
    def __init__(self, inner):
        self._inner = inner

    def raw_post(self, *args, **kwargs):
        return self._inner.post(*args, **kwargs)

    def post(self, url, *args, **kwargs):
        # Sessions are only created by the explicit /api/session endpoint (Sprint 2). Legacy tests
        # that connect data without a session_id get one created for them here.
        upload_data = kwargs.get("data")
        needs_upload_session = url == "/api/connect/upload" and not (upload_data and upload_data.get("session_id"))
        sample_json = kwargs.get("json")
        needs_sample_session = url == "/api/connect/sample" and (sample_json is None or sample_json == {} or not sample_json.get("session_id"))
        if needs_upload_session or needs_sample_session:
            headers = kwargs.get("headers") or {}
            session = self._inner.post("/api/session", headers=headers, json={})
            if session.status_code == 200:
                sid = session.json()["session_id"]
                if url == "/api/connect/sample":
                    kwargs["json"] = {**(sample_json or {}), "session_id": sid}
                else:
                    kwargs["data"] = {**(upload_data or {}), "session_id": sid}
        response = self._inner.post(url, *args, **kwargs)
        if response.status_code != 202 or url not in ("/api/connect/upload", "/api/generate-report"):
            return response
        body = response.json()
        job_id = body.get("job_id")
        if not job_id:
            return response
        headers = kwargs.get("headers") or {}
        job = self._inner.get(f"/api/jobs/{job_id}", headers=headers)
        if job.status_code != 200:
            return response
        status = job.json().get("status")
        if status == "succeeded":
            from httpx import Response
            # Legacy synchronous contract: the final payload carried ``success: true``.
            return Response(200, json={"success": True, **(job.json().get("result") or {})}, request=response.request)
        if status == "failed":
            from httpx import Response
            payload = job.json()
            return Response(422, json={"success": False, "error": payload.get("error", "Background job failed."), "error_code": payload.get("error_code", "JOB_FAILED")}, request=response.request)
        return response

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture()
def client():
    with TestClient(api_module.app) as c:
        yield CompatClient(c)


@pytest.fixture()
def auth_headers(client):
    """Registers a fresh user per test and returns an Authorization header."""
    import uuid

    email = f"test-{uuid.uuid4().hex[:10]}@example.com"
    password = "TestPassword123!"
    r = client.post("/api/auth/register", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    verify_registered_email(client, email)
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _reset_ai_circuit_breakers():
    """Provider circuit-breaker state is process-global; never let one test's simulated outage
    trip the breaker for the next test."""
    import ai_guard
    ai_guard.reset_all()
    yield
    ai_guard.reset_all()
