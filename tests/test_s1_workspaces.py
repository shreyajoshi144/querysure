"""Sprint 1: workspaces, roles, the dataset catalog and the audit trail.

Exercised through the real HTTP API (TestClient) with real uploads, so dataset registration, storage release, quota
and session behaviour are the production code paths, not mocks.
"""
from __future__ import annotations

import io
import json
import os

import pytest

import api as api_module
import storage
from db import AuditLog, Dataset, PipelineSession, SessionLocal, Workspace, WorkspaceMember, update_session
from tests.sprint3_helpers import register_headers


@pytest.fixture(autouse=True)
def local_uploads(tmp_path, monkeypatch):
    uploads = str(tmp_path / "uploads")
    os.makedirs(uploads)
    monkeypatch.setattr(storage, "BACKEND", "local")
    monkeypatch.setattr(storage, "UPLOAD_DIR", uploads)
    monkeypatch.setattr(storage, "CACHE_DIR", os.path.join(uploads, ".cache"))
    monkeypatch.setattr(api_module, "UPLOAD_DIR", uploads)
    return uploads


def _user(client, prefix="ws"):
    headers, email = register_headers(client, prefix)
    me = client.get("/api/auth/me", headers=headers).json()
    return {"h": headers, "email": email, "id": me["id"]}


def _session(client, user):
    return client._inner.post("/api/session", headers=user["h"], json={}).json()["session_id"]


def _upload(client, user, sid=None, workspace_id=None, name="sales.csv", body=b"region,amount\nN,1\nS,2\nE,3\n"):
    sid = sid or _session(client, user)
    data = {"session_id": sid}
    if workspace_id:
        data["workspace_id"] = workspace_id
    return client.post("/api/connect/upload", headers=user["h"], data=data,
                       files={"file": (name, io.BytesIO(body), "text/csv")}), sid


def _personal(client, user):
    r = client.get("/api/workspaces", headers=user["h"])
    assert r.status_code == 200, r.text
    return r.json()["personal_workspace_id"]


def _team(client, owner, name="Finance"):
    r = client.post("/api/workspaces", headers=owner["h"], json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["workspace"]["id"]


def _add(client, actor, ws_id, member, role):
    return client.post(f"/api/workspaces/{ws_id}/members", headers=actor["h"], json={"email": member["email"], "role": role})


def _datasets(client, user, ws_id):
    return client.get(f"/api/workspaces/{ws_id}/datasets", headers=user["h"])


def _audit_actions(ws_id):
    with SessionLocal() as db:
        return [a.action for a in db.query(AuditLog).filter(AuditLog.workspace_id == ws_id).order_by(AuditLog.created_at)]


@pytest.fixture()
def team(client):
    """A team workspace with one member of every role, plus an outsider."""
    users = {r: _user(client, r) for r in ("owner", "admin", "analyst", "viewer", "outsider")}
    ws_id = _team(client, users["owner"])
    for role in ("admin", "analyst", "viewer"):
        assert _add(client, users["owner"], ws_id, users[role], role).status_code == 201
    return ws_id, users


# ---------------------------------------------------------------- personal workspace

def test_every_user_gets_exactly_one_personal_workspace_as_owner(client):
    u = _user(client)
    first = _personal(client, u)
    assert _personal(client, u) == first  # idempotent
    team_id = _team(client, u, "Zeta")
    listed = [(w["id"], w["kind"]) for w in client.get("/api/workspaces", headers=u["h"]).json()["workspaces"]]
    assert listed == [(first, "personal"), (team_id, "team")], "the personal workspace is listed first"
    assert client.delete(f"/api/workspaces/{team_id}", headers=u["h"]).status_code == 200
    ws = client.get("/api/workspaces", headers=u["h"]).json()["workspaces"]
    assert [(w["id"], w["kind"], w["role"]) for w in ws] == [(first, "personal", "owner")]
    with SessionLocal() as db:
        assert db.query(Workspace).filter(Workspace.personal_owner_id == u["id"]).count() == 1


def test_personal_workspace_cannot_take_members_or_be_deleted(client):
    a, b = _user(client), _user(client)
    pid = _personal(client, a)
    r = _add(client, a, pid, b, "viewer")
    assert r.status_code == 400 and r.json()["error_code"] == "PERSONAL_WORKSPACE"
    r = client.delete(f"/api/workspaces/{pid}", headers=a["h"])
    assert r.status_code == 400 and r.json()["error_code"] == "PERSONAL_WORKSPACE"


# ---------------------------------------------------------------- dataset catalog

def test_upload_is_catalogued_in_the_personal_workspace_without_internal_refs(client):
    u = _user(client)
    r, sid = _upload(client, u)
    assert r.status_code == 200, r.text
    pid = _personal(client, u)
    body = r.json()
    assert body["workspace_id"] == pid and body["dataset_id"]
    listing = _datasets(client, u, pid).json()
    assert [d["id"] for d in listing["datasets"]] == [body["dataset_id"]]
    ds = listing["datasets"][0]
    assert ds["name"] == "sales.csv" and ds["file_format"] == "csv" and ds["table_count"] == 1 and ds["row_count"] == 3
    assert ds["created_by"] == u["id"] and ds["can_delete"] is True
    text = json.dumps(listing)
    assert "storage_ref" not in text and "uploads" not in text and ".db" not in text
    with SessionLocal() as db:
        assert db.get(PipelineSession, sid).dataset_id == body["dataset_id"]


def test_replacing_or_flushing_a_session_dataset_keeps_the_workspace_dataset(client):
    """Sprint 3 (decision 2): a dataset's lifetime is its own, not its session's. Replacing or flushing a session's dataset
    only unlinks it; DELETE /api/datasets/{id} is the one way to remove it."""
    u = _user(client)
    r1, sid = _upload(client, u, name="first.csv")
    first = r1.json()["dataset_id"]
    r2, _ = _upload(client, u, sid=sid, name="second.csv")
    second = r2.json()["dataset_id"]
    pid = _personal(client, u)
    assert {d["id"] for d in _datasets(client, u, pid).json()["datasets"]} == {first, second}
    assert client.post("/api/session/flush", headers=u["h"], json={"session_id": sid}).status_code == 200
    assert {d["id"] for d in _datasets(client, u, pid).json()["datasets"]} == {first, second}
    with SessionLocal() as db:
        row = db.get(PipelineSession, sid)
        assert row.dataset_id is None and row.dataset_version is None
    assert client.delete(f"/api/datasets/{first}", headers=u["h"]).status_code == 200
    assert [d["id"] for d in _datasets(client, u, pid).json()["datasets"]] == [second]


def test_sample_database_is_not_catalogued(client):
    u = _user(client)
    sid = _session(client, u)
    assert client.post("/api/connect/sample", headers=u["h"], json={"session_id": sid}).status_code == 200
    assert _datasets(client, u, _personal(client, u)).json()["datasets"] == []


def test_upload_into_team_workspace_requires_analyst(client, team):
    ws_id, users = team
    r, _ = _upload(client, users["analyst"], workspace_id=ws_id)
    assert r.status_code == 200, r.text
    assert r.json()["workspace_id"] == ws_id
    r, _ = _upload(client, users["viewer"], workspace_id=ws_id)
    assert r.status_code == 403 and r.json()["error_code"] == "FORBIDDEN"
    r, _ = _upload(client, users["outsider"], workspace_id=ws_id)
    assert r.status_code == 404 and r.json()["error_code"] == "WORKSPACE_NOT_FOUND"
    names = [d["created_by"] for d in _datasets(client, users["viewer"], ws_id).json()["datasets"]]
    assert names == [users["analyst"]["id"]]  # the viewer can see the catalog


def test_member_cannot_read_another_members_file_through_the_catalog(client, team):
    """Membership grants catalog visibility, not storage access: the uploader's session is still the only way in."""
    ws_id, users = team
    r, sid = _upload(client, users["analyst"], workspace_id=ws_id)
    assert r.status_code == 200
    for other in ("owner", "admin", "viewer"):
        res = client.get(f"/api/dataset/preview?session_id={sid}&table=sales", headers=users[other]["h"])
        assert res.status_code == 404, (other, res.text)


# ---------------------------------------------------------------- role matrix

ROLES = ("owner", "admin", "analyst", "viewer", "outsider")


@pytest.mark.parametrize("path, allowed", [
    ("", ("owner", "admin", "analyst", "viewer")),
    ("/members", ("owner", "admin", "analyst", "viewer")),
    ("/datasets", ("owner", "admin", "analyst", "viewer")),
    ("/audit", ("owner", "admin")),
])
def test_read_role_matrix(client, team, path, allowed):
    ws_id, users = team
    for role in ROLES:
        r = client.get(f"/api/workspaces/{ws_id}{path}", headers=users[role]["h"])
        expected = 200 if role in allowed else (404 if role == "outsider" else 403)
        assert r.status_code == expected, (path, role, r.status_code, r.text)


def test_rename_and_delete_role_matrix(client, team):
    ws_id, users = team
    for role, expected in (("outsider", 404), ("viewer", 403), ("analyst", 403)):
        assert client.patch(f"/api/workspaces/{ws_id}", headers=users[role]["h"], json={"name": "x"}).status_code == expected
    assert client.patch(f"/api/workspaces/{ws_id}", headers=users["admin"]["h"], json={"name": "Renamed"}).status_code == 200
    for role, expected in (("outsider", 404), ("viewer", 403), ("analyst", 403), ("admin", 403)):
        assert client.delete(f"/api/workspaces/{ws_id}", headers=users[role]["h"]).status_code == expected
    assert client.delete(f"/api/workspaces/{ws_id}", headers=users["owner"]["h"]).status_code == 200
    for role in ROLES:
        assert client.get(f"/api/workspaces/{ws_id}", headers=users[role]["h"]).status_code == 404


def test_member_management_rules(client, team):
    ws_id, users = team
    extra = _user(client, "extra")
    # analyst / viewer / outsider cannot add; admin cannot grant admin; owner can.
    assert _add(client, users["analyst"], ws_id, extra, "viewer").status_code == 403
    assert _add(client, users["viewer"], ws_id, extra, "viewer").status_code == 403
    assert _add(client, users["outsider"], ws_id, extra, "viewer").status_code == 404
    assert _add(client, users["admin"], ws_id, extra, "admin").status_code == 403
    assert _add(client, users["admin"], ws_id, extra, "viewer").status_code == 201
    assert _add(client, users["admin"], ws_id, extra, "viewer").json()["error_code"] == "ALREADY_MEMBER"
    assert _add(client, users["owner"], ws_id, {"email": "nobody-here@example.com"}, "viewer").status_code == 404
    assert _add(client, users["owner"], ws_id, extra, "owner").json()["error_code"] == "INVALID_ROLE"

    url = f"/api/workspaces/{ws_id}/members"
    # admin manages analyst/viewer only; nobody changes their own role or the owner's.
    assert client.patch(f"{url}/{extra['id']}", headers=users["admin"]["h"], json={"role": "analyst"}).status_code == 200
    assert client.patch(f"{url}/{extra['id']}", headers=users["admin"]["h"], json={"role": "admin"}).status_code == 403
    assert client.patch(f"{url}/{users['admin']['id']}", headers=users["admin"]["h"], json={"role": "viewer"}).status_code == 403
    assert client.patch(f"{url}/{users['owner']['id']}", headers=users["admin"]["h"], json={"role": "viewer"}).status_code == 403
    assert client.patch(f"{url}/{users['analyst']['id']}", headers=users["viewer"]["h"], json={"role": "viewer"}).status_code == 403
    assert client.patch(f"{url}/{users['admin']['id']}", headers=users["owner"]["h"], json={"role": "analyst"}).status_code == 200
    # remove: viewer cannot remove others but can leave; owner can neither leave nor be removed.
    assert client.delete(f"{url}/{users['analyst']['id']}", headers=users["viewer"]["h"]).status_code == 403
    assert client.delete(f"{url}/{users['viewer']['id']}", headers=users["viewer"]["h"]).status_code == 200
    assert client.delete(f"{url}/{users['owner']['id']}", headers=users["owner"]["h"]).status_code == 403
    assert client.delete(f"{url}/{extra['id']}", headers=users["analyst"]["h"]).status_code == 403
    assert client.delete(f"{url}/{extra['id']}", headers=users["owner"]["h"]).status_code == 200
    members = {m["email"]: m["role"] for m in client.get(url, headers=users["owner"]["h"]).json()["members"]}
    assert members == {users["owner"]["email"]: "owner", users["admin"]["email"]: "analyst", users["analyst"]["email"]: "analyst"}
    assert client.get(f"/api/workspaces/{ws_id}", headers=users["viewer"]["h"]).status_code == 404


def test_dataset_delete_permissions(client, team):
    ws_id, users = team
    r, _ = _upload(client, users["analyst"], workspace_id=ws_id)
    ds_id = r.json()["dataset_id"]
    assert client.delete(f"/api/datasets/{ds_id}", headers=users["outsider"]["h"]).status_code == 404
    assert client.delete(f"/api/datasets/{ds_id}", headers=users["viewer"]["h"]).status_code == 403
    r2, _ = _upload(client, users["admin"], workspace_id=ws_id, name="admins.csv")
    other = r2.json()["dataset_id"]
    assert client.delete(f"/api/datasets/{other}", headers=users["analyst"]["h"]).status_code == 403  # not theirs
    assert client.delete(f"/api/datasets/{ds_id}", headers=users["admin"]["h"]).status_code == 200    # admin may
    assert client.delete(f"/api/datasets/{other}", headers=users["admin"]["h"]).status_code == 200    # own
    assert client.delete("/api/datasets/" + "x" * 80, headers=users["admin"]["h"]).status_code == 404


def test_deleting_a_dataset_removes_the_file_empties_sessions_and_frees_quota(client):
    u = _user(client)
    r, sid = _upload(client, u)
    ds_id = r.json()["dataset_id"]
    with SessionLocal() as db:
        ref = db.get(PipelineSession, sid).db_path
    assert os.path.exists(ref)
    before = client.get("/api/sessions", headers=u["h"]).json()["quota"]["used"]
    res = client.delete(f"/api/datasets/{ds_id}", headers=u["h"])
    assert res.status_code == 200 and res.json()["sessions_cleared"] == 1
    assert not os.path.exists(ref)
    with SessionLocal() as db:
        row = db.get(PipelineSession, sid)
        assert row.db_path is None and row.dataset_id is None and row.dataset_bytes is None and row.schema is None
        assert db.get(Dataset, ds_id) is None
    after = client.get("/api/sessions", headers=u["h"]).json()["quota"]["used"]
    assert before["datasets"] == 1 and before["dataset_bytes"] > 0
    assert after["datasets"] == 0 and after["dataset_bytes"] == 0
    q = client.post("/api/execute-sql", headers=u["h"], json={"session_id": sid, "sql": "SELECT 1"})
    assert q.status_code in (400, 404, 410) and q.json()["success"] is False


def test_storage_failure_on_dataset_delete_changes_nothing(client, monkeypatch):
    import account_service
    u = _user(client)
    r, sid = _upload(client, u)
    ds_id = r.json()["dataset_id"]

    def boom(ref):
        raise OSError("disk says no")
    monkeypatch.setattr(account_service, "_delete_dataset_strict", boom)
    res = client.delete(f"/api/datasets/{ds_id}", headers=u["h"])
    assert res.status_code == 503 and res.json()["error_code"] == "DELETE_INCOMPLETE"
    with SessionLocal() as db:
        assert db.get(Dataset, ds_id) is not None and db.get(PipelineSession, sid).dataset_id == ds_id


# ---------------------------------------------------------------- invariants: datasets follow their creator

def test_leaving_or_removal_moves_datasets_to_the_creators_personal_workspace(client, team):
    ws_id, users = team
    r, _ = _upload(client, users["analyst"], workspace_id=ws_id)
    ds_id = r.json()["dataset_id"]
    url = f"/api/workspaces/{ws_id}/members/{users['analyst']['id']}"
    res = client.delete(url, headers=users["admin"]["h"])
    assert res.status_code == 200 and res.json()["datasets_rehomed"] == 1
    personal = _personal(client, users["analyst"])
    assert [d["id"] for d in _datasets(client, users["analyst"], personal).json()["datasets"]] == [ds_id]
    assert _datasets(client, users["owner"], ws_id).json()["datasets"] == []


def test_deleting_a_team_workspace_rehomes_datasets_and_erases_its_trail(client, team):
    ws_id, users = team
    r, _ = _upload(client, users["analyst"], workspace_id=ws_id)
    ds_id = r.json()["dataset_id"]
    res = client.delete(f"/api/workspaces/{ws_id}", headers=users["owner"]["h"])
    assert res.status_code == 200 and res.json()["datasets_rehomed"] == 1
    with SessionLocal() as db:
        assert db.get(Dataset, ds_id).workspace_id == _personal(client, users["analyst"])
        assert db.query(WorkspaceMember).filter(WorkspaceMember.workspace_id == ws_id).count() == 0
        assert db.query(AuditLog).filter(AuditLog.workspace_id == ws_id).count() == 0
    assert "workspace.delete" in _audit_actions(_personal(client, users["owner"]))


# ---------------------------------------------------------------- isolation

def test_users_never_see_each_others_workspaces_datasets_or_audit(client):
    a, b = _user(client, "a"), _user(client, "b")
    ra, _ = _upload(client, a, name="alpha.csv")
    pa, pb = _personal(client, a), _personal(client, b)
    ta = _team(client, a, "Alpha team")
    assert {w["id"] for w in client.get("/api/workspaces", headers=b["h"]).json()["workspaces"]} == {pb}
    for ws in (pa, ta):
        for path in ("", "/members", "/datasets", "/audit"):
            r = client.get(f"/api/workspaces/{ws}{path}", headers=b["h"])
            assert r.status_code == 404, (ws, path, r.text)
            assert "alpha" not in r.text.lower()
    assert client.delete(f"/api/datasets/{ra.json()['dataset_id']}", headers=b["h"]).status_code == 404
    r, _ = _upload(client, b, workspace_id=pa)
    assert r.status_code == 404


def test_cross_user_isolation_of_sessions_history_reports_jobs_and_retrieval(client):
    """Sprint 0 regression: nothing a user owns is reachable with another user's credentials."""
    a, b = _user(client, "a"), _user(client, "b")
    r, sid = _upload(client, a, name="alpha_payroll.csv", body=b"employee,alpha_salary\nx,1\ny,2\n")
    assert r.status_code == 200
    q = client.post("/api/execute-sql", headers=a["h"], json={"session_id": sid, "sql": "SELECT * FROM alpha_payroll", "question": "all"})
    assert q.status_code == 200, q.text
    hist_id = q.json()["history_id"]
    job = client._inner.post("/api/connect/upload", headers=a["h"], data={"session_id": sid},
                             files={"file": ("again.csv", io.BytesIO(b"k,v\n1,2\n"), "text/csv")}).json()["job_id"]
    with SessionLocal() as db:  # a report needs a completed pipeline run; the stored state is what a full run leaves
        row = db.get(PipelineSession, sid)
        update_session(db, row, question="all", sql="SELECT 1", sql_result={"sql": "SELECT 1", "explanation": "e"},
                       validation={"is_valid": True}, query_result={"status": "ok", "success": True, "row_count": 1,
                                                                    "columns": ["a"], "rows": [{"a": 1}]},
                       reliability={"reliability_score": 90, "reliability_level": "High"}, insights={"summary": "s"})
    rep = client.post("/api/generate-report", headers=a["h"], json={"session_id": sid})
    assert rep.status_code == 200, rep.text
    report_id = rep.json()["report_id"]

    assert client.get(f"/api/dataset/preview?session_id={sid}&table=alpha_payroll", headers=b["h"]).status_code == 404
    assert client.post("/api/retrieve-tables", headers=b["h"], json={"session_id": sid, "question": "salary"}).status_code == 404
    assert client.post("/api/execute-sql", headers=b["h"], json={"session_id": sid, "sql": "SELECT 1"}).status_code == 404
    assert client.delete(f"/api/session/{sid}", headers=b["h"]).status_code == 404
    assert client.get(f"/api/history/{hist_id}", headers=b["h"]).status_code == 404
    assert hist_id not in client.get("/api/history", headers=b["h"]).text
    assert client.get(f"/api/jobs/{job}", headers=b["h"]).status_code == 404
    assert client.get(f"/api/reports/{report_id}/download?kind=json", headers=b["h"]).status_code == 404
    assert sid not in client.get("/api/sessions", headers=b["h"]).text
    assert client.get(f"/api/answer-trace?session_id={sid}", headers=b["h"]).status_code == 404


# ---------------------------------------------------------------- audit trail

def test_actions_are_audited_without_sensitive_content(client, team):
    ws_id, users = team
    owner = users["owner"]
    pid = _personal(client, owner)
    assert "auth.login" in _audit_actions(pid)
    r, sid = _upload(client, users["analyst"], workspace_id=ws_id, name="secret_plans.csv",
                     body=b"customer_email,amount\nceo@corp.example,99\n")
    q = client.post("/api/execute-sql", headers=users["analyst"]["h"],
                    json={"session_id": sid, "sql": "SELECT customer_email FROM secret_plans", "question": "who is the ceo"})
    assert q.status_code == 200, q.text
    client.patch(f"/api/workspaces/{ws_id}/members/{users['viewer']['id']}", headers=owner["h"], json={"role": "analyst"})
    client.delete(f"/api/datasets/{r.json()['dataset_id']}", headers=owner["h"])
    actions = _audit_actions(ws_id)
    for expected in ("workspace.create", "member.add", "dataset.upload", "query.run", "member.role_change", "dataset.delete"):
        assert expected in actions, (expected, actions)
    trail = client.get(f"/api/workspaces/{ws_id}/audit?limit=100", headers=owner["h"]).json()["entries"]
    blob = json.dumps([e["details"] for e in trail])
    for secret in ("ceo@corp.example", "SELECT", "who is the ceo", "customer_email", "uploads/", "TestPassword"):
        assert secret not in blob, secret
    role_change = next(e for e in trail if e["action"] == "member.role_change")
    assert role_change["details"] == {"role": "analyst", "previous_role": "viewer"}
    assert role_change["actor_id"] == owner["id"] and role_change["target_id"] == users["viewer"]["id"]


def test_dataset_audit_entries_never_carry_the_file_name(client):
    u = _user(client)
    r, _ = _upload(client, u, name="jane_doe_medical_bills.csv")
    ds_id = r.json()["dataset_id"]
    assert client.delete(f"/api/datasets/{ds_id}", headers=u["h"]).status_code == 200
    pid = _personal(client, u)
    entries = [e for e in client.get(f"/api/workspaces/{pid}/audit", headers=u["h"]).json()["entries"]
               if e["action"].startswith("dataset.")]
    assert sorted(e["action"] for e in entries) == ["dataset.delete", "dataset.upload"]
    assert all(e["target_id"] == ds_id for e in entries)
    assert "jane_doe" not in json.dumps(entries)


def test_failed_session_update_leaves_no_unlinked_catalog_row(client, monkeypatch):
    import jobs
    from db import SessionConflictError

    def conflict(*args, **kwargs):
        raise SessionConflictError("Session was changed by another request")
    u = _user(client)
    sid = _session(client, u)
    monkeypatch.setattr(jobs, "update_session", conflict)
    r, _ = _upload(client, u, sid=sid)
    assert r.status_code == 422, r.text  # the job failed and says so
    with SessionLocal() as db:
        assert db.query(Dataset).filter(Dataset.created_by == u["id"]).count() == 0
        assert db.get(PipelineSession, sid).dataset_id is None
    monkeypatch.undo()
    r, _ = _upload(client, u, sid=sid)  # the session still works once the conflict is gone
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        assert db.query(Dataset).filter(Dataset.created_by == u["id"]).count() == 1


def test_audit_trail_paginates_newest_first(client):
    u = _user(client)
    ws_id = _team(client, u)
    for i in range(5):
        client.patch(f"/api/workspaces/{ws_id}", headers=u["h"], json={"name": f"Name {i}"})
    p1 = client.get(f"/api/workspaces/{ws_id}/audit?limit=3", headers=u["h"]).json()
    assert len(p1["entries"]) == 3 and p1["next_cursor"]
    p2 = client.get(f"/api/workspaces/{ws_id}/audit?limit=3&before={p1['next_cursor']}", headers=u["h"]).json()
    ids = [e["id"] for e in p1["entries"] + p2["entries"]]
    assert len(ids) == len(set(ids)) == 6 and p2["next_cursor"] is None
    assert p1["entries"][0]["details"]["name"] == "Name 4"
    other = _team(client, u, "Other")
    bad = client.get(f"/api/workspaces/{other}/audit?before={p1['next_cursor']}", headers=u["h"])
    assert bad.status_code == 400 and bad.json()["error_code"] == "INVALID_CURSOR"


def test_audit_log_is_append_only(client):
    import audit
    u = _user(client)
    pid = _personal(client, u)
    with SessionLocal() as db:
        row = db.query(AuditLog).filter(AuditLog.workspace_id == pid).first()
        row.action = "tampered"
        with pytest.raises(audit.AppendOnlyViolation):
            db.commit()
        db.rollback()
        row = db.query(AuditLog).filter(AuditLog.workspace_id == pid).first()
        db.delete(row)
        with pytest.raises(audit.AppendOnlyViolation):
            db.commit()


def test_audit_details_are_allow_listed_and_scrubbed():
    import audit
    cleaned = audit.clean_details({"name": "report for jane@corp.example", "sql": "SELECT 1", "question": "q",
                                   "role": "viewer", "bytes": 10, "nested": {"x": 1}})
    assert cleaned == {"name": "report for [email]", "role": "viewer", "bytes": 10}
    assert audit.clean_details({"sql": "SELECT 1"}) is None


def test_audit_failure_never_fails_the_audited_request(client, monkeypatch):
    import audit

    class Broken:
        def __call__(self):
            raise RuntimeError("audit db down")
    monkeypatch.setattr(audit, "SessionLocal", Broken())
    u = _user(client)  # login is audited
    r, _ = _upload(client, u)
    assert r.status_code == 200


# ---------------------------------------------------------------- account export / deletion

def test_export_includes_workspaces_and_datasets_without_storage_refs(client, team):
    ws_id, users = team
    _upload(client, users["analyst"], workspace_id=ws_id)
    data = client.get("/api/account/export", headers=users["analyst"]["h"]).json()
    kinds = sorted((w["kind"], w["role"]) for w in data["workspaces"])
    assert kinds == [("personal", "owner"), ("team", "analyst")]
    assert len(data["datasets"]) == 1 and "storage_ref" not in json.dumps(data["datasets"])


def test_deleting_an_owner_account_removes_their_workspaces_and_keeps_others_data(client, team):
    ws_id, users = team
    r, _ = _upload(client, users["analyst"], workspace_id=ws_id)
    ds_id = r.json()["dataset_id"]
    owner = users["owner"]
    res = client.post("/api/account/delete", headers=owner["h"],
                      json={"confirm_email": owner["email"], "password": "TestPassword123!"})
    assert res.status_code == 200, res.text
    with SessionLocal() as db:
        assert db.get(Workspace, ws_id) is None
        assert db.query(Workspace).filter(Workspace.owner_id == owner["id"]).count() == 0
        assert db.query(WorkspaceMember).filter(WorkspaceMember.user_id == owner["id"]).count() == 0
        moved = db.get(Dataset, ds_id)
        assert moved is not None and moved.workspace_id == _personal(client, users["analyst"])
    assert client.get(f"/api/workspaces/{ws_id}", headers=users["analyst"]["h"]).status_code == 404


def test_deleting_a_member_account_removes_their_datasets_from_the_team(client, team):
    ws_id, users = team
    _upload(client, users["analyst"], workspace_id=ws_id)
    a = users["analyst"]
    res = client.post("/api/account/delete", headers=a["h"], json={"confirm_email": a["email"], "password": "TestPassword123!"})
    assert res.status_code == 200, res.text
    assert _datasets(client, users["owner"], ws_id).json()["datasets"] == []
    emails = [m["email"] for m in client.get(f"/api/workspaces/{ws_id}/members", headers=users["owner"]["h"]).json()["members"]]
    assert a["email"] not in emails
