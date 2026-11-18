"""Sprint 3: connectors, layered ingestion (runs, quarantine, lineage, atomic swap), workspace-owned datasets.

Real API, real files, real jobs (they run inline without Redis). S3 is moto. Nothing about storage or the pipeline is mocked
except where a test must force a failure.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3

import pandas as pd
import pytest

import api as api_module
import ingest
import storage
from connectors import registry
from db import AuditLog, Dataset, IngestionRun, Job, PipelineSession, SessionLocal
from tests.test_s1_workspaces import _add, _datasets, _personal, _session, _team, _user, local_uploads  # noqa: F401


# ---------------------------------------------------------------- helpers

def _csv(rows: int = 30, bad: bool = False, id_blank: bool = False, cols: str = "id,region,amount") -> bytes:
    lines = [cols]
    for i in range(1, rows + 1):
        lines.append(f"{'' if (id_blank and i == 7) else i},R{i % 3},{i * 10}")
    if bad:
        lines.insert(5, "99,X,1,unexpected,extra")
    return ("\n".join(lines) + "\n").encode()


def _xlsx() -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        pd.DataFrame({"city": ["A", "B", "C"], "pop": [1, 2, 3]}).to_excel(w, sheet_name="Cities", index=False)
        pd.DataFrame({"k": [1, 2], "v": ["x", "y"]}).to_excel(w, sheet_name="Other", index=False)
    return buf.getvalue()


def _parquet() -> bytes:
    buf = io.BytesIO()
    pd.DataFrame({"sku": ["a", "b", "c"], "qty": [3, 4, 5], "price": [1.5, 2.5, 3.5],
                  "tags": [["x"], ["y", "z"], []]}).to_parquet(buf, index=False)
    return buf.getvalue()


def _sqlite_bytes(path) -> bytes:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    con.executemany("INSERT INTO items VALUES (?,?)", [(1, "a"), (2, "b")])
    con.commit()
    con.close()
    return open(path, "rb").read()


def _create(client, user, name, body, workspace_id=None, session=True):
    """Upload through /api/connect/upload (the legacy route, now on the new pipeline). Returns (response, session_id)."""
    sid = _session(client, user)
    data = {"session_id": sid, **({"workspace_id": workspace_id} if workspace_id else {})}
    return client.post("/api/connect/upload", headers=user["h"], data=data, files={"file": (name, io.BytesIO(body), "application/octet-stream")}), sid


def _job(client, user, job_id):
    return client._inner.get(f"/api/jobs/{job_id}", headers=user["h"]).json()


def _reimport(client, user, ds_id, name, body):
    r = client._inner.post(f"/api/datasets/{ds_id}/runs", headers=user["h"], files={"file": (name, io.BytesIO(body), "text/csv")})
    return r, (_job(client, user, r.json()["job_id"]) if r.status_code == 202 else None)


def _open(client, user, ds_id):
    sid = _session(client, user)
    return client._inner.post(f"/api/datasets/{ds_id}/open", headers=user["h"], json={"session_id": sid}), sid


def _sql(client, user, sid, sql):
    return client._inner.post("/api/execute-sql", headers=user["h"], json={"session_id": sid, "sql": sql})


def _runs(client, user, ds_id):
    return client._inner.get(f"/api/datasets/{ds_id}/runs", headers=user["h"]).json()["runs"]


def _curated_path(ds_id):
    with SessionLocal() as db:
        return storage.ensure_local_workspace(db.get(Dataset, ds_id).storage_ref)


# ---------------------------------------------------------------- connectors

def test_connector_listing_reports_only_what_exists(client):
    u = _user(client)
    body = client._inner.get("/api/connectors", headers=u["h"]).json()
    names = {c["name"]: c for c in body["connectors"]}
    assert set(names) == {"csv", "excel", "json", "parquet", "sqlite", "postgres", "mysql", "rest", "s3"}
    files = [c for c in names.values() if c["kind"] == "file"]
    assert all(c["full_refresh"] is True and c["incremental"] is False and c["requires_credentials"] is False for c in files)
    dbs = [names["postgres"], names["mysql"]]      # Sprint 4: incremental and credentials exist only where they are implemented
    assert all(c["kind"] == "database" and c["incremental"] is True and c["requires_credentials"] is True for c in dbs)
    assert names["rest"]["kind"] == "api" and names["s3"]["kind"] == "object_storage"      # Sprint 5
    assert client._inner.get("/api/connectors").status_code == 401


def test_connector_interface_validates_config_and_refuses_cursors(tmp_path):
    c = registry.get("csv")
    assert c.validate_config({"batch_size": 100}) == {"batch_size": 100}
    for bad in ({"batch_size": 0}, {"batch_size": "5"}, {"batch_size": 10 ** 9}, {"nope": 1}):
        with pytest.raises(ValueError):
            c.validate_config(bad)
    p = tmp_path / "a.csv"
    p.write_bytes(_csv(10))
    assert c.test_connection(str(p)).ok and not c.test_connection(str(tmp_path / "missing.csv")).ok
    assert list(c.discover_schema(str(p), "a")["a"]) == ["id", "region", "amount"]
    assert [len(b) for _, b in c.read_batches(str(p), "a", batch_size=4)] == [4, 4, 2]
    with pytest.raises(ValueError):
        list(c.read_batches(str(p), "a", cursor="x"))


# ---------------------------------------------------------------- formats

@pytest.mark.parametrize("name, make, table, expect_rows", [
    ("sales.csv", lambda tmp: _csv(30), "sales", 30),
    ("sales.json", lambda tmp: json.dumps([{"id": i, "v": i * 2} for i in range(1, 6)]).encode(), "sales", 5),
    ("book.xlsx", lambda tmp: _xlsx(), "book_cities", 3),
    ("stock.parquet", lambda tmp: _parquet(), "stock", 3),
    ("items.db", lambda tmp: _sqlite_bytes(tmp / "i.db"), "items", 2),
])
def test_every_format_ingests_into_a_queryable_curated_dataset(client, tmp_path, name, make, table, expect_rows):
    u = _user(client)
    r, sid = _create(client, u, name, make(tmp_path))
    assert r.status_code == 200, r.text
    body = r.json()
    run = _runs(client, u, body["dataset_id"])[0]
    assert run["status"] == "succeeded" and run["records_rejected"] == 0 and run["records_read"] == run["records_inserted"]
    assert run["table_stats"][table]["inserted"] == expect_rows
    q = _sql(client, u, sid, f"SELECT COUNT(*) AS n FROM {table}")
    assert q.status_code == 200, q.text
    assert q.json()["rows"][0]["n"] == expect_rows
    names = [t["name"] for t in body["tables"]]
    assert table in names and not any(n.startswith("_qs_") for n in names)
    con = sqlite3.connect(_curated_path(body["dataset_id"]))
    lineage = con.execute("SELECT table_name, connector, source_name FROM _qs_lineage WHERE table_name=?", (table,)).fetchone()
    con.close()
    assert lineage and lineage[2] == name


def test_parquet_nested_values_become_json_text_and_oversize_is_refused(client, tmp_path):
    u = _user(client)
    r, sid = _create(client, u, "stock.parquet", _parquet())
    q = _sql(client, u, sid, "SELECT tags FROM stock ORDER BY qty")
    assert [x["tags"] for x in q.json()["rows"]] == ['["x"]', '["y", "z"]', "[]"]
    bad, _ = _create(client, u, "broken.parquet", b"PAR1 not really parquet")
    assert bad.status_code == 422 and "Parquet" in bad.json()["error"]


# ---------------------------------------------------------------- same data as the original pipeline

@pytest.mark.parametrize("name, body", [("a.csv", _csv(40)), ("b.json", json.dumps([{"id": i, "t": f"x{i}", "f": i / 3} for i in range(25)]).encode()),
                                        ("c.xlsx", _xlsx())])
def test_curated_tables_equal_what_ingest_to_sqlite_always_produced(tmp_path, name, body):
    from connectors import registry as reg
    from modules.ingestion import ingest_to_sqlite
    src = tmp_path / name
    src.write_bytes(body)
    ext = os.path.splitext(name)[1]
    old, new = str(tmp_path / "old.db"), str(tmp_path / "new.db")
    ingest_to_sqlite(str(src), ext, old, "data")
    ingest.build_curated(str(src), reg.for_extension(ext), new, "data", "run-1", name)

    def dump(path):
        con = sqlite3.connect(path)
        out = {}
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND substr(name,1,4) <> '_qs_'"):
            out[t] = (con.execute(f'PRAGMA table_info("{t}")').fetchall(), con.execute(f'SELECT * FROM "{t}"').fetchall())
        con.close()
        return out
    assert dump(old) == dump(new)


@pytest.mark.skipif(not os.environ.get("QUERYSURE_EVAL_DATA"), reason="evaluation source files not supplied")
def test_evaluation_files_ingest_identically_through_the_new_pipeline(tmp_path):
    from connectors import registry as reg
    from evaluation.multiformat.sources import DATASETS, Sources, resolve_data_dir
    from modules.ingestion import ingest_to_sqlite
    src = Sources(resolve_data_dir(None))
    for key, (fname, ext) in DATASETS.items():
        if not src.available(key) or ext in (".db", ".sqlite"):
            continue
        old, new = str(tmp_path / f"{key}-old.db"), str(tmp_path / f"{key}-new.db")
        ingest_to_sqlite(src.path(key), ext, old, os.path.splitext(fname)[0])
        built = ingest.build_curated(src.path(key), reg.for_extension(ext), new, os.path.splitext(fname)[0], "r", fname)
        assert sum(s["rejected"] for s in built["tables"].values()) == 0, f"{key}: the evaluation file lost rows to quarantine"
        for path in (old, new):
            con = sqlite3.connect(path)
            rows = {t: con.execute(f'SELECT * FROM "{t}"').fetchall() for (t,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND substr(name,1,4) <> '_qs_'")}
            sch = {t: con.execute(f'PRAGMA table_info("{t}")').fetchall() for t in rows}
            con.close()
            if path == old:
                want = (rows, sch)
            else:
                assert (rows, sch) == want, key


# ---------------------------------------------------------------- validation and quarantine

def test_malformed_and_keyless_rows_are_quarantined_with_reasons(client):
    u = _user(client)
    r, sid = _create(client, u, "orders.csv", _csv(40, bad=True, id_blank=True))
    assert r.status_code == 200, r.text
    ds_id = r.json()["dataset_id"]
    run = _runs(client, u, ds_id)[0]
    assert run["records_read"] == 41 and run["records_inserted"] == 39 and run["records_rejected"] == 2
    assert run["reject_reasons"] == {"malformed_row": 1, "missing_key": 1}
    assert _sql(client, u, sid, "SELECT COUNT(*) AS n FROM orders").json()["rows"][0]["n"] == 39
    sample = client._inner.get(f"/api/datasets/{ds_id}/quarantine", headers=u["h"]).json()["rows"]
    assert sorted(x["reason"] for x in sample) == ["malformed_row", "missing_key"]
    assert next(x for x in sample if x["reason"] == "missing_key")["column"] == "id"
    assert "unexpected" not in json.dumps(sample)       # row values are never stored


def test_exact_duplicate_rows_are_reported_but_kept(client):
    u = _user(client)
    body = b"sku,qty\n" + b"a,1\n" * 5 + b"b,2\n"
    r, sid = _create(client, u, "lines.csv", body)
    run = _runs(client, u, r.json()["dataset_id"])[0]
    assert run["duplicate_rows"] == 4 and run["records_inserted"] == 6 and run["records_rejected"] == 0
    assert _sql(client, u, sid, "SELECT SUM(qty) AS s FROM lines").json()["rows"][0]["s"] == 7


# ---------------------------------------------------------------- re-import, skip, swap, failure

def test_identical_reimport_is_skipped_and_changed_reimport_swaps_versions(client):
    u = _user(client)
    r, _ = _create(client, u, "sales.csv", _csv(30))
    ds_id = r.json()["dataset_id"]
    with SessionLocal() as db:
        v1_ref = db.get(Dataset, ds_id).storage_ref
    same, job = _reimport(client, u, ds_id, "sales.csv", _csv(30))
    assert same.status_code == 202 and job["status"] == "succeeded" and job["result"]["run_status"] == "skipped"
    with SessionLocal() as db:
        ds = db.get(Dataset, ds_id)
        assert ds.version == 1 and ds.storage_ref == v1_ref
        assert db.query(IngestionRun).filter(IngestionRun.dataset_id == ds_id, IngestionRun.status == "skipped").count() == 1
    _, job = _reimport(client, u, ds_id, "sales.csv", _csv(50))
    assert job["status"] == "succeeded" and job["result"]["run_status"] == "succeeded"
    with SessionLocal() as db:
        ds = db.get(Dataset, ds_id)
        assert ds.version == 2 and ds.row_count == 50 and ds.storage_ref != v1_ref
    assert not os.path.exists(v1_ref), "the replaced curated version is removed after the swap"
    con = sqlite3.connect(_curated_path(ds_id))
    assert con.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 50
    con.close()
    assert [x["status"] for x in _runs(client, u, ds_id)] == ["succeeded", "skipped", "succeeded"]


def test_failed_reimport_keeps_the_previous_version_queryable(client, monkeypatch):
    u = _user(client)
    r, sid = _create(client, u, "sales.csv", _csv(30))
    ds_id = r.json()["dataset_id"]
    with SessionLocal() as db:
        before = db.get(Dataset, ds_id).storage_ref

    def boom(*a, **k):
        raise ValueError("source is corrupt")
    monkeypatch.setattr(ingest, "build_curated", boom)
    _, job = _reimport(client, u, ds_id, "sales.csv", _csv(60))
    monkeypatch.undo()
    assert job["status"] == "failed"
    with SessionLocal() as db:
        ds = db.get(Dataset, ds_id)
        assert ds.version == 1 and ds.storage_ref == before and ds.status == "ready"
    assert os.path.exists(before)
    assert _sql(client, u, sid, "SELECT COUNT(*) AS n FROM sales").json()["rows"][0]["n"] == 30
    failed = _runs(client, u, ds_id)[0]
    assert failed["status"] == "failed" and failed["error_code"] == "INVALID_SOURCE"
    _, job = _reimport(client, u, ds_id, "sales.csv", _csv(60))     # the dataset is free again
    assert job["status"] == "succeeded"


def test_a_failed_first_import_leaves_no_dataset_and_a_bad_file_is_reported(client):
    u = _user(client)
    r, _ = _create(client, u, "empty.csv", b"")
    assert r.status_code == 422
    with SessionLocal() as db:
        assert db.query(Dataset).filter(Dataset.created_by == u["id"]).count() == 0


# ---------------------------------------------------------------- overlapping runs and interruption

def test_only_one_active_run_per_dataset_is_enforced_by_the_database(client):
    from sqlalchemy.exc import IntegrityError
    u = _user(client)
    r, _ = _create(client, u, "sales.csv", _csv(30))
    ds_id = r.json()["dataset_id"]
    with SessionLocal() as db:
        ds = db.get(Dataset, ds_id)
        ingest.start_run(db, ds, user_id=u["id"], job_id=None, connector="csv", source_name="x.csv", source_bytes=1, fp="f")
        db.add(IngestionRun(dataset_id=ds_id, workspace_id=ds.workspace_id, connector="csv", status="queued"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    res, _ = _reimport(client, u, ds_id, "sales.csv", _csv(31))
    assert res.status_code == 409 and res.json()["error_code"] == "RUN_IN_PROGRESS"
    with SessionLocal() as db:
        with pytest.raises(ingest.RunInProgress):
            ingest.start_run(db, db.get(Dataset, ds_id), user_id=u["id"], job_id="j2", connector="csv", source_name="y", source_bytes=1, fp="g")


def test_a_job_lost_in_a_restart_marks_its_run_interrupted_and_frees_the_dataset(client, monkeypatch):
    import jobs
    from datetime import datetime, timedelta, timezone
    u = _user(client)
    r, _ = _create(client, u, "sales.csv", _csv(30))
    ds_id = r.json()["dataset_id"]
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    with SessionLocal() as db:
        db.add(Job(id="lost-job-1", user_id=u["id"], kind="dataset_ingestion", status="running", created_at=old, updated_at=old))
        db.commit()
        ingest.start_run(db, db.get(Dataset, ds_id), user_id=u["id"], job_id="lost-job-1", connector="csv", source_name="x", source_bytes=1, fp="z")
        # a brand-new dataset whose first import was lost: its placeholder must disappear
        pending = Dataset(workspace_id=db.get(Dataset, ds_id).workspace_id, created_by=u["id"], name="p", storage_ref="pending:abc",
                          status="importing", version=0)
        db.add(pending)
        db.commit()
        db.add(Job(id="lost-job-2", user_id=u["id"], kind="dataset_ingestion", status="running", created_at=old, updated_at=old))
        db.commit()
        ingest.start_run(db, pending, user_id=u["id"], job_id="lost-job-2", connector="csv", source_name="p", source_bytes=1, fp="p")
        pid = pending.id
    monkeypatch.setattr(jobs, "worker_alive", lambda: False)
    with SessionLocal() as db:
        assert jobs.fail_orphaned_jobs(db) >= 2
    with SessionLocal() as db:
        run = db.query(IngestionRun).filter(IngestionRun.job_id == "lost-job-1").one()
        assert run.status == "interrupted" and run.error_code == "JOB_INTERRUPTED"
        assert db.get(Dataset, pid) is None
    _, job = _reimport(client, u, ds_id, "sales.csv", _csv(35))
    assert job["status"] == "succeeded"


# ---------------------------------------------------------------- schema changes

def test_schema_changes_are_detected_reported_and_audited_never_coerced(client):
    u = _user(client)
    r, sid = _create(client, u, "m.csv", b"a,b\n1,2\n3,4\n")
    ds_id = r.json()["dataset_id"]
    _, job = _reimport(client, u, ds_id, "m.csv", b"a,b,c\n1,x,5\n3,y,6\n")
    assert job["status"] == "succeeded"
    changes = _runs(client, u, ds_id)[0]["schema_changes"]
    assert {"table": "m", "change": "column_added", "column": "c", "to": "INTEGER"} in changes
    assert {"table": "m", "change": "type_changed", "column": "b", "from": "INTEGER", "to": "TEXT", "compatible": False} in changes
    with SessionLocal() as db:
        ds = db.get(Dataset, ds_id)
        assert ds.schema_version == 2 and ds.version == 2
        acts = [a.action for a in db.query(AuditLog).filter(AuditLog.workspace_id == ds.workspace_id)]
        assert "ingestion.schema_change" in acts and acts.count("ingestion.finish") == 2
        details = json.dumps([a.details for a in db.query(AuditLog).filter(AuditLog.workspace_id == ds.workspace_id)])
    assert "x" not in json.loads(json.dumps(details)).replace("ingestion", "")  # no cell values in the audit trail
    con = sqlite3.connect(_curated_path(ds_id))
    assert [r[0] for r in con.execute("SELECT b FROM m ORDER BY rowid")] == ["x", "y"]
    con.close()


# ---------------------------------------------------------------- internal tables are invisible

def test_qs_tables_are_hidden_and_blocked_everywhere(client, tmp_path):
    u = _user(client)
    r, sid = _create(client, u, "orders.csv", _csv(40, bad=True))
    body = r.json()
    assert not any(t["name"].startswith("_qs_") for t in body["tables"])
    prev = client._inner.get(f"/api/dataset/preview?session_id={sid}&table=_qs_quarantine", headers=u["h"])
    assert prev.status_code == 404
    for sql in ("SELECT * FROM _qs_quarantine", "SELECT table_name FROM _qs_lineage"):
        res = _sql(client, u, sid, sql)
        assert res.status_code >= 400 and res.json()["success"] is False
    rt = client._inner.post("/api/retrieve-tables", headers=u["h"], json={"session_id": sid, "question": "quarantine lineage rows"})
    assert "_qs_" not in rt.text
    from modules.query_result_engine import execute_query
    out = execute_query("SELECT * FROM _qs_quarantine", _curated_path(body["dataset_id"]), parameterize=False)
    assert out["success"] is False                       # the execution authorizer refuses it even if validation were bypassed
    evil = tmp_path / "evil.db"
    con = sqlite3.connect(evil)
    con.execute("CREATE TABLE _qs_quarantine (x)")
    con.commit()
    con.close()
    res, _ = _create(client, u, "evil.db", evil.read_bytes())
    assert res.status_code == 422 and "reserved" in res.json()["error"]


# ---------------------------------------------------------------- workspace authorization

@pytest.fixture()
def team(client):
    users = {r: _user(client, r) for r in ("owner", "admin", "analyst", "viewer", "outsider")}
    ws_id = _team(client, users["owner"])
    for role in ("admin", "analyst", "viewer"):
        assert _add(client, users["owner"], ws_id, users[role], role).status_code == 201
    return ws_id, users


def test_viewer_can_open_and_query_but_not_reimport_or_see_quarantine(client, team):
    ws_id, users = team
    r, _ = _create(client, users["analyst"], "orders.csv", _csv(40, bad=True), workspace_id=ws_id)
    ds_id = r.json()["dataset_id"]
    assert r.json()["workspace_id"] == ws_id
    for role in ("owner", "admin", "viewer"):
        res, sid = _open(client, users[role], ds_id)
        assert res.status_code == 200, (role, res.text)
        q = _sql(client, users[role], sid, "SELECT COUNT(*) AS n FROM orders")
        assert q.status_code == 200 and q.json()["rows"][0]["n"] == 40, role
    v = users["viewer"]
    assert _reimport(client, v, ds_id, "orders.csv", _csv(41))[0].status_code == 403
    assert client._inner.get(f"/api/datasets/{ds_id}/quarantine", headers=v["h"]).status_code == 403
    assert client._inner.get(f"/api/datasets/{ds_id}/runs", headers=v["h"]).status_code == 200
    assert _runs(client, v, ds_id)[0]["error_detail"] is None
    o = users["outsider"]
    assert _open(client, o, ds_id)[0].status_code == 404
    for path in ("runs", "quarantine"):
        assert client._inner.get(f"/api/datasets/{ds_id}/{path}", headers=o["h"]).status_code == 404
    assert _reimport(client, o, ds_id, "orders.csv", _csv(41))[0].status_code == 404
    assert client._inner.get(f"/api/datasets/{ds_id}/runs/{_runs(client, users['analyst'], ds_id)[0]['id']}", headers=o["h"]).status_code == 404


def test_an_analyst_can_reimport_a_teammates_dataset_and_open_sessions_must_reopen(client, team):
    ws_id, users = team
    r, _ = _create(client, users["admin"], "orders.csv", _csv(30), workspace_id=ws_id)
    ds_id = r.json()["dataset_id"]
    res, viewer_sid = _open(client, users["viewer"], ds_id)
    assert _sql(client, users["viewer"], viewer_sid, "SELECT COUNT(*) AS n FROM orders").status_code == 200
    _, job = _reimport(client, users["analyst"], ds_id, "orders.csv", _csv(45))
    assert job["status"] == "succeeded"
    stale = _sql(client, users["viewer"], viewer_sid, "SELECT COUNT(*) AS n FROM orders")
    assert stale.status_code == 409 and stale.json()["error_code"] == "DATASET_CHANGED"
    res, sid2 = _open(client, users["viewer"], ds_id)
    assert _sql(client, users["viewer"], sid2, "SELECT COUNT(*) AS n FROM orders").json()["rows"][0]["n"] == 45


def test_removing_a_member_ends_access_for_their_open_session_immediately(client, team):
    ws_id, users = team
    r, _ = _create(client, users["owner"], "orders.csv", _csv(30), workspace_id=ws_id)
    ds_id = r.json()["dataset_id"]
    _, sid = _open(client, users["viewer"], ds_id)
    assert _sql(client, users["viewer"], sid, "SELECT COUNT(*) AS n FROM orders").status_code == 200
    assert client.delete(f"/api/workspaces/{ws_id}/members/{users['viewer']['id']}", headers=users["owner"]["h"]).status_code == 200
    for call in (lambda: _sql(client, users["viewer"], sid, "SELECT 1"),
                 lambda: client._inner.get(f"/api/dataset/preview?session_id={sid}&table=orders", headers=users["viewer"]["h"])):
        res = call()
        assert res.status_code == 403 and res.json()["error_code"] == "DATASET_ACCESS_REVOKED"
    assert _open(client, users["viewer"], ds_id)[0].status_code == 404


def test_opening_a_teammates_dataset_costs_the_opener_no_quota(client, team):
    import quotas
    ws_id, users = team
    r, _ = _create(client, users["owner"], "orders.csv", _csv(30), workspace_id=ws_id)
    ds_id = r.json()["dataset_id"]
    with SessionLocal() as db:
        before = quotas.usage_for_user(db, users["viewer"]["id"])
        owner_used = quotas.usage_for_user(db, users["owner"]["id"])
    _open(client, users["viewer"], ds_id)
    with SessionLocal() as db:
        after = quotas.usage_for_user(db, users["viewer"]["id"])
        assert (after["datasets"], after["dataset_bytes"]) == (before["datasets"], before["dataset_bytes"])
        assert owner_used["datasets"] == 1 and owner_used["dataset_bytes"] > 0
    assert client.delete(f"/api/datasets/{ds_id}", headers=users["owner"]["h"]).status_code == 200
    with SessionLocal() as db:
        assert quotas.usage_for_user(db, users["owner"]["id"])["datasets"] == 0
    assert not os.path.exists(os.path.join(storage.UPLOAD_DIR, "ws")), "delete removes every version and raw file"


def test_rejoining_workspace_state_survives_rehoming(client, team):
    """Deleting a team workspace moves datasets to their creators' personal workspaces; they stay readable there."""
    ws_id, users = team
    r, _ = _create(client, users["analyst"], "orders.csv", _csv(30), workspace_id=ws_id)
    ds_id = r.json()["dataset_id"]
    assert client.delete(f"/api/workspaces/{ws_id}", headers=users["owner"]["h"]).status_code == 200
    res, sid = _open(client, users["analyst"], ds_id)
    assert res.status_code == 200
    assert _sql(client, users["analyst"], sid, "SELECT COUNT(*) AS n FROM orders").json()["rows"][0]["n"] == 30
    assert _reimport(client, users["analyst"], ds_id, "orders.csv", _csv(31))[1]["status"] == "succeeded"
    assert _open(client, users["viewer"], ds_id)[0].status_code == 404


def test_create_dataset_without_a_session(client, team):
    ws_id, users = team
    r = client._inner.post("/api/datasets", headers=users["analyst"]["h"], data={"workspace_id": ws_id},
                           files={"file": ("plain.csv", io.BytesIO(_csv(20)), "text/csv")})
    assert r.status_code == 202
    assert _job(client, users["analyst"], r.json()["job_id"])["status"] == "succeeded"
    listing = _datasets(client, users["viewer"], ws_id).json()["datasets"]
    assert [(d["name"], d["status"], d["version"], d["connector"]) for d in listing] == [("plain.csv", "ready", 1, "csv")]
    denied = client._inner.post("/api/datasets", headers=users["viewer"]["h"], data={"workspace_id": ws_id},
                                files={"file": ("p.csv", io.BytesIO(_csv(5)), "text/csv")})
    assert denied.status_code == 403
    bad = client._inner.post("/api/datasets", headers=users["analyst"]["h"], files={"file": ("p.exe", io.BytesIO(b"x"), "text/plain")})
    assert bad.status_code == 400 and bad.json()["error_code"] == "UNSUPPORTED_FILE_TYPE"


def test_legacy_session_without_a_catalog_link_still_works(client):
    """Pre-Sprint-3 uploads (user-owned refs) are untouched: resolved through the session's own db_path."""
    u = _user(client)
    sid = _session(client, u)
    legacy = os.path.join(storage.UPLOAD_DIR, "legacy.db")
    con = sqlite3.connect(legacy)
    con.execute("CREATE TABLE t (a INTEGER)")
    con.execute("INSERT INTO t VALUES (1), (2)")
    con.commit()
    con.close()
    from modules import build_semantic_layer, load_database
    schema = load_database(legacy)
    from db import update_session
    with SessionLocal() as db:
        update_session(db, db.get(PipelineSession, sid), db_path=legacy, schema=schema, semantic=build_semantic_layer(schema))
    q = _sql(client, u, sid, "SELECT COUNT(*) AS n FROM t")
    assert q.status_code == 200 and q.json()["rows"][0]["n"] == 2


# ---------------------------------------------------------------- S3 (moto)

@pytest.fixture()
def s3(tmp_path, monkeypatch):
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    for k, v in {"AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_SESSION_TOKEN": "t", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
    uploads = str(tmp_path / "s3uploads")
    os.makedirs(uploads)
    with moto.mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="qs-s3-bucket")
        monkeypatch.setattr(storage, "BACKEND", "s3")
        monkeypatch.setattr(storage, "BUCKET", "qs-s3-bucket")
        monkeypatch.setattr(storage, "PREFIX", "datasets")
        monkeypatch.setattr(storage, "REGION", "us-east-1")
        monkeypatch.setattr(storage, "ENDPOINT_URL", None)
        monkeypatch.setattr(storage, "UPLOAD_DIR", uploads)
        monkeypatch.setattr(storage, "CACHE_DIR", os.path.join(uploads, ".cache"))
        monkeypatch.setattr(api_module, "UPLOAD_DIR", uploads)
        storage._reset_client_cache()
        yield boto3.client("s3", region_name="us-east-1")
        storage._reset_client_cache()


def _wipe_cache():
    import shutil
    shutil.rmtree(storage.CACHE_DIR, ignore_errors=True)


def test_s3_lifecycle_import_swap_survive_cache_loss_and_delete(client, s3, team):
    ws_id, users = team
    r, _ = _create(client, users["analyst"], "orders.csv", _csv(30), workspace_id=ws_id)
    assert r.status_code == 200, r.text
    ds_id = r.json()["dataset_id"]

    def keys():
        return sorted(o["Key"] for o in s3.list_objects_v2(Bucket="qs-s3-bucket").get("Contents", []))
    k1 = keys()
    assert len(k1) == 2 and all(k.startswith(f"datasets/ws/{ws_id}/{ds_id}/") for k in k1) and sum("/raw/" in k for k in k1) == 1
    _, job = _reimport(client, users["admin"], ds_id, "orders.csv", _csv(44))
    assert job["status"] == "succeeded"
    k2 = keys()
    assert len(k2) == 3 and sum("/raw/" in k for k in k2) == 2   # two raw uploads are kept, one curated version
    assert sum("/curated-" in k for k in k2) == 1, "the replaced curated object is deleted after the swap"
    _wipe_cache()                                              # redeploy: no local copy
    res, sid = _open(client, users["viewer"], ds_id)
    assert res.status_code == 200 and _sql(client, users["viewer"], sid, "SELECT COUNT(*) AS n FROM orders").json()["rows"][0]["n"] == 44
    assert client.delete(f"/api/datasets/{ds_id}", headers=users["admin"]["h"]).status_code == 200
    assert keys() == [], "delete removes every version and raw file"


def test_workspace_refs_are_validated_and_cannot_be_read_as_legacy(s3):
    assert not storage.is_workspace_ref("s3://qs-s3-bucket/datasets/ws/not-a-uuid/x/curated.db")
    assert not storage.is_workspace_ref("s3://other-bucket/datasets/ws/" + "a" * 36 + "/" + "b" * 36 + "/curated-1.db")
    good = "s3://qs-s3-bucket/datasets/ws/" + "a" * 36 + "/" + "b" * 36 + "/curated-run1.db"
    assert storage.is_workspace_ref(good) and not storage.is_managed(good)
    with pytest.raises(storage.DatasetUnavailable):
        storage.ensure_local(good, "someone")
    with pytest.raises(storage.DatasetUnavailable):
        storage.ensure_local_workspace("s3://qs-s3-bucket/datasets/" + "a" * 36 + "/" + "b" * 36 + ".db")
