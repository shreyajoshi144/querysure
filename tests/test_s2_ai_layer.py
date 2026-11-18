"""Sprint 2: the AI provider layer (ai/), per-task routing, Gemini / xAI adapters, the ai_calls trace and the
side-by-side model evaluation. Provider HTTP is stubbed: no test spends credits or needs a key."""
from __future__ import annotations

import io
import json
import sqlite3
import types

import pytest

import api as api_module
import config
from ai import providers, router, trace as ai_trace
from ai.types import LLMResult, ProviderError
from db import AICall, AIInvocation, SessionLocal
from modules import build_semantic_layer, load_database, retrieve_context
from modules import sql_generator as sg
from tests.sprint3_helpers import register_headers

GOOD = {"sql": "SELECT s.region FROM sales AS s", "explanation": "x", "confidence": "High", "complexity": "Simple"}


@pytest.fixture()
def world(tmp_path):
    db = str(tmp_path / "w.db")
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE sales(id INTEGER PRIMARY KEY, region TEXT, amount REAL);"
                      "INSERT INTO sales VALUES (1,'N',1.5),(2,'S',2.5);")
    con.commit()
    con.close()
    schema = load_database(db)
    return schema, build_semantic_layer(schema), db


@pytest.fixture()
def only_groq(monkeypatch):
    """Deterministic provider config: Groq (two models) only, no task routes."""
    monkeypatch.setattr(sg, "GROQ_MODEL", "g-main")
    monkeypatch.setattr(sg, "GROQ_FALLBACK_MODEL", "g-fallback")
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setattr(sg, name, "")
    for name in ("GEMINI_API_KEY", "GEMINI_MODEL", "XAI_API_KEY", "XAI_MODEL"):
        monkeypatch.setattr(config, name, "")
    monkeypatch.setattr(config, "AI_TASK_ROUTES", {})
    monkeypatch.setattr(config, "LLM_PROVIDER_ORDER", ["groq", "openai", "anthropic"])


def _names(chain):
    return [e["name"] for e in chain]


# ---------------------------------------------------------------- routing

def test_default_chain_is_unchanged_and_new_providers_need_key_and_model(only_groq, monkeypatch):
    assert _names(sg._build_provider_chain("gk")) == ["groq:g-main", "groq:g-fallback"]
    monkeypatch.setattr(sg, "OPENAI_API_KEY", "ok")
    monkeypatch.setattr(sg, "OPENAI_FALLBACK_MODEL", "o-model")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "gem-key")          # key without a model: not configured
    monkeypatch.setattr(config, "XAI_MODEL", "x-model")              # model without a key: not configured
    assert _names(sg._build_provider_chain("gk")) == ["groq:g-main", "groq:g-fallback", "openai:o-model"]
    assert config.openai_compatible_providers_configured() is False
    monkeypatch.setattr(config, "GEMINI_MODEL", "gem-model")
    monkeypatch.setattr(config, "XAI_API_KEY", "x-key")
    assert _names(sg._build_provider_chain("gk"))[-2:] == ["gemini:gem-model", "xai:x-model"]
    assert config.openai_compatible_providers_configured() is True
    monkeypatch.setattr(config, "LLM_PROVIDER_ORDER", ["xai", "groq"])
    assert _names(sg._build_provider_chain("gk"))[:3] == ["xai:x-model", "groq:g-main", "groq:g-fallback"]


def test_task_routes_pin_one_task_only(only_groq, monkeypatch):
    monkeypatch.setattr(sg, "OPENAI_API_KEY", "ok")
    monkeypatch.setattr(sg, "OPENAI_FALLBACK_MODEL", "o-model")
    monkeypatch.setattr(config, "AI_TASK_ROUTES", {"sql_repair": [("openai", "o-repair"), ("anthropic", "")]})
    with router.task_scope("sql_repair"):
        assert _names(sg._build_provider_chain("gk")) == ["openai:o-repair"]  # anthropic listed but has no key
    assert _names(sg._build_provider_chain("gk")) == ["groq:g-main", "groq:g-fallback", "openai:o-model"]
    assert router.current_task() == "sql_generation"
    assert [c["model"] for c in sg.describe_provider_chain("gk", task="sql_repair")] == ["o-repair"]


def test_a_routed_task_with_no_configured_provider_fails_clearly(only_groq, monkeypatch):
    monkeypatch.setattr(config, "AI_TASK_ROUTES", {"sql_generation": [("anthropic", "")]})
    with pytest.raises(ProviderError, match="sql_generation"):
        sg._run_provider_chain("s", "u", "gk")


@pytest.mark.parametrize("raw, error", [("bogus=groq", "unknown task"), ("sql_repair=nope", "unknown provider"),
                                        ("sql_repair", "unknown task")])
def test_route_config_errors_fail_at_startup(raw, error):
    with pytest.raises(RuntimeError, match=error):
        config._parse_task_routes(raw)


def test_route_config_parses_providers_and_models():
    assert config._parse_task_routes(" sql_repair = openai:gpt-x , groq ; sql_generation=xai:grok-y ") == {
        "sql_repair": [("openai", "gpt-x"), ("groq", "")], "sql_generation": [("xai", "grok-y")]}
    assert config._parse_task_routes("") == {}


def test_unknown_task_scope_is_refused():
    with pytest.raises(ValueError):
        with router.task_scope("send_email"):
            pass


def test_repairs_run_as_their_own_task(world, only_groq, monkeypatch):
    schema, semantic, _ = world
    seen = []

    def fake(model, key, sp, up):
        seen.append(router.current_task())
        sql = "SELECT s.nope FROM sales AS s" if len(seen) == 1 else GOOD["sql"]
        return LLMResult(json.dumps({**GOOD, "sql": sql}), 10, 5)
    monkeypatch.setattr(sg, "_call_groq", fake)
    out = sg.generate_sql("regions", schema, "gk", semantic, retrieve_context("regions", semantic, cache_key="s2"))
    assert out["success"] is True and out["repair_attempts"] == 1
    assert seen == ["sql_generation", "sql_repair"]
    assert router.current_task() == "sql_generation"


# ---------------------------------------------------------------- Gemini / xAI adapters

class _FakeOpenAI:
    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.requests = []
        _FakeOpenAI.instances.append(self)
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def _create(self, **req):
        self.requests.append(req)
        msg = types.SimpleNamespace(content=json.dumps(GOOD))
        usage = types.SimpleNamespace(prompt_tokens=120, completion_tokens=30)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)


@pytest.fixture()
def fake_openai(monkeypatch):
    import openai
    _FakeOpenAI.instances = []
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    return _FakeOpenAI


@pytest.mark.parametrize("provider, base_url", [("gemini", providers.GEMINI_BASE_URL), ("xai", providers.XAI_BASE_URL)])
def test_openai_compatible_adapters_use_documented_endpoint_and_strict_json_schema(fake_openai, provider, base_url):
    call = getattr(sg, f"_call_{provider}")
    out = call("some-model", "key-123", "system", "user")
    assert isinstance(out, LLMResult) and (out.input_tokens, out.output_tokens) == (120, 30)
    client = fake_openai.instances[-1]
    assert client.kwargs["base_url"] == base_url and client.kwargs["api_key"] == "key-123"
    assert client.kwargs["max_retries"] == 1 and client.kwargs["timeout"]
    req = client.requests[-1]
    assert req["model"] == "some-model"
    fmt = req["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["name"] == "emit_sql" and fmt["json_schema"]["schema"] == sg.GENERATION_JSON_SCHEMA
    assert [m["role"] for m in req["messages"]] == ["system", "user"]


def test_gemini_only_deployment_generates_sql_end_to_end(world, only_groq, fake_openai, monkeypatch):
    schema, semantic, _ = world
    monkeypatch.setattr(config, "GEMINI_API_KEY", "gem-key")
    monkeypatch.setattr(config, "GEMINI_MODEL", "gem-model")
    out = sg.generate_sql("regions", schema, "", semantic, None)
    assert out["success"] is True and out["sql"] == GOOD["sql"]
    assert out["provider_used"] == "gemini:gem-model"
    assert out["usage"]["estimated"] is False and out["usage"]["input_tokens"] == 120


def test_provider_capabilities_are_listed_not_assumed_equal():
    caps = {name: p["structured_output"] for name, p in providers.PROVIDERS.items()}
    assert set(caps) == set(config._KNOWN_LLM_PROVIDERS)
    assert caps["anthropic"] == "forced_tool_call" and "beta" in caps["gemini"]


# ---------------------------------------------------------------- trace

def _user_id(client):
    headers, _ = register_headers(client, "s2")
    return headers, client.get("/api/auth/me", headers=headers).json()["id"]


def test_every_attempt_is_traced_without_prompt_content(client, only_groq, monkeypatch):
    _, uid = _user_id(client)
    monkeypatch.setattr(config, "LLM_TRANSIENT_RETRIES", 0)
    calls = []

    def flaky(model, key, sp, up):
        calls.append(model)
        if model == "g-main":
            raise RuntimeError("401 Unauthorized: bad key FAKE-KEY-should-not-leak")
        return LLMResult("{}", 50, 7)
    monkeypatch.setattr(sg, "_call_groq", flaky)
    with ai_trace.start(user_id=uid, request_id="req-s2-test-1") as tr:
        text, used, attempts = sg._run_provider_chain("SYSTEM-PROMPT-TEXT", "TOP SECRET QUESTION", "gk")
    assert used == "groq:g-fallback" and len(tr.calls) == 2
    with SessionLocal() as db:
        rows = db.query(AICall).filter(AICall.trace_id == tr.trace_id).order_by(AICall.attempt_no).all()
    assert [(r.provider, r.model, r.outcome, r.task_type, r.attempt_no) for r in rows] == [
        ("groq", "g-main", "failure", "sql_generation", 1), ("groq", "g-fallback", "success", "sql_generation", 2)]
    assert rows[0].error_category and rows[0].input_tokens is None
    assert (rows[1].input_tokens, rows[1].output_tokens, rows[1].usage_estimated) == (50, 7, False)
    assert rows[1].latency_ms is not None and rows[1].request_id == "req-s2-test-1"
    blob = json.dumps([{c.name: getattr(r, c.name) for c in AICall.__table__.columns} for r in rows], default=str)
    for secret in ("TOP SECRET", "SYSTEM-PROMPT", "sk-secret", "Unauthorized"):
        assert secret not in blob


def test_skipped_attempts_are_traced(only_groq, monkeypatch):
    import ai_guard
    monkeypatch.setattr(sg, "_call_groq", lambda *a: LLMResult("{}", 1, 1))
    monkeypatch.setattr(ai_guard, "consume_attempt", lambda: False)
    with ai_trace.start() as tr, pytest.raises(ProviderError):
        sg._run_provider_chain("s", "u", "gk")
    assert [(c["outcome"], c["error_category"]) for c in tr.calls] == [("skipped", "attempt_limit")]


def test_no_trace_means_nothing_is_written(only_groq, monkeypatch):
    monkeypatch.setattr(sg, "_call_groq", lambda *a: LLMResult("{}", 1, 1))
    with SessionLocal() as db:
        before = db.query(AICall).count()
    assert ai_trace.current() is None
    sg._run_provider_chain("s", "u", "gk")
    with SessionLocal() as db:
        assert db.query(AICall).count() == before


def test_trace_write_failure_never_breaks_generation(only_groq, monkeypatch):
    import db as db_module

    def broken():
        raise RuntimeError("database down")
    monkeypatch.setattr(db_module, "SessionLocal", broken)
    monkeypatch.setattr(sg, "_call_groq", lambda *a: LLMResult("{}", 1, 1))
    with ai_trace.start(user_id="00000000-0000-0000-0000-000000000000") as tr:
        text, used, _ = sg._run_provider_chain("s", "u", "gk")
    assert used == "groq:g-main" and len(tr.calls) == 1


# ---------------------------------------------------------------- API

def _connect(client, headers):
    sid = client._inner.post("/api/session", headers=headers, json={}).json()["session_id"]
    r = client.post("/api/connect/upload", headers=headers, data={"session_id": sid},
                    files={"file": ("sales.csv", io.BytesIO(b"region,amount\nN,1\nS,2\n"), "text/csv")})
    assert r.status_code == 200, r.text
    return sid


def test_generate_sql_returns_a_trace_the_owner_can_read(client, only_groq, monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "gk", raising=False)
    monkeypatch.setattr(api_module, "GROQ_API_KEY", "gk", raising=False)
    monkeypatch.setattr(sg, "_call_groq", lambda *a: LLMResult(json.dumps({**GOOD, "sql": "SELECT s.region FROM sales AS s"}), 300, 40))
    headers, uid = _user_id(client)
    other, _ = _user_id(client)
    sid = _connect(client, headers)
    r = client.post("/api/generate-sql", headers=headers, json={"session_id": sid, "question": "regions", "api_key": "gk"})
    assert r.status_code == 200, r.text
    trace_id = r.json()["trace_id"]
    t = client.get(f"/api/ai/traces/{trace_id}", headers=headers)
    assert t.status_code == 200, t.text
    body = t.json()
    assert body["summary"]["attempts"] == len(body["calls"]) >= 1 and body["summary"]["succeeded"] >= 1
    first = body["calls"][0]
    assert (first["task_type"], first["provider"], first["outcome"], first["input_tokens"]) == ("sql_generation", "groq", "success", 300)
    assert "sales" not in json.dumps(body["calls"]).lower()          # no SQL or schema in the trace
    assert client.get(f"/api/ai/traces/{trace_id}", headers=other).status_code == 404
    assert client.get("/api/ai/traces/" + "x" * 80, headers=headers).status_code == 404
    with SessionLocal() as db:
        inv = db.query(AIInvocation).filter(AIInvocation.user_id == uid).order_by(AIInvocation.created_at.desc()).first()
        assert inv.trace_id == trace_id
        assert db.query(AICall).filter(AICall.trace_id == trace_id).first().workspace_id  # attributed to a workspace
    usage = client.get("/api/ai-usage", headers=headers).json()["invocations"]
    assert usage[0]["trace_id"] == trace_id
    exported = client.get("/api/account/export", headers=headers).json()
    assert any(c["trace_id"] == trace_id for c in exported["ai_calls"])
    email = client.get("/api/auth/me", headers=headers).json()["email"]
    res = client.post("/api/account/delete", headers=headers, json={"confirm_email": email, "password": "TestPassword123!"})
    assert res.status_code == 200, res.text
    with SessionLocal() as db:
        assert db.query(AICall).filter(AICall.user_id == uid).count() == 0


def test_provider_status_counts_a_gemini_only_server(client, monkeypatch):
    for name in ("GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setattr(api_module, name, "", raising=False)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "gem-key")
    monkeypatch.setattr(config, "GEMINI_MODEL", "gem-model")
    headers, _ = _user_id(client)
    body = client.get("/api/provider-status", headers=headers).json()
    assert body["llm_configured"] is True and "gemini" in body["providers_configured"]


# ---------------------------------------------------------------- side-by-side evaluation

def test_route_override_pins_both_tasks(only_groq, monkeypatch):
    monkeypatch.setattr(sg, "OPENAI_API_KEY", "ok")
    monkeypatch.setattr(sg, "OPENAI_FALLBACK_MODEL", "o-model")
    with router.route_override({"sql_generation": [("openai", "o-cmp")], "sql_repair": [("openai", "o-cmp")]}):
        assert _names(sg._build_provider_chain("gk")) == ["openai:o-cmp"]
        with router.task_scope("sql_repair"):
            assert _names(sg._build_provider_chain("gk")) == ["openai:o-cmp"]
    assert _names(sg._build_provider_chain("gk")) == ["groq:g-main", "groq:g-fallback", "openai:o-model"]


def test_comparison_runs_each_route_and_tabulates(monkeypatch):
    from evaluation.multiformat import runner
    seen = []

    def fake_run(data_dir=None, only=None, holdout=False, live=False, delay_seconds=0.0, replay=None, route=None):
        assert live is True
        seen.append(route)
        overall = {"cases": 4, "answered_runs": 3, "provider_unavailable": 1, "exact_correct_of_answered_pct": 66.67,
                   "semantic_correct_of_answered_pct": 100.0, "clean_pct": 75.0, "median_latency_ms": 900.0,
                   "total_tokens": 1234, "estimated_cost_usd": 0.0012}
        return {"metrics": {"overall": overall}, "route": route}
    monkeypatch.setattr(runner, "run", fake_run)
    cmp = runner.compare(["groq", "openai:gpt-x"], holdout=True)
    assert seen == ["groq", "openai:gpt-x"]
    assert [r["route"] for r in cmp["comparison"]] == ["groq", "openai:gpt-x"]
    text = runner.render_comparison(cmp)
    assert "openai:gpt-x" in text and "66.67" in text and "held-out" in text
    with pytest.raises(ValueError):
        runner.compare(["nope:model"])


def test_route_requires_live_and_cli_refuses_compare_without_live(capsys):
    from evaluation.multiformat import runner
    from evaluation.multiformat.__main__ import main
    with pytest.raises(ValueError):
        runner.run(route="groq")
    assert main(["--compare", "groq"]) == 1
    assert "--live --yes" in capsys.readouterr().err
