from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import types

import pytest

import config
from modules import sql_generator as sg
from modules import load_database, build_semantic_layer, retrieve_context
from tests.sprint3_helpers import connect_csv, generate, register_headers

import api as api_module
from db import AIInvocation, SessionLocal


@pytest.fixture()
def world(tmp_path):
    path = str(tmp_path / "t.db")
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE sales(id INTEGER PRIMARY KEY, region TEXT, amount REAL);"
                      "INSERT INTO sales VALUES (1,'N',1.5),(2,'S',2.5);")
    con.commit(); con.close()
    schema = load_database(path)
    semantic = build_semantic_layer(schema)
    return schema, semantic


def _fake_sdk(monkeypatch, module, cls_name, make_resp):
    class Client:
        def __init__(self, *a, **k):
            outer = self
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **kw: make_resp()))
            self.messages = types.SimpleNamespace(create=lambda **kw: make_resp())
    mod = types.ModuleType(module)
    setattr(mod, cls_name, Client)
    monkeypatch.setitem(sys.modules, module, mod)


def test_groq_transport_reads_real_usage(monkeypatch):
    resp = lambda: types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="hi"))],
        usage=types.SimpleNamespace(prompt_tokens=321, completion_tokens=45))
    _fake_sdk(monkeypatch, "groq", "Groq", resp)
    out = sg._call_groq("m", "k", "sys", "user")
    assert (out.text, out.input_tokens, out.output_tokens) == ("hi", 321, 45)


def test_openai_transport_reads_real_usage(monkeypatch):
    resp = lambda: types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="yo"))],
        usage=types.SimpleNamespace(prompt_tokens=11, completion_tokens=7))
    _fake_sdk(monkeypatch, "openai", "OpenAI", resp)
    out = sg._call_openai("m", "k", "s", "u")
    assert (out.text, out.input_tokens, out.output_tokens) == ("yo", 11, 7)


def test_anthropic_transport_reads_real_usage(monkeypatch):
    resp = lambda: types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text="ok")],
        usage=types.SimpleNamespace(input_tokens=90, output_tokens=12))
    _fake_sdk(monkeypatch, "anthropic", "Anthropic", resp)
    out = sg._call_anthropic("m", "k", "s", "u")
    assert (out.text, out.input_tokens, out.output_tokens) == ("ok", 90, 12)


def test_missing_provider_usage_falls_back_to_heuristic_and_is_marked_estimated():
    u = sg._usage_for_call("groq:llama-3.3-70b-versatile", "a" * 400, "b" * 400, "c" * 80, "plain string")
    assert u["estimated"] is True and u["input_tokens"] == 200 and u["output_tokens"] == 20
    resp = sg.LLMResult("x", input_tokens=None, output_tokens=5)  # partial usage is not trusted
    assert sg._usage_for_call("groq:m", "s", "u", "x", resp)["estimated"] is True


def test_cost_uses_per_model_prices_and_unknown_model_is_not_free():
    assert config.llm_cost_cents("gpt-4o-mini", 1_000_000, 0) == pytest.approx(15.0)
    assert config.llm_cost_cents("gpt-4o-mini", 0, 1_000_000) == pytest.approx(60.0)
    assert config.llm_cost_cents("some-new-model", 1_000_000, 1_000_000) == pytest.approx(
        (config.LLM_DEFAULT_PRICE_IN_USD_PER_MTOK + config.LLM_DEFAULT_PRICE_OUT_USD_PER_MTOK) * 100)


def test_prices_are_env_overridable():
    env = {**os.environ, "LLM_PRICES_JSON": '{"gpt-4o-mini": [2, 4]}', "LLM_DEFAULT_PRICE_IN": "9"}
    code = "import config; print(config.llm_cost_cents('gpt-4o-mini',1000000,1000000), config.LLM_DEFAULT_PRICE_IN_USD_PER_MTOK)"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["600.0", "9.0"]
    bad = subprocess.run([sys.executable, "-c", "import config"], env={**env, "LLM_PRICES_JSON": "not json"},
                         capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert bad.returncode != 0 and "LLM_PRICES_JSON" in bad.stderr


def test_generate_sql_reports_real_usage_and_sums_repairs(world, monkeypatch):
    schema, semantic = world
    monkeypatch.setattr(config, "GROQ_API_KEY", "k", raising=False)
    calls = []

    def fake_groq(model, key, sp, up):
        calls.append(model)
        # 1st call: invalid SQL (unknown column) -> triggers one repair; 2nd: valid.
        sql = "SELECT s.nope FROM sales AS s" if len(calls) == 1 else "SELECT s.region FROM sales AS s"
        return sg.LLMResult(json.dumps({"sql": sql, "explanation": "x", "confidence": "High", "complexity": "Simple"}), 1000, 100)

    monkeypatch.setattr(sg, "_call_groq", fake_groq)
    out = sg.generate_sql("regions", schema, "k", semantic, retrieve_context("regions", semantic, cache_key="u1"))
    assert out["success"] is True
    u = out["usage"]
    assert out["repair_attempts"] == 1 and u["calls"] == 2
    assert (u["input_tokens"], u["output_tokens"], u["total_tokens"]) == (2000, 200, 2200)
    assert u["estimated"] is False
    assert u["cost_cents"] == pytest.approx(2 * config.llm_cost_cents(config.GROQ_MODEL, 1000, 100))


def test_plain_string_transport_is_flagged_estimated(world, monkeypatch):
    schema, semantic = world
    monkeypatch.setattr(sg, "_call_groq", lambda *a: json.dumps({"sql": "SELECT s.region FROM sales AS s", "explanation": "x", "confidence": "High", "complexity": "Simple"}))
    out = sg.generate_sql("regions", schema, "k", semantic, None)
    assert out["usage"]["estimated"] is True and out["usage"]["total_tokens"] > 0


def test_endpoint_charges_real_cost_and_records_tokens(client, monkeypatch):
    real = {"input_tokens": 5000, "output_tokens": 500, "total_tokens": 5500, "estimated": False,
            "cost_cents": 0.42, "calls": 1}
    monkeypatch.setattr(api_module, "generate_sql", lambda **k: {
        "success": True, "sql": "SELECT s.region FROM sales AS s", "explanation": "x", "confidence": "High",
        "complexity": "Simple", "provider_used": "groq:llama-3.3-70b-versatile", "usage": real,
        "validation": {"is_valid": True, "issues": []}})
    headers, _ = register_headers(client)
    sid = connect_csv(client, headers)
    r = generate(client, headers, sid)
    assert r.status_code == 200, r.text
    assert r.json()["usage_today"]["estimated_cost_usd"] == pytest.approx(0.0042, abs=1e-6)
    with SessionLocal() as db:
        row = db.query(AIInvocation).order_by(AIInvocation.created_at.desc()).first()
    assert (row.input_tokens, row.output_tokens, row.usage_estimated) == (5000, 500, False)
    assert row.estimated_cost_cents == pytest.approx(0.42)
