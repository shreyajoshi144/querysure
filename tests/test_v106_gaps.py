"""v1.0.6: quota exhaustion at 10/10 and reset, Flush Session (state cleared, history kept), honest reliability caps,
no secrets in logs, autofill hygiene, removed Download-report control."""
from __future__ import annotations

import json
import logging
import os
import re
import uuid

import pytest

import api as api_module
import db as db_module
from modules import sql_generator as sg
from modules.data_reliability_agent import assess_reliability
from tests.auth_test_utils import verify_registered_email
from tests.sprint3_helpers import connect_csv, generate, register_headers

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PW = "TestPassword123!"
GOOD = {"sql": "SELECT s.region FROM sales AS s", "explanation": "regions", "confidence": "High", "complexity": "Simple"}


def _sample(client, headers):
    sid = client.post("/api/session", headers=headers, json={}).json()["session_id"]
    assert client.post("/api/connect/sample", headers=headers, json={"session_id": sid}).status_code == 200
    return sid


def _exec(client, headers, sid, q="count rows"):
    return client.post("/api/execute-sql", headers=headers,
                       json={"session_id": sid, "sql": "SELECT COUNT(*) AS n FROM orders", "question": q})


def _used(headers_user_id):
    with db_module.SessionLocal() as db:
        return db_module.get_or_create_daily_usage(db, headers_user_id).query_count


def _uid(client, headers):
    return client.get("/api/auth/me", headers=headers).json()["id"]


# ------------------------------------------------------------------ quota
def test_quota_ten_of_ten_blocks_the_eleventh_and_resets_next_day(client, auth_headers, monkeypatch):
    sid = _sample(client, auth_headers)
    uid = _uid(client, auth_headers)
    for i in range(10):
        r = _exec(client, auth_headers, sid, f"count rows {i}")
        assert r.status_code == 200, (i, r.text)
    usage = client.get("/api/account/usage", headers=auth_headers).json()
    assert usage["used_today"] == 10 and usage["daily_limit"] == 10 and usage["remaining_today"] == 0 and usage["reset_at"]

    blocked = _exec(client, auth_headers, sid, "the eleventh")
    assert blocked.status_code == 429
    b = blocked.json()
    assert b["code"] == "QUERY_LIMIT_REACHED" and b["used_today"] == 10 and b["daily_limit"] == 10 and b["remaining"] == 0
    assert _used(uid) == 10                                    # a blocked request never consumes or overshoots a slot
    items = client.get("/api/history", headers=auth_headers).json()
    assert items["total"] == 10 and not any(i["question"] == "the eleventh" for i in items["items"])  # blocked run not recorded

    # reset: a new UTC day has no usage row yet, so the allowance is back
    monkeypatch.setattr(db_module, "_today_str", lambda: "2999-01-02")
    assert client.get("/api/account/usage", headers=auth_headers).json()["used_today"] == 0
    assert _exec(client, auth_headers, sid, "after the reset").status_code == 200
    assert _used(uid) == 1
    monkeypatch.undo()


def test_exhausted_quota_blocks_generation_before_any_llm_spend(client, auth_headers, monkeypatch):
    calls = []
    monkeypatch.setattr(api_module, "GROQ_API_KEY", "k")
    monkeypatch.setattr(sg, "_call_groq", lambda *a: (calls.append(1), sg.LLMResult(json.dumps(GOOD), 10, 5))[1])
    sid = _sample(client, auth_headers)
    uid = _uid(client, auth_headers)
    with db_module.SessionLocal() as db:
        row = db_module.get_or_create_daily_usage(db, uid)
        row.query_count = 10
        db.commit()
    r = client.post("/api/generate-sql", headers=auth_headers, json={"session_id": sid, "question": "total orders"})
    assert r.status_code == 429 and r.json()["code"] == "QUERY_LIMIT_REACHED"
    assert calls == []                                          # the provider was never called
    with db_module.SessionLocal() as db:
        row = db_module.get_or_create_daily_usage(db, uid)
        row.query_count = 9
        db.commit()
    r2 = client.post("/api/generate-sql", headers=auth_headers, json={"session_id": sid, "question": "total orders"})
    assert r2.status_code != 429 and calls, r2.text            # below the limit generation proceeds


# ------------------------------------------------------------------ flush session
def test_flush_session_clears_backend_state_but_keeps_history(client, auth_headers):
    sid = _sample(client, auth_headers)
    assert _exec(client, auth_headers, sid, "rows before flush").status_code == 200
    with db_module.SessionLocal() as db:
        before = db.query(db_module.PipelineSession).filter_by(id=sid).one()
        assert before.db_path and before.schema and before.sql_result is not None or before.query_result is not None
    before_hist = client.get("/api/history", headers=auth_headers).json()
    assert before_hist["total"] == 1

    r = client.post("/api/session/flush", headers=auth_headers, json={"session_id": sid})
    assert r.status_code == 200 and r.json()["flushed"] is True and r.json()["dataset_connected"] is False
    with db_module.SessionLocal() as db:
        row = db.query(db_module.PipelineSession).filter_by(id=sid).one()
        for field in ("db_path", "schema", "semantic", "retrieval", "sql", "sql_result", "question", "validation",
                      "query_result", "reliability", "insights"):
            assert getattr(row, field) in (None, {}, []), field
    # nothing runs against the flushed session...
    assert _exec(client, auth_headers, sid).json().get("error_code") == "NO_DATABASE"
    # ...but the saved history is untouched and still opens with its saved result
    after = client.get("/api/history", headers=auth_headers).json()
    assert after["total"] == 1 and after["items"][0]["id"] == before_hist["items"][0]["id"]
    full = client.get(f"/api/history/{after['items'][0]['id']}", headers=auth_headers).json()
    assert full["saved_result"] is True and full["result"]["rows"]


# ------------------------------------------------------------------ misleading scores
def _qr(verification=None, semantic=None):
    rows = [{"a": i, "b": f"x{i}"} for i in range(20)]
    return {"success": True, "status": "success", "rows": rows, "columns": ["a", "b"], "row_count": 20, "query_id": "q",
            "result_profile": {"row_count": 20, "distinct_counts": {"a": 20, "b": 20}}, "verification": verification,
            "semantic_validation": semantic}


SCHEMA = {"tables": {"t": {"columns": [{"name": "a", "type": "INTEGER"}, {"name": "b", "type": "TEXT"}], "row_count": 20}}, "relationships": []}
VALID = {"is_valid": True, "validation_score": 100, "risk_score": 0, "issues": [], "warnings": []}


def test_high_reliability_requires_independent_verification():
    unver = assess_reliability(SCHEMA, VALID, _qr(None))
    assert unver["reliability_score"] <= 89 and unver["reliability_level"] != "High" and unver["answer_verified"] is False
    assert "not independently verified" in (unver["capped_reason"] or "") or unver["reliability_score"] < 90
    ver = assess_reliability(SCHEMA, VALID, _qr({"status": "verified_match", "verified": True, "checks": []}))
    assert ver["answer_verified"] is True and ver["capped_reason"] is None
    assert ver["reliability_score"] >= unver["reliability_score"]


@pytest.mark.parametrize("status,cap", [("mismatch", 60), ("suspicious_empty", 60)])
def test_failed_verification_caps_the_score_and_is_explained(status, cap):
    r = assess_reliability(SCHEMA, VALID, _qr({"status": status, "verified": False, "checks": []}))
    assert r["reliability_score"] <= cap and r["capped_reason"] and status in r["capped_reason"]
    assert r["dimensions"]["result_correctness"]["status"] != "verified"


def test_semantic_issues_cap_the_score_even_with_perfect_validation():
    sem = {"status": "needs_attention", "issues": [{"type": "plan_filter", "message": "filter missing"}]}
    r = assess_reliability(SCHEMA, VALID, _qr({"status": "verified_match", "verified": True, "checks": []}, sem))
    assert r["reliability_score"] <= 74 and "semantic" in (r["capped_reason"] or "")


def test_ui_labels_validation_score_as_safety_not_correctness():
    html = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    assert "Validation score" not in html and "NOT a measure of whether the answer is correct" in html


# ------------------------------------------------------------------ no secrets in logs
def test_auth_flows_never_log_passwords_or_tokens(client, caplog):
    caplog.set_level(logging.DEBUG)
    email = f"log-{uuid.uuid4().hex[:8]}@example.com"
    secret_pw = "Sup3r-Secret-Passw0rd!x"
    assert client.post("/api/auth/register", json={"email": email, "password": secret_pw}).status_code == 200
    verify_registered_email(client, email)
    tok = client.post("/api/auth/login", json={"email": email, "password": secret_pw}).json()["access_token"]
    client.post("/api/auth/login", json={"email": email, "password": "Wrong-Passw0rd!zzz"})
    client.post("/api/auth/forgot-password", json={"email": email})
    client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
    text = "\n".join(f"{r.getMessage()} {getattr(r, 'args', '')}" for r in caplog.records)
    assert secret_pw not in text and "Wrong-Passw0rd!zzz" not in text and tok not in text
    assert not re.search(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.", text)       # no JWTs


# ------------------------------------------------------------------ autofill hygiene + removed control (static)
def _html():
    return open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()


def test_only_the_sign_in_form_asks_the_browser_to_fill_credentials():
    h = _html()
    assert 'id="loginEmail" autocomplete="username"' in h and 'id="loginPassword" autocomplete="current-password"' in h
    for field in ("registerEmail", "forgotEmail"):
        assert re.search(rf'id="{field}" autocomplete="off"', h), field
    for field in ("registerPassword", "resetPassword", "resetPasswordConfirm", "deleteConfirmPassword", "apiKeyInput"):
        assert re.search(rf'id="{field}"[^>]*autocomplete="new-password"', h), field
    assert not re.search(r'id="deleteConfirm(Email|Password)"[^>]*autocomplete="(current-password|username)"', h)
    # non-visible credential fields start disabled (never autofilled); JS enables them only when shown
    assert re.search(r'id="deleteConfirmPassword"[^>]*\bdisabled\b', h) and re.search(r'id="deleteConfirmEmail"[^>]*\bdisabled\b', h)
    js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
    assert "syncAuthFieldEligibility" in js and "setDeleteFieldsEligible" in js


def test_download_report_control_and_handler_are_gone_but_csv_json_remain():
    h = _html()
    js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
    assert "downloadReportBtn" not in h and "downloadReportBtn" not in js and "Download report" not in h
    assert 'id="downloadCsvBtn"' in h and 'id="downloadJsonBtn"' in h
    assert "querysure-result.csv" in js and "querysure-result.json" in js


def test_pipeline_stages_are_accessible_buttons_with_a_details_region():
    h = _html()
    assert len(re.findall(r'<button type="button" class="stage-btn" aria-pressed="false" aria-controls="stageDetail"', h)) == 8  # v1.0.7: the eight-node workflow canvas
    assert len(re.findall(r'class="node-details-btn"', h)) == 8  # every stage has a View details action
    assert 'id="stageDetail" role="region" aria-live="polite"' in h
