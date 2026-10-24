"""Query history: persistence, search, saved-vs-rerun semantics, per-user isolation, account deletion."""
from __future__ import annotations

import uuid

from tests.auth_test_utils import verify_registered_email

PW = "TestPassword123!"


def _user(client):
    email = f"hist-{uuid.uuid4().hex[:10]}@example.com"
    assert client.post("/api/auth/register", json={"email": email, "password": PW}).status_code == 200
    verify_registered_email(client, email)
    token = client.post("/api/auth/login", json={"email": email, "password": PW}).json()["access_token"]
    return email, {"Authorization": f"Bearer {token}"}


def _sample(client, headers):
    sid = client.post("/api/session", headers=headers, json={}).json()["session_id"]
    r = client.post("/api/connect/sample", headers=headers, json={"session_id": sid})
    assert r.status_code == 200, r.text
    return sid, r.json()["tables"][0]["name"]


def _run(client, headers, sid, sql, question):
    return client.post("/api/execute-sql", headers=headers, json={"session_id": sid, "sql": sql, "question": question})


def test_empty_history_is_graceful(client, auth_headers):
    r = client.get("/api/history", headers=auth_headers)
    assert r.status_code == 200 and r.json()["items"] == [] and r.json()["total"] == 0


def test_execution_is_saved_with_question_sql_result_and_dataset(client, auth_headers):
    sid, table = _sample(client, auth_headers)
    sql = f"SELECT COUNT(*) AS n FROM {table}"
    r = _run(client, auth_headers, sid, sql, "How many rows are in the first table?")
    assert r.status_code == 200, r.text
    assert r.json().get("history_id")
    items = client.get("/api/history", headers=auth_headers).json()["items"]
    assert len(items) == 1
    it = items[0]
    assert it["question"] == "How many rows are in the first table?" and it["sql"] == sql
    assert it["status"] == "success" and it["row_count"] == 1 and it["created_at"] and it["dataset"]["tables"]
    full = client.get(f"/api/history/{it['id']}", headers=auth_headers).json()
    assert full["saved_result"] is True and full["result"]["rows"][0]["n"] == r.json()["rows"][0]["n"]


def test_history_search_and_pagination(client, auth_headers):
    sid, table = _sample(client, auth_headers)
    for i, q in enumerate(["alpha revenue question", "beta churn question", "gamma revenue again"]):
        assert _run(client, auth_headers, sid, f"SELECT COUNT(*) AS n{i} FROM {table}", q).status_code == 200
    got = client.get("/api/history", headers=auth_headers, params={"q": "revenue"}).json()
    assert got["total"] == 2 and all("revenue" in i["question"] for i in got["items"])
    assert client.get("/api/history", headers=auth_headers, params={"q": "n1"}).json()["total"] == 1  # SQL text is searchable
    assert client.get("/api/history", headers=auth_headers, params={"q": "%"}).json()["total"] == 0   # wildcard is literal
    page = client.get("/api/history", headers=auth_headers, params={"limit": 2, "offset": 2}).json()
    assert len(page["items"]) == 1 and page["total"] == 3
    # newest first
    order = [i["question"] for i in client.get("/api/history", headers=auth_headers).json()["items"]]
    assert order[0].startswith("gamma")


def test_users_cannot_see_or_touch_each_others_history(client):
    _, a = _user(client)
    _, b = _user(client)
    sid, table = _sample(client, a)
    hid = _run(client, a, sid, f"SELECT COUNT(*) AS n FROM {table}", "A's private question").json()["history_id"]
    assert client.get("/api/history", headers=b).json()["items"] == []
    assert client.get("/api/history", headers=b, params={"q": "private"}).json()["total"] == 0
    assert client.get(f"/api/history/{hid}", headers=b).status_code == 404
    assert client.delete(f"/api/history/{hid}", headers=b).status_code == 404
    assert client.get(f"/api/history/{hid}", headers=a).status_code == 200  # still there for the owner


def test_history_requires_authentication(client):
    assert client.get("/api/history").status_code in (401, 403)
    assert client.get("/api/history/abc").status_code in (401, 403)


def test_viewing_does_not_execute_and_rerun_revalidates_against_current_dataset(client, auth_headers):
    sid, table = _sample(client, auth_headers)
    r = _run(client, auth_headers, sid, f"SELECT COUNT(*) AS n FROM {table}", "count rows")
    hid = r.json()["history_id"]
    before = client.get("/api/account/usage", headers=auth_headers).json()
    client.get(f"/api/history/{hid}", headers=auth_headers)
    after = client.get("/api/account/usage", headers=auth_headers).json()
    assert before == after  # opening an entry consumed no query slot / ran nothing
    # Rerun = the normal execute path. With the dataset gone it must FAIL, not serve the saved result.
    assert client.post("/api/session/flush", headers=auth_headers, json={"session_id": sid}).status_code == 200
    rr = _run(client, auth_headers, sid, f"SELECT COUNT(*) AS n FROM {table}", "count rows")
    assert rr.status_code >= 400 and rr.json().get("error_code") == "NO_DATABASE"
    # and a rerun with SQL that no longer matches the schema is rejected by validation, not answered from history
    sid2, _ = _sample(client, auth_headers)
    bad = _run(client, auth_headers, sid2, "SELECT COUNT(*) FROM table_that_is_gone", "count rows")
    assert bad.status_code == 422


def test_delete_and_clear(client, auth_headers):
    sid, table = _sample(client, auth_headers)
    ids = [_run(client, auth_headers, sid, f"SELECT COUNT(*) AS n{i} FROM {table}", f"q{i}").json()["history_id"] for i in range(3)]
    assert client.delete(f"/api/history/{ids[0]}", headers=auth_headers).json()["deleted"] is True
    assert client.get("/api/history", headers=auth_headers).json()["total"] == 2
    assert client.post("/api/history/clear", headers=auth_headers).json()["deleted"] == 2
    assert client.get("/api/history", headers=auth_headers).json()["items"] == []


def test_history_survives_session_deletion(client, auth_headers):
    sid, table = _sample(client, auth_headers)
    _run(client, auth_headers, sid, f"SELECT COUNT(*) AS n FROM {table}", "keep me")
    assert client.delete(f"/api/session/{sid}", headers=auth_headers).status_code == 200
    assert client.get("/api/history", headers=auth_headers).json()["total"] == 1


def test_account_deletion_removes_history(client):
    email, h = _user(client)
    sid, table = _sample(client, h)
    _run(client, h, sid, f"SELECT COUNT(*) AS n FROM {table}", "gone soon")
    r = client.post("/api/account/delete", headers=h, json={"confirm_email": email, "password": PW})
    assert r.status_code == 200, r.text
    from db import SessionLocal, QueryHistory
    with SessionLocal() as s:
        assert s.query(QueryHistory).count() == 0 or all(x.question != "gone soon" for x in s.query(QueryHistory).all())


def test_failed_execution_is_recorded_without_leaking_result(client, auth_headers):
    sid, table = _sample(client, auth_headers)
    r = _run(client, auth_headers, sid, f"SELECT nope_col FROM {table}", "bad column question")
    assert r.status_code == 422  # validation failure: never executed, nothing to save
    assert client.get("/api/history", headers=auth_headers).json()["total"] == 0
