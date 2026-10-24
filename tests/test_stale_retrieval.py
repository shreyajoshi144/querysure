"""
/api/generate-sql must always retrieve context for the question being asked.

It used to reuse `row.retrieval` -- whatever was last saved on the session,
computed for an earlier question (or, after a re-upload, an earlier dataset) --
so SQL was generated from the wrong tables/columns. The LLM call is replaced
with a recorder so we can see exactly which retrieval it was handed.
"""
from __future__ import annotations

from tests.auth_test_utils import verify_registered_email

import io
import uuid

import pandas as pd
import pytest

import api as api_module


@pytest.fixture()
def captured(monkeypatch):
    calls = []

    def fake_generate_sql(**kwargs):
        calls.append(kwargs)
        return {
            "success": True, "sql": "SELECT 1", "explanation": "stub",
            "confidence": 90, "complexity": "simple", "provider_used": "stub",
        }

    monkeypatch.setattr(api_module, "generate_sql", fake_generate_sql)
    return calls


def _register(client):
    email = f"sr-{uuid.uuid4().hex[:10]}@example.com"
    pw = "TestPassword123!"
    assert client.post("/api/auth/register", json={"email": email, "password": pw}).status_code == 200
    verify_registered_email(client, email)
    r = client.post("/api/auth/login", json={"email": email, "password": pw})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _upload_csv(client, headers, filename, df, session_id=None):
    r = client.post(
        "/api/connect/upload", headers=headers,
        data={"session_id": session_id} if session_id else None,
        files={"file": (filename, df.to_csv(index=False).encode(), "text/csv")},
    )
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _generate(client, headers, sid, question):
    r = client.post("/api/generate-sql", headers=headers,
                    json={"session_id": sid, "question": question, "api_key": "stub-key"})
    assert r.status_code == 200, r.text
    return r


def test_generate_sql_uses_the_current_question_not_a_previous_retrieval(client, captured):
    headers = _register(client)
    sid = _upload_csv(client, headers, "sales.csv", pd.DataFrame({"region": ["N", "S"], "amount": [1.0, 2.0]}))

    first_q = "total amount by region"
    client.post("/api/retrieve-tables", headers=headers, json={"session_id": sid, "question": first_q})

    second_q = "how many rows are there"  # different question, no retrieve-tables call in between
    _generate(client, headers, sid, second_q)

    assert len(captured) == 1
    assert captured[0]["retrieval"]["question"] == second_q, (
        "generate-sql was handed the retrieval computed for the previous question"
    )


def test_retrieval_saved_on_session_tracks_the_latest_generate_call(client, captured):
    headers = _register(client)
    sid = _upload_csv(client, headers, "sales.csv", pd.DataFrame({"region": ["N"], "amount": [1.0]}))
    _generate(client, headers, sid, "question one")
    _generate(client, headers, sid, "question two")

    assert [c["retrieval"]["question"] for c in captured] == ["question one", "question two"]


def test_after_reupload_generate_sql_retrieves_from_the_new_dataset(client, captured):
    headers = _register(client)
    sid = _upload_csv(client, headers, "alpha_payroll.csv",
                      pd.DataFrame({"employee": ["x"], "alpha_salary": [1.0]}))
    client.post("/api/retrieve-tables", headers=headers, json={"session_id": sid, "question": "show data"})

    # Same session, different dataset; the saved retrieval still describes alpha.
    _upload_csv(client, headers, "beta_orders.csv",
                pd.DataFrame({"order_ref": ["p"], "beta_amount": [2.0]}), session_id=sid)
    _generate(client, headers, sid, "show data")

    tables = [t["table"] for t in captured[0]["retrieval"]["top_tables"]]
    assert "beta_orders" in tables
    assert "alpha_payroll" not in tables, "SQL context came from the previous dataset"
