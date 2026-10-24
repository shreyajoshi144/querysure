from __future__ import annotations

import json

import api as api_module
from db import AIInvocation, SessionLocal
from tests.sprint3_helpers import connect_csv, fake_result, generate, register_headers


def _rows(user_email=None):
    with SessionLocal() as db:
        return db.query(AIInvocation).order_by(AIInvocation.created_at).all()


def test_success_writes_one_row_with_metadata_and_no_prompt_or_data(client, monkeypatch):
    monkeypatch.setattr(api_module, "generate_sql", lambda **k: fake_result())
    headers, _ = register_headers(client)
    sid = connect_csv(client, headers)
    before = len(_rows())
    r = generate(client, headers, sid)
    assert r.status_code == 200, r.text
    new = _rows()[before:]
    assert len(new) == 1
    row = new[0]
    assert (row.provider, row.model) == ("groq", "llama-test")
    assert row.prompt_version == "sql-gen-test" and row.latency_ms == 12
    assert row.repair_attempts == 1 and row.fallback_used is True
    assert row.validation_status == "valid" and row.success is True and row.error_type is None
    assert (row.input_tokens, row.output_tokens) == (100, 20)
    assert row.session_id == sid and row.request_id
    blob = json.dumps(row.retrieval_summary)
    assert "sales" in blob                       # names only
    assert "stub-key" not in blob and "total amount" not in blob and "N," not in blob


def test_failure_is_recorded_too(client, monkeypatch):
    monkeypatch.setattr(api_module, "generate_sql", lambda **k: {
        "success": False, "error": "all providers failed", "error_type": "provider_error",
        "prompt_version": "sql-gen-test", "latency_ms": 5})
    headers, _ = register_headers(client)
    sid = connect_csv(client, headers)
    before = len(_rows())
    r = generate(client, headers, sid)
    assert r.status_code == 502 and r.json()["error_code"] == "GENERATION_FAILED"
    row = _rows()[before:][0]
    assert row.success is False and row.error_type == "provider_error"


def test_invalid_sql_result_is_recorded_as_invalid(client, monkeypatch):
    monkeypatch.setattr(api_module, "generate_sql", lambda **k: fake_result(validation={"is_valid": False, "issues": []}))
    headers, _ = register_headers(client)
    sid = connect_csv(client, headers)
    before = len(_rows())
    assert generate(client, headers, sid).status_code == 200
    assert _rows()[before:][0].validation_status == "invalid"


def test_ai_usage_endpoint_is_owner_scoped_and_read_only(client, monkeypatch):
    monkeypatch.setattr(api_module, "generate_sql", lambda **k: fake_result())
    a, _ = register_headers(client)
    b, _ = register_headers(client)
    sid = connect_csv(client, a)
    assert generate(client, a, sid).status_code == 200
    mine = client.get("/api/ai-usage?limit=5", headers=a).json()["invocations"]
    theirs = client.get("/api/ai-usage", headers=b).json()["invocations"]
    assert len(mine) == 1 and theirs == []
    assert client.post("/api/ai-usage", headers=a).status_code == 405
    assert client.get("/api/ai-usage").status_code in (401, 403)
