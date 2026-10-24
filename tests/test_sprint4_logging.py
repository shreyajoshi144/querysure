"""Sprint 4 item 1: PII-safe structured JSON logging + request/user/session correlation."""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

import api as api_module
from observability import context, errors
from observability.logging_setup import JsonFormatter, TextFormatter, UvicornAccessFilter, safe_frames
from observability.redaction import scrub_text, scrub_value
from tests.sprint3_helpers import connect_csv, register_headers
from tests.sprint4_helpers import fresh_store, log_lines  # noqa: F401 - fixtures


# ---- redaction -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,must_not_contain", [
    ("POST /api/auth/verify-email?token=abc123DEF456ghi789", "abc123DEF456ghi789"),
    ("failed for bob@example.com", "bob@example.com"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlLWJ5dGVz", "eyJhbGci"),
    ("DETAIL:  Key (email)=(alice@corp.io) already exists.", "alice@corp.io"),
    ("provider error sk-abcdefghijklmnopqrstuvwxyz123456", "sk-abcdefghijkl"),
    ("gsk_abcdefghijklmnopqrstuvwxyz", "gsk_abcdefghij"),
    ("postgresql://qs:hunter2@db:5432/x", "hunter2"),
    ("callback?code=OAUTHCODE&state=xyz&page=2", "OAUTHCODE"),
    ('{"api_key": "abc def ghi"}', "abc def ghi"),
    ("key AKIAABCDEFGHIJKLMNOP leaked", "AKIAABCDEFGHIJKLMNOP"),
    ("opaque " + "a1B2" * 15, "a1B2a1B2a1B2"),
])
def test_scrub_text_removes_secrets_and_pii(raw, must_not_contain):
    assert must_not_contain not in scrub_text(raw)


def test_scrub_text_keeps_ordinary_diagnostics_and_caps_length():
    assert scrub_text("status code: 500 input_tokens=120") == "status code: 500 input_tokens=120"
    assert len(scrub_text("x " * 5000)) < 2100


def test_scrub_value_redacts_denied_keys_and_nested_values():
    out = scrub_value({"email": "a@b.co", "sql": "select 1", "n": 3, "nested": {"authorization": "x", "note": "hi a@b.co"}})
    assert out["email"] == out["sql"] == "[redacted]"
    assert out["n"] == 3 and out["nested"]["authorization"] == "[redacted]" and "a@b.co" not in out["nested"]["note"]


# ---- formatter -------------------------------------------------------------------------------------------------
def test_json_formatter_emits_one_valid_object_with_correlation(log_lines):
    context.new_context("req-abc12345")
    context.bind_user("user-123")
    context.bind_session("sess-456")
    try:
        logging.getLogger("querysure.test").info("hello bob@example.com", extra={"event": "t", "latency_ms": 3, "question": "secret question"})
        lines, raw = log_lines()
    finally:
        context.clear()
    rec = [l for l in lines if l.get("event") == "t"][0]
    assert rec["request_id"] == "req-abc12345" and rec["level"] == "INFO" and rec["latency_ms"] == 3
    assert rec["user_ref"] == context.ref("user-123") and rec["session_ref"] == context.ref("sess-456")
    # references are pseudonymous: the raw ids, the e-mail and the denied field value never appear
    for forbidden in ("user-123", "sess-456", "bob@example.com", "secret question"):
        assert forbidden not in raw
    assert rec["question"] == "[redacted]"


def test_refs_are_stable_nonreversible_and_distinct():
    assert context.ref("u1") == context.ref("u1") != context.ref("u2")
    assert context.ref(None) is None and "u1" not in context.ref("u1") and len(context.ref("u1")) == 12


def test_exception_logging_has_type_and_frames_but_no_message_values_or_source(log_lines):
    try:
        raise ValueError("row value victim@example.com password=hunter2")
    except ValueError:
        logging.getLogger("querysure.test").exception("failed")
    lines, raw = log_lines()
    rec = [l for l in lines if l["msg"] == "failed"][0]
    assert rec["exc_type"] == "ValueError" and rec["stack"] and ":" in rec["stack"][-1]
    assert "exc_msg" not in rec
    assert "victim@example.com" not in raw and "hunter2" not in raw
    assert "raise ValueError" not in raw  # no source lines


def test_text_formatter_is_scrubbed_too():
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "user %s", ("bob@example.com",), None)
    assert "bob@example.com" not in TextFormatter().format(rec)


def test_uvicorn_access_filter_drops_client_and_query_string():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "f", 1, '%s - "%s %s HTTP/%s" %d',
                            ("203.0.113.9:5555", "GET", "/api/x?token=SECRETSECRET1", "1.1", 200), None)
    UvicornAccessFilter().filter(rec)
    out = rec.getMessage()
    assert "203.0.113.9" not in out and "SECRET" not in out and "/api/x" in out


def test_inbound_request_id_validation():
    assert context.valid_inbound_request_id("abc-12345_xyz") == "abc-12345_xyz"
    for bad in ("short", "has space in it", "x" * 80, "inj\nected-line", "", None, "<script>alert(1)</script>"):
        assert context.valid_inbound_request_id(bad) is None


# ---- through the real app ---------------------------------------------------------------------------------------
def test_request_logging_is_correlated_and_free_of_ids_emails_and_tokens(client, log_lines, fresh_store):
    headers, email = register_headers(client, "logs")
    sid = connect_csv(client, headers)
    r = client.get(f"/api/sessions?token=QUERYSTRINGSECRET123456", headers={**headers, "X-Request-ID": "trace-0001-abcd"})
    assert r.headers["X-Request-ID"] == "trace-0001-abcd"
    client.post(f"/api/auth/verify-email?token=ANOTHERSECRETTOKEN987654")
    lines, raw = log_lines()
    http = [l for l in lines if l.get("event") == "http_request"]
    mine = [l for l in http if l["request_id"] == "trace-0001-abcd"][0]
    assert mine["route"] == "/api/sessions" and mine["status"] == 200 and mine["user_ref"] and mine["duration_ms"] >= 0
    for forbidden in (email, "QUERYSTRINGSECRET", "ANOTHERSECRETTOKEN", sid):
        assert forbidden not in raw, forbidden
    # raw paths with ids are never logged: only the route template
    client.delete(f"/api/session/{sid}", headers=headers)
    lines, raw = log_lines()
    assert sid not in raw and any(l.get("route") == "/api/session/{session_id}" for l in lines)


def test_unhandled_exception_is_fingerprinted_without_its_message(log_lines, fresh_store):
    errors.reset()

    def boom():
        raise RuntimeError("leaky message victim@example.com SELECT * FROM secrets WHERE pw='hunter2'")
    api_module.app.add_api_route("/api/_s4_boom", boom, methods=["GET"])
    try:
        with TestClient(api_module.app, raise_server_exceptions=False) as c:
            r = c.get("/api/_s4_boom")
    finally:
        api_module.app.router.routes[:] = [x for x in api_module.app.router.routes if getattr(x, "path", "") != "/api/_s4_boom"]
    assert r.status_code == 500 and r.json()["error"] == "Internal server error." and r.json()["request_id"]
    lines, raw = log_lines()
    for forbidden in ("victim@example.com", "hunter2", "SELECT * FROM secrets"):
        assert forbidden not in raw, forbidden
    top = errors.top_errors()
    assert top and top[0]["type"] == "RuntimeError" and "boom" in top[0]["location"]
    assert "leaky" not in json.dumps(top)


def test_safe_frames_has_no_locals_or_source():
    try:
        secret_local = "VALUE-IN-LOCALS"  # noqa: F841
        raise KeyError("x")
    except KeyError as e:
        frames = safe_frames(e.__traceback__)
    assert frames and "VALUE-IN-LOCALS" not in " ".join(frames)


def test_log_format_text_option_exists():
    import config
    assert config.LOG_FORMAT in ("json", "text") and isinstance(JsonFormatter(), logging.Formatter)


def test_exception_messages_are_opt_in_and_still_scrubbed(monkeypatch, log_lines):
    import config
    monkeypatch.setattr(config, "LOG_EXCEPTION_MESSAGES", True)
    try:
        raise ValueError("bad value for victim@example.com")
    except ValueError:
        logging.getLogger("querysure.test").exception("failed-debug")
    lines, raw = log_lines()
    rec = [l for l in lines if l["msg"] == "failed-debug"][0]
    assert "bad value for" in rec["exc_msg"] and "victim@example.com" not in raw
