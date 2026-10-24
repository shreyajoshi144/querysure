"""Shared helpers for Sprint 3 tests (kept out of conftest.py so existing fixtures stay untouched)."""
from __future__ import annotations

import io
import uuid

from tests.auth_test_utils import verify_registered_email


def register_headers(client, prefix="s3"):
    email = f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"
    pw = "TestPassword123!"
    assert client.post("/api/auth/register", json={"email": email, "password": pw}).status_code == 200
    verify_registered_email(client, email)
    r = client.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, email


def connect_csv(client, headers, csv_bytes=b"region,amount\nN,1\nS,2\n", name="sales.csv"):
    sid = client._inner.post("/api/session", headers=headers, json={}).json()["session_id"]
    r = client.post("/api/connect/upload", headers=headers, data={"session_id": sid},
                    files={"file": (name, io.BytesIO(csv_bytes), "text/csv")})
    assert r.status_code == 200, r.text
    return sid


def generate(client, headers, sid, question="total amount by region"):
    return client.post("/api/generate-sql", headers=headers,
                       json={"session_id": sid, "question": question, "api_key": "stub-key"})


def fake_result(**over):
    base = {"success": True, "sql": "SELECT s.region FROM sales AS s", "explanation": "stub",
            "confidence": "High", "complexity": "Simple", "provider_used": "groq:llama-test",
            "prompt_version": "sql-gen-test", "latency_ms": 12, "repair_attempts": 1,
            "fallback_used": True, "validation": {"is_valid": True, "issues": []},
            "usage": {"input_tokens": 100, "output_tokens": 20, "estimated": False}}
    base.update(over)
    return base
