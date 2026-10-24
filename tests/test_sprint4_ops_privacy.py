"""Sprint 4 item 4: privacy-safe admin / ops dashboard. Aggregate system metrics ONLY.

The admin must not be able to reach prompts, datasets, files, SQL, results, reports, raw LLM context -- nor other users'
identities -- through the ops surface, and admin status must not widen access to any user-facing route."""
from __future__ import annotations

import ast
import inspect
import json
import os

import pytest

import api as api_module
import config
import ops_api
from observability import errors, events
from tests.sprint3_helpers import connect_csv, fake_result, generate, register_headers
from tests.sprint4_helpers import fresh_store  # noqa: F401

SECRETS = {
    "question": "WHICH-CONFIDENTIAL-CUSTOMERS-CHURNED-QUESTION",
    "sql": "SELECT s.region AS CONFIDENTIAL_SQL_ALIAS FROM sales AS s",
    "dataset": "payroll_CONFIDENTIAL_dataset.csv",
    "cell": "CONFIDENTIAL-CELL-VALUE-42",
}


@pytest.fixture()
def admin(client, monkeypatch):
    headers, email = register_headers(client, "admin")
    monkeypatch.setattr(config, "ADMIN_EMAILS", frozenset({email.lower()}))
    return headers, email


@pytest.fixture()
def busy_system(client, monkeypatch, fresh_store):
    """A victim user who has uploaded data, asked a question, generated SQL, executed it, and hit an error."""
    headers, email = register_headers(client, "victim")
    sid = connect_csv(client, headers, csv_bytes=f"region,amount\n{SECRETS['cell']},1\nS,2\n".encode(), name=SECRETS["dataset"])
    monkeypatch.setattr(api_module, "generate_sql", lambda **kw: fake_result(
        sql=SECRETS["sql"], explanation=SECRETS["question"],
        usage={"input_tokens": 120, "output_tokens": 30, "estimated": False, "cost_cents": 0.01}))
    assert client.post("/api/generate-sql", headers=headers, json={"session_id": sid, "question": SECRETS["question"], "api_key": "k"}).status_code == 200
    from db import PipelineSession, SessionLocal
    with SessionLocal() as db:
        table = next(iter(db.query(PipelineSession).filter(PipelineSession.id == sid).one().schema["tables"]))
    assert client.post("/api/execute-sql", headers=headers, json={"session_id": sid, "sql": f"SELECT t.region FROM {table} AS t"}).status_code == 200
    SECRETS["table"] = table
    events.record_event("test_privacy", severity="info", component="ai", detail={"provider": "groq", "question": SECRETS["question"]}, sync=True)
    try:
        raise RuntimeError(f"failure involving {SECRETS['cell']} and {email}")
    except RuntimeError as exc:
        errors.capture_exception(exc, component="api")
    return headers, email, sid


# ---- access control --------------------------------------------------------------------------------------------------
ENDPOINTS = ["/api/admin/ops/summary", "/api/admin/ops/slo", "/api/admin/ops/incidents", "/api/admin/ops/events",
             "/api/admin/ops/errors", "/api/admin/ops/metrics"]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_anonymous_gets_401_and_ordinary_users_get_403(client, path):
    assert client.get(path).status_code in (401, 403)
    headers, _ = register_headers(client, "plain")
    assert client.get(path, headers=headers).status_code == 403


def test_nobody_is_admin_by_default():
    assert config.ADMIN_EMAILS == frozenset() or isinstance(config.ADMIN_EMAILS, frozenset)
    import importlib
    old = os.environ.pop("ADMIN_EMAILS", None)
    try:
        assert importlib.reload(config).ADMIN_EMAILS == frozenset()
    finally:
        if old is not None:
            os.environ["ADMIN_EMAILS"] = old


def test_allow_listed_admin_gets_in_and_the_check_is_case_insensitive(client, admin, monkeypatch):
    headers, email = admin
    monkeypatch.setattr(config, "ADMIN_EMAILS", frozenset({email.lower()}))
    assert client.post("/api/admin/ops/alerts/evaluate", headers=headers).status_code in (200,)
    assert client.get("/api/admin/ops/summary", headers=headers).status_code == 200


def test_unverified_account_cannot_be_admin_even_if_listed(client, monkeypatch):
    import uuid
    email = f"unverified-{uuid.uuid4().hex[:8]}@example.com"
    assert client.post("/api/auth/register", json={"email": email, "password": "TestPassword123!"}).status_code == 200
    monkeypatch.setattr(config, "ADMIN_EMAILS", frozenset({email}))
    from db import SessionLocal, User
    from auth import create_access_token
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).one()
        assert u.email_verified is False
        token = create_access_token(u.id, auth_version=int(u.auth_version or 0))
    r = client.get("/api/admin/ops/summary", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_metrics_scraper_token_works_without_a_user_and_wrong_token_does_not(client, monkeypatch):
    monkeypatch.setattr(config, "OPS_METRICS_TOKEN", "scrape-token-123456")
    assert client.get("/api/admin/ops/metrics", headers={"X-Ops-Token": "scrape-token-123456"}).status_code == 200
    assert client.get("/api/admin/ops/metrics", headers={"X-Ops-Token": "wrong"}).status_code == 403
    monkeypatch.setattr(config, "OPS_METRICS_TOKEN", "")
    assert client.get("/api/admin/ops/metrics", headers={"X-Ops-Token": ""}).status_code == 403


def test_every_admin_route_requires_the_admin_dependency():
    from tests.route_utils import iter_api_routes
    routes = [(r, path, extra) for r, path, extra in iter_api_routes(api_module.app.routes) if path.startswith("/api/admin/")]
    assert len(routes) >= 8                               # every ops route must be found, not silently skipped
    for r, path, extra in routes:
        if path.endswith("/metrics"):
            continue                                      # token OR admin bearer, verified in its own tests above
        deps = {d.call for d in r.dependant.dependencies} | {getattr(d, "dependency", None) for d in extra}
        assert ops_api.require_admin in deps, path


# ---- no user content anywhere in what an admin can read ---------------------------------------------------------------
@pytest.mark.parametrize("path", ["/api/admin/ops/summary", "/api/admin/ops/slo", "/api/admin/ops/incidents",
                                  "/api/admin/ops/events?limit=500", "/api/admin/ops/errors", "/api/admin/ops/metrics"])
def test_admin_responses_contain_no_prompts_sql_dataset_names_cells_or_identities(client, admin, busy_system, path):
    admin_headers, _ = admin
    _vh, victim_email, victim_sid = busy_system
    r = client.get(path, headers=admin_headers)
    assert r.status_code == 200
    text = r.text
    for label, secret in SECRETS.items():
        assert secret not in text, f"{label} leaked via {path}"
    for forbidden in (victim_email, victim_sid, SECRETS["table"], "stub-key"):
        assert forbidden not in text, f"{forbidden!r} leaked via {path}"


def test_summary_is_numbers_names_and_fingerprints_only(client, admin, busy_system):
    body = client.get("/api/admin/ops/summary", headers=admin[0]).json()
    assert body["traffic"]["requests"] >= 1 and body["ai"]["requests"] >= 1
    assert body["ai"]["tokens"]["input"] >= 120 and body["privacy"].startswith("Aggregate")

    def keys(o, acc=None):
        acc = set() if acc is None else acc
        if isinstance(o, dict):
            for k, v in o.items():
                acc.add(k); keys(v, acc)
        elif isinstance(o, list):
            for v in o:
                keys(v, acc)
        return acc
    banned = {"sql", "question", "prompt", "rows", "result", "results", "email", "user_id", "session_id", "db_path", "report", "schema", "dataset"}
    assert not (keys(body) & banned)


def test_error_aggregation_has_type_and_location_but_never_the_message(busy_system):
    top = errors.top_errors()
    assert top and top[0]["type"] == "RuntimeError"
    assert set(top[0]) == {"fingerprint", "type", "component", "location", "count", "first_seen", "last_seen"}
    assert SECRETS["cell"] not in json.dumps(top)


# ---- admin gets NO extra power over user data -----------------------------------------------------------------------
def test_admin_cannot_read_or_use_another_users_session_or_data(client, admin, busy_system):
    admin_headers, _ = admin
    _vh, _e, victim_sid = busy_system
    assert client.get("/api/sessions", headers=admin_headers).json().get("sessions", []) == [] or all(
        s.get("id") != victim_sid for s in client.get("/api/sessions", headers=admin_headers).json().get("sessions", []))
    for method, path, body in (
        ("post", "/api/execute-sql", {"session_id": victim_sid, "sql": f"SELECT t.region FROM {SECRETS['table']} AS t"}),
        ("post", "/api/generate-sql", {"session_id": victim_sid, "question": "x", "api_key": "k"}),
        ("post", "/api/generate-insights", {"session_id": victim_sid}),
        ("post", "/api/generate-report", {"session_id": victim_sid}),
        ("get", f"/api/answer-trace?session_id={victim_sid}", None),
    ):
        r = getattr(client, method)(path, headers=admin_headers, **({"json": body} if body is not None else {}))
        assert r.status_code in (404, 422, 409), (path, r.status_code)
        assert SECRETS["cell"] not in r.text


def test_ops_module_never_touches_user_content_models_or_storage():
    tree = ast.parse(inspect.getsource(ops_api))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported |= {a.name for a in node.names}
            if node.module in ("modules", "report_storage", "jobs", "quotas"):
                pytest.fail(f"ops_api imports {node.module}")
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
    assert not (imported & {"PipelineSession", "Report", "Job", "AIInvocation", "UsageDaily", "session_to_dict", "SESSION_FIELDS",
                            "get_or_create_session", "store_report_file", "open_report"})


def test_ops_dashboard_page_is_static_and_xss_safe(client):
    r = client.get("/ops")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    js = open(os.path.join(api_module.ROOT, "static", "js", "ops.js"), encoding="utf-8").read()
    html = r.text
    assert "innerHTML" not in js and "document.write" not in js and "eval(" not in js
    assert "<script>" not in html.replace('<script src="/static/js/ops.js"></script>', "")      # CSP script-src 'self': no inline script
    assert "noindex" in html
    assert "script-src 'self'" in r.headers["content-security-policy"]


def test_sentry_scrubber_drops_messages_request_user_locals_and_breadcrumbs():
    event = {"level": "error", "message": "user alice@example.com failed", "request": {"data": {"question": "q"}, "headers": {"authorization": "x"}},
             "user": {"email": "alice@example.com"}, "breadcrumbs": [{"message": "SELECT secret"}], "extra": {"sql": "select 1"},
             "tags": {"component": "api", "fingerprint": "abc", "other": "x"},
             "exception": {"values": [{"type": "ValueError", "value": "bad row alice@example.com",
                                       "stacktrace": {"frames": [{"filename": "/app/api.py", "function": "f", "lineno": 3, "vars": {"password": "p"},
                                                                  "context_line": "secret = row['ssn']"}]}}]}}
    clean = errors.scrub_sentry_event(event)
    text = json.dumps(clean)
    for forbidden in ("alice@example.com", "question", "authorization", "SELECT secret", "select 1", "password", "ssn", "bad row"):
        assert forbidden not in text
    assert clean["exception"]["values"][0]["type"] == "ValueError" and clean["tags"] == {"component": "api", "fingerprint": "abc"}


def test_sentry_is_not_initialised_without_a_dsn(monkeypatch):
    monkeypatch.setattr(config, "SENTRY_DSN", "")
    monkeypatch.setattr(errors, "_sentry_ready", False)
    assert errors.init_sentry() is False


def test_real_sentry_client_delivers_a_scrubbed_event_end_to_end(monkeypatch):
    """Regression for a closure finding: the scrubber used to drop ``event_id``, the SDK then raised KeyError internally and EVERY
    event was silently lost. Uses the real SDK client + a capturing transport (no network)."""
    sentry_sdk = pytest.importorskip("sentry_sdk")
    from sentry_sdk.transport import Transport
    sent = []

    class Capture(Transport):
        def capture_envelope(self, envelope):
            sent.append(envelope)

    monkeypatch.setattr(config, "SENTRY_DSN", "http://publickey@127.0.0.1:1/1")
    monkeypatch.setattr(errors, "_sentry_ready", False)
    assert errors.init_sentry() is True
    client = sentry_sdk.get_client()
    client.transport = Capture({"dsn": config.SENTRY_DSN})
    try:
        raise RuntimeError("leak victim.person@example.com password=hunter2 SELECT * FROM secrets")
    except RuntimeError as exc:
        assert errors.capture_exception(exc, component="api")
    sentry_sdk.flush(2)
    assert len(sent) == 1, "the SDK dropped the event"
    payload = json.dumps([item.payload.json for item in sent[0].items if item.payload.json])
    for forbidden in ("victim.person@example.com", "hunter2", "SELECT * FROM secrets"):
        assert forbidden not in payload
    assert "RuntimeError" in payload
    sentry_sdk.get_client().close()
    monkeypatch.setattr(errors, "_sentry_ready", False)


def test_provider_that_only_failed_is_visible_in_the_ops_view(client, admin, fresh_store):
    """Closure finding: a provider with zero successes (i.e. a provider that is DOWN) was missing from ai.providers, hiding its
    failure reasons exactly when they matter."""
    from observability import metrics as m
    m.record_provider_failure("groq", "timeout")
    m.record_provider_failure("groq", "timeout")
    m.record_circuit_skip("groq")
    m.record_ai_call("openai", success=True, latency_ms=100, input_tokens=1, output_tokens=1, cost_cents=0.01, fallback=True)
    body = client.get("/api/admin/ops/summary", headers=admin[0]).json()
    providers = body["ai"]["providers"]
    assert providers["groq"]["failure_reasons"] == {"timeout": 2} and providers["groq"]["circuit_skips"] == 1
    assert providers["groq"]["success"] == 0 and providers["openai"]["success"] == 1
