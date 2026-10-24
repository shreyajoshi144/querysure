"""
Exercises the full pipeline through the real HTTP API, as far as it can go
without a configured LLM provider key (none is available in this sandbox --
see the final report). Covers: connect -> schema-intelligence ->
semantic-layer -> retrieve-tables (all local, no LLM involved), plus
confirming /api/generate-sql degrades to a clean, actionable error instead
of a crash or a generic 500 when no provider is configured -- this IS the
correct behavior to test here, since a real deployment without a key set
should behave exactly this way too.
"""
from __future__ import annotations

import io

import pandas as pd


def _upload_sample(client, auth_headers):
    buf = io.BytesIO()
    df = pd.DataFrame({
        "region": ["North", "South", "North", "East"],
        "amount": [100.0, 200.0, 150.0, 50.0],
    })
    df.to_excel(buf, index=False, engine="openpyxl")
    r = client.post(
        "/api/connect/upload",
        headers=auth_headers,
        files={"file": ("sales.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def test_schema_intelligence_after_upload(client, auth_headers):
    session_id = _upload_sample(client, auth_headers)
    r = client.post("/api/schema-intelligence", headers=auth_headers, json={"session_id": session_id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert len(body["table_profiles"]) == 1
    assert body["table_profiles"][0]["row_count"] == 4


def test_semantic_layer_after_upload(client, auth_headers):
    session_id = _upload_sample(client, auth_headers)
    r = client.post("/api/semantic-layer", headers=auth_headers, json={"session_id": session_id})
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True


def test_retrieve_tables_after_upload(client, auth_headers):
    session_id = _upload_sample(client, auth_headers)
    r = client.post(
        "/api/retrieve-tables",
        headers=auth_headers,
        json={"session_id": session_id, "question": "What is the total amount by region?"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True


def test_retrieve_tables_requires_a_question(client, auth_headers):
    session_id = _upload_sample(client, auth_headers)
    r = client.post("/api/retrieve-tables", headers=auth_headers, json={"session_id": session_id})
    assert r.status_code == 400
    assert r.json()["success"] is False


def test_generate_sql_fails_cleanly_with_no_llm_provider_configured(client, auth_headers):
    """No GROQ/OPENAI/ANTHROPIC key is set in this test environment (see
    conftest.py), which is exactly the "no provider configured" case a real
    deployment can also hit. This must be a clean, actionable 400 with an
    error_code the frontend can react to -- not a 500."""
    session_id = _upload_sample(client, auth_headers)
    r = client.post(
        "/api/generate-sql",
        headers=auth_headers,
        json={"session_id": session_id, "question": "What is the total amount by region?"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["success"] is False
    assert body.get("error_code") == "NO_LLM_PROVIDER"


def test_generate_sql_requires_a_connected_database(client, auth_headers):
    r = client.post("/api/session", headers=auth_headers)
    session_id = r.json()["session_id"]
    r = client.post(
        "/api/generate-sql",
        headers=auth_headers,
        json={"session_id": session_id, "question": "anything"},
    )
    assert r.status_code == 400
    assert r.json().get("error_code") == "NO_DATABASE"
