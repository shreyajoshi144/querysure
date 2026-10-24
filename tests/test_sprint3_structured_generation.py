from __future__ import annotations

import json
import sqlite3
import sys
import types

import pytest

import api as api_module
from modules import sql_generator as sg
from modules import load_database, build_semantic_layer
from tests.sprint3_helpers import connect_csv, generate, register_headers

GOOD = {"sql": "SELECT s.region FROM sales AS s", "explanation": "regions", "confidence": "high", "complexity": "simple"}


@pytest.fixture()
def world(tmp_path):
    path = str(tmp_path / "t.db")
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE sales(id INTEGER PRIMARY KEY, region TEXT, amount REAL);"
                      "INSERT INTO sales VALUES (1,'N',1.5);")
    con.commit(); con.close()
    schema = load_database(path)
    return schema, build_semantic_layer(schema)


# ---- parser ---------------------------------------------------------------
def test_parse_valid_json_normalises_case():
    out = sg.parse_generation(json.dumps(GOOD))
    assert (out.sql, out.confidence, out.complexity) == (GOOD["sql"], "High", "Simple")


def test_parse_tolerates_one_json_fence_but_not_prose():
    assert sg.parse_generation("```json\n" + json.dumps(GOOD) + "\n```").sql == GOOD["sql"]
    for bad in ["SQL:\n```sql\nSELECT 1\n```", "Sure! " + json.dumps(GOOD), "SELECT 1", "", "[1,2]",
                json.dumps({"sql": 5}), json.dumps({"sql": "x", "confidence": "Certain"})]:
        with pytest.raises(ValueError):
            sg.parse_generation(bad)


def test_empty_sql_is_a_valid_refusal():
    out = sg.parse_generation(json.dumps({"sql": "", "explanation": "cannot answer", "confidence": "Low", "complexity": "Simple"}))
    assert out.sql == ""


# ---- transports request structured output ----------------------------------
def _capture_sdk(monkeypatch, module, cls_name, resp):
    seen = {}

    class Client:
        def __init__(self, *a, **k):
            def create(**kw):
                seen.update(kw)
                return resp
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
            self.messages = types.SimpleNamespace(create=create)
    mod = types.ModuleType(module)
    setattr(mod, cls_name, Client)
    monkeypatch.setitem(sys.modules, module, mod)
    return seen


def test_groq_and_openai_request_json_mode(monkeypatch):
    resp = types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="{}"))], usage=None)
    seen = _capture_sdk(monkeypatch, "groq", "Groq", resp)
    sg._call_groq("m", "k", "s", "u")
    assert seen["response_format"] == {"type": "json_object"}
    seen = _capture_sdk(monkeypatch, "openai", "OpenAI", resp)
    sg._call_openai("m", "k", "s", "u")
    assert seen["response_format"] == {"type": "json_object"}


def test_anthropic_uses_forced_tool_call_and_returns_tool_input_as_json(monkeypatch):
    resp = types.SimpleNamespace(content=[types.SimpleNamespace(type="tool_use", input=GOOD)],
                                 usage=types.SimpleNamespace(input_tokens=3, output_tokens=4))
    seen = _capture_sdk(monkeypatch, "anthropic", "Anthropic", resp)
    out = sg._call_anthropic("m", "k", "s", "u")
    assert seen["tool_choice"] == {"type": "tool", "name": "emit_sql"}
    assert seen["tools"][0]["input_schema"] == sg.GENERATION_JSON_SCHEMA
    assert sg.parse_generation(out.text).sql == GOOD["sql"]


# ---- retry once, then fail --------------------------------------------------
def test_invalid_json_is_retried_once_then_succeeds(world, monkeypatch):
    schema, semantic = world
    replies = iter(["not json at all", json.dumps(GOOD)])
    prompts = []
    monkeypatch.setattr(sg, "_call_groq", lambda m, k, sp, up: (prompts.append(up), sg.LLMResult(next(replies), 10, 5))[1])
    out = sg.generate_sql("regions", schema, "k", semantic, None)
    assert out["success"] is True and out["sql"] == GOOD["sql"]
    assert len(prompts) == 2 and "not valid JSON" in prompts[1] and "not valid JSON" not in prompts[0]
    assert out["usage"]["calls"] == 2  # both billed calls are accounted


def test_second_invalid_reply_fails_through_generation_failed_path(world, monkeypatch):
    schema, semantic = world
    calls = []
    monkeypatch.setattr(sg, "_call_groq", lambda m, k, sp, up: (calls.append(1), sg.LLMResult("SQL: SELECT 1", 10, 5))[1])
    out = sg.generate_sql("regions", schema, "k", semantic, None)
    assert out["success"] is False and out["error_type"] == "invalid_json" and len(calls) == 2
    assert out["usage"]["total_tokens"] == 30   # tokens spent on the two bad replies are not lost


def test_endpoint_maps_invalid_json_to_existing_generation_failed_contract(client, monkeypatch):
    monkeypatch.setattr(sg, "_call_groq", lambda *a: sg.LLMResult("garbage", 100, 10))
    headers, _ = register_headers(client)
    sid = connect_csv(client, headers)
    r = generate(client, headers, sid)
    assert r.status_code == 502
    body = r.json()
    assert body["error_code"] == "GENERATION_FAILED" and body["success"] is False
    # spent tokens still hit the user's usage and the AIInvocation row
    from db import AIInvocation, SessionLocal
    with SessionLocal() as db:
        row = db.query(AIInvocation).order_by(AIInvocation.created_at.desc()).first()
    assert row.success is False and row.error_type == "invalid_json" and row.estimated_cost_cents > 0


def test_success_response_shape_is_unchanged(client, monkeypatch):
    monkeypatch.setattr(sg, "_call_groq", lambda *a: sg.LLMResult(json.dumps(GOOD), 100, 10))
    headers, _ = register_headers(client)
    sid = connect_csv(client, headers)
    r = generate(client, headers, sid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"success", "session_id", "sql", "explanation", "confidence", "complexity", "tables_used",
                         "retrieval_confidence", "provider_used", "usage_today",
                         # v1.0.5 additive fields
                         "plan", "semantic_validation", "verification", "grounded_values"}
    assert body["confidence"] in {"High", "Medium", "Low"} and body["complexity"] in {"Simple", "Moderate", "Complex"}
    assert set(body["usage_today"]) == {"requests", "estimated_cost_usd", "budget_usd"}


def test_repair_loop_still_runs_with_json_and_validator_still_gates(world, monkeypatch):
    schema, semantic = world
    replies = iter([json.dumps({**GOOD, "sql": "SELECT s.nope FROM sales AS s"}), json.dumps({**GOOD, "sql": "SELECT s.region FROM sales AS s"})])
    monkeypatch.setattr(sg, "_call_groq", lambda *a: sg.LLMResult(next(replies), 10, 5))
    out = sg.generate_sql("regions", schema, "k", semantic, None)
    assert out["repair_attempts"] == 1 and out["validation"]["is_valid"] is True
    # a repair that returns garbage never turns into a "successful" valid query
    replies = iter([json.dumps({**GOOD, "sql": "SELECT s.nope FROM sales AS s"}), "garbage", "garbage"])
    monkeypatch.setattr(sg, "_call_groq", lambda *a: sg.LLMResult(next(replies), 10, 5))
    out = sg.generate_sql("regions", schema, "k", semantic, None)
    assert out["validation"]["is_valid"] is False


# ---- final hardening: unknown keys are rejected, via the same retry-once path -----------------------------
def test_unknown_keys_are_rejected_by_the_model_and_the_json_schema():
    assert sg.GenerationOutput.model_config["extra"] == "forbid"
    assert sg.GENERATION_JSON_SCHEMA["additionalProperties"] is False
    with pytest.raises(ValueError):
        sg.parse_generation(json.dumps({**GOOD, "extra_field": "x"}))
    with pytest.raises(ValueError):
        sg.parse_generation(json.dumps({**GOOD, "tool_calls": [{"name": "rm"}]}))
    assert sg.parse_generation(json.dumps(GOOD)).sql == GOOD["sql"]          # exact contract still accepted


def test_reply_with_extra_keys_is_retried_once_then_succeeds(world, monkeypatch):
    schema, semantic = world
    replies = iter([json.dumps({**GOOD, "note": "sneaky"}), json.dumps(GOOD)])
    prompts = []
    monkeypatch.setattr(sg, "_call_groq", lambda m, k, sp, up: (prompts.append(up), sg.LLMResult(next(replies), 10, 5))[1])
    out = sg.generate_sql("regions", schema, "k", semantic, None)
    assert out["success"] is True and out["sql"] == GOOD["sql"] and len(prompts) == 2
    assert "not valid JSON for the required schema" in prompts[1]


def test_reply_with_extra_keys_twice_fails_through_generation_failed(world, monkeypatch):
    schema, semantic = world
    calls = []
    monkeypatch.setattr(sg, "_call_groq", lambda *a: (calls.append(1), sg.LLMResult(json.dumps({**GOOD, "x": 1}), 10, 5))[1])
    out = sg.generate_sql("regions", schema, "k", semantic, None)
    assert out["success"] is False and out["error_type"] == "invalid_json" and len(calls) == 2
    assert out["usage"]["total_tokens"] == 30                                 # both billed replies still accounted
