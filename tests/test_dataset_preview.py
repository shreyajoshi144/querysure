"""Dataset Explorer row preview: GET /api/dataset/preview.

It is browsing, not analytics: it must be owner-scoped, only accept tables from the profiled schema, never exceed
10 rows, and never spend a daily query slot or touch the session's query state.
"""
from __future__ import annotations

import io
import uuid

import pandas as pd

from tests.auth_test_utils import verify_registered_email


def _second_user(client) -> dict:
    email = f"other-{uuid.uuid4().hex[:10]}@example.com"
    password = "TestPassword123!"
    assert client.post("/api/auth/register", json={"email": email, "password": password}).status_code == 200
    verify_registered_email(client, email)
    token = client.post("/api/auth/login", json={"email": email, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _connect_sample(client, headers) -> str:
    sid = client.post("/api/session", headers=headers, json={}).json()["session_id"]
    r = client.post("/api/connect/sample", headers=headers, json={"session_id": sid})
    assert r.status_code == 200, r.text
    return sid, r.json()


def test_preview_returns_at_most_ten_rows_with_columns(client, auth_headers):
    sid, sample = _connect_sample(client, auth_headers)
    big = max(sample["tables"], key=lambda t: t["rows"])
    assert big["rows"] > 10, "sample table should be larger than the preview limit"
    r = client.get("/api/dataset/preview", headers=auth_headers, params={"session_id": sid, "table": big["name"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True and body["table"] == big["name"]
    assert body["row_count"] == len(body["rows"]) == 10 and body["limit"] == 10
    assert body["total_rows"] == big["rows"]
    assert body["columns"] == [c["name"] for c in big["columns"]]


def test_preview_does_not_spend_query_quota_or_change_session_state(client, auth_headers):
    sid, sample = _connect_sample(client, auth_headers)
    before = client.get("/api/account/usage", headers=auth_headers).json()["used_today"]
    for t in sample["tables"][:3]:
        assert client.get("/api/dataset/preview", headers=auth_headers, params={"session_id": sid, "table": t["name"]}).status_code == 200
    assert client.get("/api/account/usage", headers=auth_headers).json()["used_today"] == before
    trace = client.get("/api/answer-trace", headers=auth_headers, params={"session_id": sid}).json()
    # previews must not create query state: no question, SQL or execution record appears in the trace
    assert not any(trace["trace"].get(k) for k in ("question", "sql", "execution", "answer"))


def test_preview_rejects_unknown_and_injected_table_names(client, auth_headers):
    sid, sample = _connect_sample(client, auth_headers)
    real = sample["tables"][0]["name"]
    for bad in ("nope", f'{real}"; DROP TABLE {real}; --', f"{real} UNION SELECT 1", "sqlite_master", ""):
        r = client.get("/api/dataset/preview", headers=auth_headers, params={"session_id": sid, "table": bad})
        assert r.status_code in (404, 422), (bad, r.status_code)
        assert r.json()["success"] is False
    # the real table is still intact and readable
    assert client.get("/api/dataset/preview", headers=auth_headers, params={"session_id": sid, "table": real}).status_code == 200


def test_preview_is_owner_scoped_and_authenticated(client, auth_headers):
    sid, sample = _connect_sample(client, auth_headers)
    name = sample["tables"][0]["name"]
    other = _second_user(client)
    r = client.get("/api/dataset/preview", headers=other, params={"session_id": sid, "table": name})
    assert r.status_code == 404 and r.json()["success"] is False
    assert client.get("/api/dataset/preview", params={"session_id": sid, "table": name}).status_code == 401


def test_preview_without_a_dataset_is_a_clean_error(client, auth_headers):
    sid = client.post("/api/session", headers=auth_headers, json={}).json()["session_id"]
    r = client.get("/api/dataset/preview", headers=auth_headers, params={"session_id": sid, "table": "anything"})
    assert r.status_code == 400 and r.json()["error_code"] == "NO_DATABASE"


def test_preview_truncates_wide_cells(client, auth_headers):
    buf = io.BytesIO()
    pd.DataFrame({"note": ["x" * 5000, "short"], "n": [1, 2]}).to_excel(buf, index=False, engine="openpyxl")
    r = client.post("/api/connect/upload", headers=auth_headers, files={"file": ("wide.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    table = client.post("/api/schema-intelligence", headers=auth_headers, json={"session_id": sid}).json()["table_profiles"][0]["table"]
    body = client.get("/api/dataset/preview", headers=auth_headers, params={"session_id": sid, "table": table}).json()
    longest = max(len(str(r["note"])) for r in body["rows"])
    assert longest <= 201


def test_evaluation_summary_exposes_real_category_counts(client):
    body = client.get("/api/evaluation/product-summary").json()
    b = body["benchmark"]
    assert b["available"] is True
    assert sum(b["category_counts"].values()) == b["cases"]
    assert sorted(b["category_counts"]) == b["categories"]


# ---------------------------------------------------------------- pagination (default behaviour above is unchanged)

def test_preview_pages_are_disjoint_and_cover_the_table(client, auth_headers):
    sid, sample = _connect_sample(client, auth_headers)
    big = max(sample["tables"], key=lambda t: t["rows"])
    first = client.get("/api/dataset/preview", headers=auth_headers,
                       params={"session_id": sid, "table": big["name"], "page": 1, "page_size": 10}).json()
    second = client.get("/api/dataset/preview", headers=auth_headers,
                        params={"session_id": sid, "table": big["name"], "page": 2, "page_size": 10}).json()
    assert first["page"] == 1 and second["page"] == 2 and first["page_size"] == 10
    assert first["total_pages"] == -(-big["rows"] // 10)
    assert first["rows"] and second["rows"] and first["rows"] != second["rows"]
    # a size-50 page starts with the same rows as pages 1..5 of size 10 would
    wide = client.get("/api/dataset/preview", headers=auth_headers,
                      params={"session_id": sid, "table": big["name"], "page_size": 50}).json()
    assert wide["rows"][:10] == first["rows"] and wide["rows"][10:20] == second["rows"]


def test_preview_page_size_is_capped_and_out_of_range_pages_clamp(client, auth_headers):
    sid, sample = _connect_sample(client, auth_headers)
    big = max(sample["tables"], key=lambda t: t["rows"])
    capped = client.get("/api/dataset/preview", headers=auth_headers,
                        params={"session_id": sid, "table": big["name"], "page_size": 100000}).json()
    assert capped["page_size"] == 50 and len(capped["rows"]) <= 50
    past_end = client.get("/api/dataset/preview", headers=auth_headers,
                          params={"session_id": sid, "table": big["name"], "page": 9999, "page_size": 10}).json()
    assert past_end["page"] == past_end["total_pages"] and past_end["rows"]
    low = client.get("/api/dataset/preview", headers=auth_headers,
                     params={"session_id": sid, "table": big["name"], "page": -4, "page_size": 0}).json()
    assert low["page"] == 1 and low["page_size"] == 1 and len(low["rows"]) == 1


def test_preview_rejects_non_numeric_paging_without_a_500(client, auth_headers):
    sid, sample = _connect_sample(client, auth_headers)
    r = client.get("/api/dataset/preview", headers=auth_headers,
                   params={"session_id": sid, "table": sample["tables"][0]["name"], "page": "1;DROP TABLE x"})
    assert r.status_code == 422
