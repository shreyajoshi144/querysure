"""Sprint 12: launch hardening gates -- every API route is authenticated unless it is on a reviewed public list, and the
workspace overview dashboard is real, scoped and role-aware."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

import pytest

# Routes that are public ON PURPOSE (sign-in flow, health, static product facts, provider-signed webhook). Adding to this list is a
# security decision: it must be reviewed.
PUBLIC = {
    ("GET", "/api/auth/google/config"), ("GET", "/api/evaluation/product-summary"), ("GET", "/api/product-info"),
    ("GET", "/api/health"), ("GET", "/api/ready"), ("POST", "/api/auth/register"), ("POST", "/api/auth/verify-email"),
    ("POST", "/api/auth/resend-verification"), ("POST", "/api/auth/login"), ("POST", "/api/auth/forgot-password"),
    ("POST", "/api/auth/reset-password"), ("POST", "/api/auth/google"), ("POST", "/api/billing/webhook/{provider_name}"),
}


def _routes():
    import api
    from tests.route_utils import iter_api_routes
    return sorted({(m, path) for r, path, _ in iter_api_routes(api.app.routes) for m in r.methods if m not in ("HEAD", "OPTIONS") and path.startswith("/api")})


def test_every_non_public_api_route_rejects_anonymous_requests(client):
    open_routes, checked = [], 0
    for method, path in _routes():
        if (method, path) in PUBLIC:
            continue
        url = re.sub(r"\{[^}]+\}", lambda m: "1" if "step_id" in m.group(0) else str(uuid.uuid4()), path)
        resp = client._inner.request(method, url)
        checked += 1
        if resp.status_code not in (401, 403):
            open_routes.append((method, path, resp.status_code))
    assert checked >= 90, checked
    assert open_routes == [], f"routes reachable without authentication: {open_routes}"


def test_the_public_list_has_no_stale_entries():
    assert PUBLIC <= set(_routes()), sorted(PUBLIC - set(_routes()))


# ---------------------------------------------------------------- workspace overview
def test_overview_summarises_the_workspace_and_respects_roles(client, monkeypatch):
    import json
    from db import AgentRun, AgentStep, DatasetSchedule, SessionLocal
    from tests.test_s1_workspaces import _add, _team, _user, local_uploads  # noqa: F401
    from tests.test_s3_ingestion import _create, _datasets
    from tests.test_s7_knowledge import POLICY, FakeModel, _put, only_groq  # noqa: F401
    from tests.test_s9_agent import _plan, _run
    owner, analyst, viewer, outsider = (_user(client, n) for n in ("ow", "an", "vi", "ou"))
    ws = _team(client, owner)
    _add(client, owner, ws, analyst, "analyst")
    _add(client, owner, ws, viewer, "viewer")
    empty = client._inner.get(f"/api/workspaces/{ws}/overview", headers=owner["h"]).json()["overview"]
    assert empty["datasets"]["total"] == 0 and empty["knowledge"] == {"documents": 0, "passages": 0} and empty["attention_count"] == 0

    r, _ = _create(client, owner, "sales.csv", b"region,amount\nN,1\nS,2\nE,3\n", workspace_id=ws)
    assert r.status_code in (200, 202)
    assert _put(client, owner, ws, "policy.md", POLICY).status_code == 201
    ds = _datasets(client, owner, ws).json()["datasets"][0]
    FakeModel(monkeypatch, lambda s, u: json.dumps({"steps": [{"tool": "delete_document", "args": {"document_id": next(iter(
        d["id"] for d in client._inner.get(f"/api/workspaces/{ws}/knowledge", headers=owner["h"]).json()["documents"]))}, "why": "x"}]}))
    run = _run(client, analyst, ws, "remove the policy").json()["run"]          # waits for an admin to approve
    assert run["status"] == "awaiting_approval"
    FakeModel(monkeypatch, lambda s, u: _plan(("list_datasets", {})))
    assert _run(client, owner, ws, "list").status_code == 201
    with SessionLocal() as db:
        db.add(DatasetSchedule(dataset_id=ds["id"], interval_minutes=60, mode="incremental", enabled=True, created_by=owner["id"],
                               next_run_at=datetime.now(timezone.utc), last_status="failed"))
        db.commit()

    o = client._inner.get(f"/api/workspaces/{ws}/overview", headers=owner["h"]).json()["overview"]
    assert o["datasets"]["total"] == 1 and o["datasets"]["by_status"] == {"ready": 1} and o["datasets"]["by_quality"]["ok"] == 1
    assert o["ingestion"]["recent_runs"] and o["ingestion"]["recent_runs"][0]["dataset"] == ds["name"]
    assert o["ingestion"]["schedules"] == {"enabled": 1, "disabled": 0, "last_failed": 1}
    assert o["knowledge"]["documents"] == 1 and o["knowledge"]["passages"] >= 1
    assert [p["tool"] for p in o["agent"]["awaiting_approval"]] == ["delete_document"]            # admin sees the analyst's pending approval
    assert o["ai"]["operations"] >= 2 and o["ai"]["failures"] == 0 and o["attention_count"] == 2   # one approval + one failed schedule
    mine = client._inner.get(f"/api/workspaces/{ws}/overview", headers=analyst["h"]).json()["overview"]
    assert len(mine["agent"]["awaiting_approval"]) == 1 and mine["ai"] is None                    # own run only; no AI usage for non-admins
    other = client._inner.get(f"/api/workspaces/{ws}/overview", headers=viewer["h"]).json()["overview"]
    assert other["agent"]["awaiting_approval"] == [] and other["ai"] is None and other["workspace"]["role"] == "viewer"
    assert client._inner.get(f"/api/workspaces/{ws}/overview", headers=outsider["h"]).status_code == 404
    assert client._inner.get(f"/api/workspaces/{ws}/overview").status_code == 401
    with SessionLocal() as db:       # the schedule belongs to no real connection: remove it so a later scheduler tick in the shared test DB never sees it
        db.query(DatasetSchedule).filter(DatasetSchedule.dataset_id == ds["id"]).delete()
        db.commit()


def test_index_and_table_size_limits_protect_latency(client, monkeypatch):
    import config
    from tests.test_s1_workspaces import _team, _user, local_uploads  # noqa: F401
    from tests.test_s3_ingestion import _create, _datasets
    from tests.test_s7_knowledge import POLICY, _put
    owner = _user(client, "lim")
    ws = _team(client, owner)
    monkeypatch.setattr(config, "KNOWLEDGE_SEARCH_MAX_CHUNKS", 3)
    assert _put(client, owner, ws, "policy.md", POLICY).status_code == 201                       # 2-3 passages fit
    r = _put(client, owner, ws, "more.md", b"# Extra\n\n" + b"Different words entirely about gardening tools and soil. " * 60)
    assert r.status_code == 409 and r.json()["error_code"] == "INDEX_FULL", r.text                # a workspace can never outgrow what search scores
    r, _ = _create(client, owner, "sales.csv", b"order_date,region,amount\n2025-01-01,N,1\n2025-02-01,S,2\n2025-03-01,N,3\n", workspace_id=ws)
    ds = _datasets(client, owner, ws).json()["datasets"][0]["id"]
    monkeypatch.setattr(config, "INVESTIGATION_MAX_TABLE_ROWS", 2)
    big = client._inner.post(f"/api/datasets/{ds}/investigations", headers=owner["h"],
                             json={"kind": "trend", "table": "sales", "measure": "amount", "date_column": "order_date"})
    assert big.status_code == 422 and big.json()["error_code"] == "TABLE_TOO_LARGE", big.text
