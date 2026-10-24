"""v1.0.3: cross-provider LLM fallback, bounded retries, shared attempt budget, sanitized diagnostics, admin provider view."""
from __future__ import annotations

import json
import logging

import pytest

import ai_guard
import config
import modules.sql_generator as sg
from observability import events
from tests.sprint3_helpers import register_headers

SECRET_KEY = "sk-SECRET-should-never-leak-1234567890"
GOOD = json.dumps({"sql": "SELECT 1", "explanation": "ok", "confidence": "High", "complexity": "Simple"})


class APITimeoutError(Exception):
    pass


class RateLimitError(Exception):
    status_code = 429


class AuthenticationError(Exception):
    status_code = 401


class NotFoundError(Exception):
    status_code = 404


class InternalServerError(Exception):
    status_code = 503


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    ai_guard.reset_all()
    ai_guard.reset_attempts()
    monkeypatch.setattr(sg, "GROQ_MODEL", "g-main")
    monkeypatch.setattr(sg, "GROQ_FALLBACK_MODEL", "")
    monkeypatch.setattr(sg, "OPENAI_API_KEY", "o-key")
    monkeypatch.setattr(sg, "OPENAI_FALLBACK_MODEL", "o-model")
    monkeypatch.setattr(sg, "ANTHROPIC_API_KEY", "a-key")
    monkeypatch.setattr(sg, "ANTHROPIC_FALLBACK_MODEL", "a-model")
    monkeypatch.setattr(config, "LLM_PROVIDER_ORDER", ["groq", "openai", "anthropic"])
    monkeypatch.setattr(config, "LLM_TRANSIENT_RETRIES", 1)
    monkeypatch.setattr(config, "LLM_RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(config, "LLM_MAX_ATTEMPTS_PER_REQUEST", 8)
    monkeypatch.setattr(config, "CIRCUIT_FAILURE_THRESHOLD", 50, raising=False)
    yield
    ai_guard.reset_all()
    ai_guard.reset_attempts()


def _wire(monkeypatch, groq=None, openai=None, anthropic=None):
    calls = []

    def make(name, behaviour):
        def fn(model, key, sp, up):
            calls.append(f"{name}:{model}")
            b = behaviour.pop(0) if isinstance(behaviour, list) else behaviour
            if isinstance(b, Exception):
                raise b
            return b
        return fn
    monkeypatch.setattr(sg, "_call_groq", make("groq", groq if groq is not None else GOOD))
    monkeypatch.setattr(sg, "_call_openai", make("openai", openai if openai is not None else GOOD))
    monkeypatch.setattr(sg, "_call_anthropic", make("anthropic", anthropic if anthropic is not None else GOOD))
    return calls


def test_failover_to_next_provider_on_auth_error(monkeypatch):
    calls = _wire(monkeypatch, groq=AuthenticationError(f"bad key {SECRET_KEY}"))
    text, used, attempts = sg._run_provider_chain("s", "u", "g-key")
    assert used == "openai:o-model" and calls == ["groq:g-main", "openai:o-model"]  # auth is NOT retried
    assert attempts[0].error == "auth"


def test_configurable_order(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER_ORDER", ["anthropic", "groq"])
    calls = _wire(monkeypatch)
    _, used, _ = sg._run_provider_chain("s", "u", "g-key")
    assert used == "anthropic:a-model" and calls == ["anthropic:a-model"]
    assert [c["provider"] for c in sg.describe_provider_chain("g-key")] == ["anthropic", "groq", "openai"]


def test_unconfigured_provider_skipped(monkeypatch):
    monkeypatch.setattr(sg, "OPENAI_API_KEY", "")
    calls = _wire(monkeypatch, groq=AuthenticationError("x"))
    _, used, _ = sg._run_provider_chain("s", "u", "g-key")
    assert used == "anthropic:a-model" and "openai:o-model" not in calls


def test_timeout_retried_then_fallback(monkeypatch):
    calls = _wire(monkeypatch, groq=[APITimeoutError("t"), APITimeoutError("t")])
    _, used, attempts = sg._run_provider_chain("s", "u", "g-key")
    assert calls == ["groq:g-main", "groq:g-main", "openai:o-model"]
    assert [a.error for a in attempts[:2]] == ["timeout", "timeout"] and used == "openai:o-model"


def test_rate_limit_fails_over_without_app_retry(monkeypatch):
    calls = _wire(monkeypatch, groq=[RateLimitError("slow down"), GOOD])
    _, used, attempts = sg._run_provider_chain("s", "u", "g-key")
    assert used == "openai:o-model" and calls == ["groq:g-main", "openai:o-model"] and attempts[0].error == "rate_limited"


def test_server_error_retried_then_recovers(monkeypatch):
    calls = _wire(monkeypatch, groq=[InternalServerError("503"), GOOD])
    _, used, _ = sg._run_provider_chain("s", "u", "g-key")
    assert used == "groq:g-main" and calls == ["groq:g-main", "groq:g-main"]


def test_invalid_model_not_retried(monkeypatch):
    calls = _wire(monkeypatch, groq=NotFoundError("model g-main does not exist"))
    _, used, attempts = sg._run_provider_chain("s", "u", "g-key")
    assert attempts[0].error == "model_not_found" and calls.count("groq:g-main") == 1 and used == "openai:o-model"


def test_empty_model_id_removes_entry(monkeypatch):
    monkeypatch.setattr(sg, "OPENAI_FALLBACK_MODEL", "")
    assert [c["provider"] for c in sg.describe_provider_chain("g-key")] == ["groq", "anthropic"]


def test_all_providers_fail_sanitized(monkeypatch):
    recorded = []
    monkeypatch.setattr(events, "record_event", lambda kind, **kw: recorded.append((kind, kw)))
    _wire(monkeypatch, groq=AuthenticationError(SECRET_KEY), openai=InternalServerError(SECRET_KEY),
          anthropic=NotFoundError(SECRET_KEY))
    with pytest.raises(sg.ProviderError) as exc:
        sg._run_provider_chain("s", "u", "g-key")
    msg = str(exc.value)
    assert SECRET_KEY not in msg and "auth" in msg and "server_error" in msg and "model_not_found" in msg
    assert recorded and recorded[-1][0] == "llm_all_providers_failed"
    assert SECRET_KEY not in json.dumps(recorded[-1][1]["detail"])


def test_shared_attempt_budget(monkeypatch):
    monkeypatch.setattr(config, "LLM_MAX_ATTEMPTS_PER_REQUEST", 2)
    calls = _wire(monkeypatch, groq=[APITimeoutError("t")] * 5, openai=APITimeoutError("t"))
    token = ai_guard.start_deadline(60)
    try:
        with pytest.raises(sg.ProviderError):
            sg._run_provider_chain("s", "u", "g-key")
        assert len(calls) == 2
        # Budget is shared: a second chain run in the same request (JSON retry / repair) gets nothing more.
        with pytest.raises(sg.ProviderError) as exc:
            sg._run_provider_chain("s", "u", "g-key")
        assert len(calls) == 2 and "attempt_limit" in str(exc.value)
    finally:
        ai_guard.reset_deadline(token)


def test_malformed_json_falls_back_to_structured_error(monkeypatch):
    _wire(monkeypatch, groq="not json at all", openai="still not json", anthropic="nope")
    with pytest.raises(sg.StructuredOutputError):
        sg._run_structured("s", "u", "g-key")


def test_malformed_sql_from_fallback_still_validated(monkeypatch):
    bad = json.dumps({"sql": "DROP TABLE sales", "explanation": "x", "confidence": "High", "complexity": "Simple"})
    _wire(monkeypatch, groq=AuthenticationError("x"), openai=bad, anthropic=bad)
    schema = {"tables": {"sales": {"columns": [{"name": "region", "type": "TEXT"}], "row_count": 1}}}
    out = sg.generate_sql(question="total by region", schema=schema, api_key="g-key")
    v = out.get("validation") or out.get("validation_result") or {}
    assert v.get("is_valid") is False, out  # fallback output gets the same validator, never bypassed


def test_fallback_event_and_structured_log(monkeypatch, caplog):
    recorded = []
    monkeypatch.setattr(events, "record_event", lambda kind, **kw: recorded.append((kind, kw)))
    _wire(monkeypatch, groq=AuthenticationError(f"Bearer {SECRET_KEY}"))
    with caplog.at_level(logging.INFO, logger="querysure.ai"):
        sg._run_provider_chain("SYSTEM-PROMPT-SECRET", "USER-QUESTION-SECRET", "g-key")
    assert recorded[0][0] == "llm_fallback" and recorded[0][1]["detail"]["provider"] == "openai"
    lines = [r for r in caplog.records if getattr(r, "event", None) == "llm_attempt"]
    assert [(r.provider, r.outcome, r.error_code) for r in lines] == [("groq", "failure", "auth"), ("openai", "success", None)]
    for r in lines:
        blob = json.dumps(r.__dict__, default=str)
        assert SECRET_KEY not in blob and "USER-QUESTION-SECRET" not in blob and "SYSTEM-PROMPT-SECRET" not in blob
        assert isinstance(r.latency_ms, float) and r.model


def test_event_detail_allowlist_drops_secrets():
    clean = events.sanitize_detail({"provider": "groq", "model": "m-1", "api_key": SECRET_KEY, "reason": f"x {SECRET_KEY}!"})
    assert clean == {"provider": "groq", "model": "m-1"}


# ---- admin surface --------------------------------------------------------------------------------------------------
def test_providers_endpoint_requires_admin(client, monkeypatch):
    assert client.get("/api/admin/ops/providers").status_code in (401, 403)
    headers, email = register_headers(client, "plainuser")
    monkeypatch.setattr(config, "ADMIN_EMAILS", frozenset())
    assert client.get("/api/admin/ops/providers", headers=headers).status_code == 403


def test_providers_endpoint_admin_view_has_no_keys(client, monkeypatch):
    headers, email = register_headers(client, "opsadmin")
    monkeypatch.setattr(config, "ADMIN_EMAILS", frozenset({email.lower()}))
    monkeypatch.setattr(config, "GROQ_API_KEY", SECRET_KEY)
    monkeypatch.setattr(sg, "OPENAI_API_KEY", SECRET_KEY)
    r = client.get("/api/admin/ops/providers", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert SECRET_KEY not in r.text
    assert [c["provider"] for c in body["chain"]] == ["groq", "openai", "anthropic"]
    assert {"fallback_history", "circuit_breakers", "metrics", "settings", "incidents"} <= set(body)


def test_provider_status_names_only(client, monkeypatch):
    headers, _ = register_headers(client, "statususer")
    r = client.get("/api/provider-status", headers=headers)
    assert r.status_code == 200 and SECRET_KEY not in r.text
    data = r.json()
    data = data.get("data", data)
    assert "providers_configured" in data and "fallback_available" in data
