"""Sprint 4: encrypted connections, PostgreSQL / MySQL connectors, batch and incremental ingestion.

Engine tests run against real servers named by TEST_SOURCE_POSTGRES_URL and TEST_SOURCE_MYSQL_URL (each is skipped when unset;
CI provides both). Everything else (secrets, network guard, API authorization) needs no server.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pytest

import config
import ingest
import secrets_store
from connectors import netguard
from connectors.database import ConnectorError
from db import AuditLog, Connection, Dataset, IngestionCheckpoint, IngestionRun, Job, SessionLocal
from modules.ingestion import safe_table_name
from tests.test_s1_workspaces import _add, _team, _user, local_uploads  # noqa: F401
from tests.test_s3_ingestion import _curated_path, _open, _runs, _sql

PW = "S3cret-pass-word-123"


# ---------------------------------------------------------------- secrets and network guard (no server)

def test_secrets_are_encrypted_bound_to_their_connection_and_rotatable(monkeypatch):
    token = secrets_store.encrypt("conn-1", {"password": PW})
    assert PW not in token and secrets_store.decrypt("conn-1", token) == {"password": PW}
    with pytest.raises(secrets_store.SecretsUnavailable):
        secrets_store.decrypt("conn-2", token)                      # a blob cannot be moved to another connection
    from cryptography.fernet import Fernet
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setattr(config, "CONNECTIONS_ENCRYPTION_KEYS", [old])
    t_old = secrets_store.encrypt("c", {"password": PW})
    monkeypatch.setattr(config, "CONNECTIONS_ENCRYPTION_KEYS", [new, old])   # rotation: new encrypts, both decrypt
    assert secrets_store.decrypt("c", t_old) == {"password": PW}
    assert secrets_store.decrypt("c", secrets_store.encrypt("c", {"password": "x"})) == {"password": "x"}
    monkeypatch.setattr(config, "CONNECTIONS_ENCRYPTION_KEYS", [new])
    with pytest.raises(secrets_store.SecretsUnavailable):
        secrets_store.decrypt("c", t_old)                           # old key rotated out


def test_secrets_need_a_key_in_production_only(monkeypatch):
    monkeypatch.setattr(config, "CONNECTIONS_ENCRYPTION_KEYS", [])
    assert secrets_store.configured() is True                      # development derives one
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    assert secrets_store.configured() is False
    with pytest.raises(secrets_store.SecretsUnavailable):
        secrets_store.encrypt("c", {"password": PW})


@pytest.mark.parametrize("host", ["127.0.0.1", "10.1.2.3", "192.168.0.9", "172.16.5.5", "169.254.169.254", "0.0.0.0",
                                  "100.64.0.1", "::1", "fd00::1", "::ffff:127.0.0.1"])
def test_private_and_internal_hosts_are_refused_unless_allowed(monkeypatch, host):
    monkeypatch.setattr(config, "CONNECTOR_ALLOW_PRIVATE_HOSTS", False)
    with pytest.raises(netguard.HostNotAllowed):
        netguard.resolve_allowed(host, 5432)
    monkeypatch.setattr(config, "CONNECTOR_ALLOW_PRIVATE_HOSTS", True)
    assert netguard.resolve_allowed(host, 5432)


def test_public_addresses_pass_and_bad_host_syntax_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "CONNECTOR_ALLOW_PRIVATE_HOSTS", False)
    assert netguard.resolve_allowed("8.8.8.8", 5432) == ["8.8.8.8"]
    for bad in ("", "http://db.example.com", "db.example.com/x", "a b", "user@host", "x" * 300):
        with pytest.raises(ValueError):
            netguard.validate_host(bad)
    for bad in (0, 70000, "5432", True):
        with pytest.raises(ValueError):
            netguard.validate_port(bad)


# ---------------------------------------------------------------- API: lifecycle, authorization, secrecy (no server)

def _cfg(**over):
    return {"host": "127.0.0.1", "port": 5432, "database": "d", "user": "u", "ssl_mode": "require", **over}


def _mk(client, user, ws, name="Warehouse", connector="postgres", password=PW, **cfg):
    return client._inner.post("/api/connections", headers=user["h"], json={
        "workspace_id": ws, "name": name, "connector": connector, "config": _cfg(**cfg), "password": password})


@pytest.fixture()
def team(client):
    users = {r: _user(client, r) for r in ("owner", "admin", "analyst", "viewer", "outsider")}
    ws_id = _team(client, users["owner"])
    for role in ("admin", "analyst", "viewer"):
        assert _add(client, users["owner"], ws_id, users[role], role).status_code == 201
    return ws_id, users


def test_connection_lifecycle_and_the_password_is_never_exposed(client, team, caplog):
    ws_id, users = team
    r = _mk(client, users["admin"], ws_id)
    assert r.status_code == 201, r.text
    conn = r.json()["connection"]
    assert conn["has_password"] is True and conn["host"] == "127.0.0.1" and PW not in r.text
    cid = conn["id"]
    with SessionLocal() as db:
        row = db.get(Connection, cid)
        assert PW not in row.secret_ciphertext and PW not in json.dumps(row.config)
    seen = [client._inner.get(f"/api/connections/{cid}", headers=users["analyst"]["h"]).text,
            client._inner.get(f"/api/connections?workspace_id={ws_id}", headers=users["analyst"]["h"]).text]
    assert all(PW not in t and "secret_ciphertext" not in t for t in seen)
    assert _mk(client, users["admin"], ws_id).status_code == 409                       # names are unique per workspace
    assert _mk(client, users["admin"], ws_id, name="x", connector="oracle").status_code == 400
    assert _mk(client, users["admin"], ws_id, name="y", host="not a host").status_code == 422
    assert _mk(client, users["admin"], ws_id, name="z", ssl_mode="plain").status_code == 422
    # editing: a new password or name is fine; moving the host/port/user without re-entering the password is not
    patch = lambda **b: client._inner.patch(f"/api/connections/{cid}", headers=users["admin"]["h"], json=b)  # noqa: E731
    assert patch(name="Warehouse 2").status_code == 200
    moved = patch(config=_cfg(host="evil.example.com"))
    assert moved.status_code == 422 and moved.json()["error_code"] == "PASSWORD_REQUIRED"
    with SessionLocal() as db:
        before = db.get(Connection, cid).secret_ciphertext
    assert patch(config=_cfg(host="db2.example.com"), password="N3w-pass-word").status_code == 200
    with SessionLocal() as db:
        row = db.get(Connection, cid)
        assert row.secret_ciphertext != before and secrets_store.decrypt(cid, row.secret_ciphertext) == {"password": "N3w-pass-word"}
    assert client._inner.post(f"/api/connections/{cid}/disable", headers=users["admin"]["h"]).json()["connection"]["status"] == "disabled"
    assert client._inner.post(f"/api/connections/{cid}/test", headers=users["analyst"]["h"]).status_code == 409
    assert client._inner.post(f"/api/connections/{cid}/enable", headers=users["admin"]["h"]).status_code == 200
    assert client._inner.delete(f"/api/connections/{cid}", headers=users["admin"]["h"]).json()["deleted"] is True
    assert client._inner.get(f"/api/connections/{cid}", headers=users["admin"]["h"]).status_code == 404
    with SessionLocal() as db:
        acts = [a.action for a in db.query(AuditLog).filter(AuditLog.workspace_id == ws_id)]
        blob = json.dumps([a.details for a in db.query(AuditLog).filter(AuditLog.workspace_id == ws_id)])
    assert {"connection.create", "connection.update", "connection.disable", "connection.enable", "connection.delete"} <= set(acts)
    assert PW not in blob and "N3w-pass-word" not in blob and "127.0.0.1" not in blob
    assert PW not in caplog.text and "N3w-pass-word" not in caplog.text


def test_connection_role_matrix(client, team):
    ws_id, users = team
    cid = _mk(client, users["owner"], ws_id).json()["connection"]["id"]
    get = lambda who, path="": client._inner.get(f"/api/connections{path}", headers=users[who]["h"])  # noqa: E731
    for who, code in (("owner", 200), ("admin", 200), ("analyst", 200), ("viewer", 403), ("outsider", 404)):
        assert get(who, f"?workspace_id={ws_id}").status_code == code, who
    for who, code in (("analyst", 200), ("viewer", 403), ("outsider", 404)):
        assert get(who, f"/{cid}").status_code == code, who
    for who, code in (("analyst", 403), ("viewer", 403), ("outsider", 404)):
        assert _mk(client, users[who], ws_id, name=f"n-{who}").status_code == code, who
        assert client._inner.patch(f"/api/connections/{cid}", headers=users[who]["h"], json={"name": "z"}).status_code == code
        assert client._inner.delete(f"/api/connections/{cid}", headers=users[who]["h"]).status_code == code
        assert client._inner.post(f"/api/connections/{cid}/disable", headers=users[who]["h"]).status_code == code
    for who in ("viewer", "outsider"):
        assert client._inner.post(f"/api/connections/{cid}/test", headers=users[who]["h"]).status_code in (403, 404)
        assert client._inner.get(f"/api/connections/{cid}/schema", headers=users[who]["h"]).status_code in (403, 404)
    assert client._inner.get("/api/connections").status_code == 401


def test_connections_need_an_encryption_key_in_production(client, team, monkeypatch):
    ws_id, users = team
    monkeypatch.setattr(secrets_store, "configured", lambda: False)
    r = _mk(client, users["admin"], ws_id)
    assert r.status_code == 503 and r.json()["error_code"] == "CONNECTIONS_NOT_CONFIGURED"


def test_a_connection_to_an_internal_address_is_refused_when_not_allowed(client, team, monkeypatch):
    ws_id, users = team
    cid = _mk(client, users["admin"], ws_id).json()["connection"]["id"]
    monkeypatch.setattr(config, "CONNECTOR_ALLOW_PRIVATE_HOSTS", False)
    t = client._inner.post(f"/api/connections/{cid}/test", headers=users["analyst"]["h"]).json()
    assert t["ok"] is False and t["error_code"] == "HOST_NOT_ALLOWED"
    res = client._inner.post(f"/api/connections/{cid}/datasets", headers=users["analyst"]["h"], json={"table": "t"})
    assert res.status_code == 422 and res.json()["error_code"] == "HOST_NOT_ALLOWED"


def test_an_unreachable_server_reports_a_code_not_a_driver_message(client, team):
    ws_id, users = team
    cid = _mk(client, users["admin"], ws_id, port=1, ssl_mode="disable").json()["connection"]["id"]
    t = client._inner.post(f"/api/connections/{cid}/test", headers=users["analyst"]["h"]).json()
    assert t["ok"] is False and t["error_code"] == "CONNECT_FAILED" and "Connection refused" not in json.dumps(t)
    with SessionLocal() as db:
        c = db.get(Connection, cid)
        assert c.last_test_ok is False and c.last_test_error == "CONNECT_FAILED"


# ---------------------------------------------------------------- engine tests against real servers

BASE = datetime(2024, 1, 1, 10, 0, 0)


class Source:
    def __init__(self, engine: str, url: str):
        self.engine, self.url, self.tables = engine, urlparse(url), []
        self.pw = self.url.password
        self.pg = engine == "postgres"
        if self.pg:
            import psycopg2
            self.conn = psycopg2.connect(url)
            self.conn.autocommit = True
        else:
            import pymysql
            self.conn = pymysql.connect(host=self.url.hostname, port=self.url.port, user=self.url.username, password=self.pw,
                                        database=self.url.path.lstrip("/"), autocommit=True)

    def x(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        return cur

    def cfg(self):
        return {"host": self.url.hostname, "port": self.url.port, "database": self.url.path.lstrip("/"), "user": self.url.username,
                "ssl_mode": "disable" if self.pg else "require"}

    def orders(self, rows=25, pk=True):
        t = "t" + uuid.uuid4().hex[:8] + "_orders"
        self.tables.append(t)
        if self.pg:
            self.x(f"CREATE TABLE {t} (id integer {'primary key' if pk else ''}, customer text, amount numeric(10,2), note text, updated_at timestamp)")
        else:
            self.x(f"CREATE TABLE {t} (id INT {'PRIMARY KEY' if pk else ''}, customer VARCHAR(100), amount DECIMAL(10,2), note LONGTEXT, updated_at DATETIME)")
        for i in range(1, rows + 1):
            self.insert(t, i)
        return t

    def insert(self, t, i, minutes=None, amount=None):
        self.x(f"INSERT INTO {t} (id, customer, amount, note, updated_at) VALUES (%s, %s, %s, %s, %s)",
               (i, f"cust{i}", amount if amount is not None else i * 10, "n", BASE + timedelta(minutes=i if minutes is None else minutes)))

    def touch(self, t, i, amount, minutes):
        self.x(f"UPDATE {t} SET amount = %s, updated_at = %s WHERE id = %s", (amount, BASE + timedelta(minutes=minutes), i))

    def close(self):
        for t in self.tables:
            for stmt in (f"DROP VIEW IF EXISTS {t}_v", f"DROP TABLE IF EXISTS {t}"):
                try:
                    self.x(stmt)
                except Exception:  # noqa: BLE001
                    pass
        self.conn.close()


@pytest.fixture(params=["postgres", "mysql"])
def src(request):
    url = os.environ.get("TEST_SOURCE_POSTGRES_URL" if request.param == "postgres" else "TEST_SOURCE_MYSQL_URL")
    if not url:
        pytest.skip(f"set TEST_SOURCE_{request.param.upper()}_URL to run the {request.param} connector tests")
    s = Source(request.param, url)
    yield s
    s.close()


@pytest.fixture()
def ctx(client, src, monkeypatch):
    monkeypatch.setattr(config, "INCREMENTAL_OVERLAP_SECONDS", 0)
    monkeypatch.setattr(config, "CONNECTION_FETCH_BATCH_ROWS", 7)       # several batches per run
    owner = _user(client, "own")
    ws = client._inner.get("/api/workspaces", headers=owner["h"]).json()["personal_workspace_id"]
    r = client._inner.post("/api/connections", headers=owner["h"], json={
        "workspace_id": ws, "name": "src", "connector": src.engine, "config": src.cfg(), "password": src.pw})
    assert r.status_code == 201, r.text

    class C:
        pass
    c = C()
    c.owner, c.ws, c.cid, c.src = owner, ws, r.json()["connection"]["id"], src
    return c


def _job(client, user, job_id):
    return client._inner.get(f"/api/jobs/{job_id}", headers=user["h"]).json()


def _import(client, c, table, mode="full_refresh", wm=None):
    r = client._inner.post(f"/api/connections/{c.cid}/datasets", headers=c.owner["h"],
                           json={"table": table, "mode": mode, "watermark_column": wm})
    assert r.status_code == 202, r.text
    return _job(client, c.owner, r.json()["job_id"])


def _sync(client, c, ds_id, mode="incremental"):
    r = client._inner.post(f"/api/datasets/{ds_id}/sync", headers=c.owner["h"], json={"mode": mode})
    assert r.status_code == 202, r.text
    return _job(client, c.owner, r.json()["job_id"])


def _rows(ds_id, table):
    con = sqlite3.connect(_curated_path(ds_id))
    try:
        return con.execute(f'SELECT id, customer, amount, updated_at FROM "{table}" ORDER BY id').fetchall()
    finally:
        con.close()


def _checkpoint(ds_id):
    with SessionLocal() as db:
        cp = db.query(IngestionCheckpoint).filter(IngestionCheckpoint.dataset_id == ds_id).first()
        return cp.value if cp else None


def test_discovery_reports_tables_keys_and_watermark_candidates(client, ctx):
    t = ctx.src.orders(3)
    ctx.src.x(f"CREATE VIEW {t}_v AS SELECT id, customer FROM {t}")
    tables = {x["name"]: x for x in client._inner.get(f"/api/connections/{ctx.cid}/schema", headers=ctx.owner["h"]).json()["tables"]}
    o = tables[t]
    assert o["primary_key"] == ["id"] and o["incremental_ready"] is True and not o["is_view"]
    kinds = {c["name"]: c["watermark_ok"] for c in o["columns"]}
    assert kinds == {"id": True, "customer": False, "amount": False, "note": False, "updated_at": True}
    assert tables[f"{t}_v"]["is_view"] is True and tables[f"{t}_v"]["primary_key"] == []
    ok = client._inner.post(f"/api/connections/{ctx.cid}/test", headers=ctx.owner["h"]).json()
    assert ok["ok"] is True and ctx.src.engine.replace("postgres", "PostgreSQL").replace("mysql", "MySQL") in ok["message"]


def test_full_refresh_imports_every_row_in_batches_and_is_queryable(client, ctx):
    t = ctx.src.orders(25)
    job = _import(client, ctx, t)
    assert job["status"] == "succeeded", job
    res = job["result"]
    assert (res["mode"], res["fallback_reason"], res["records_read"], res["records_inserted"], res["records_rejected"]) == ("full_refresh", None, 25, 25, 0)
    ds_id = res["dataset_id"]
    curated = safe_table_name(t)
    assert len(_rows(ds_id, curated)) == 25 and _rows(ds_id, curated)[0] == (1, "cust1", 10.0, "2024-01-01 10:01:00")
    sess, sid = _open(client, ctx.owner, ds_id)
    assert sess.status_code == 200
    q = _sql(client, ctx.owner, sid, f"SELECT COUNT(*) AS n, SUM(amount) AS s FROM {curated}").json()["rows"][0]
    assert (q["n"], q["s"]) == (25, sum(i * 10 for i in range(1, 26)))
    run = _runs(client, ctx.owner, ds_id)[0]
    assert run["status"] == "succeeded" and run["table_stats"][curated]["inserted"] == 25


def test_incremental_backfill_then_only_new_and_changed_rows(client, ctx):
    src = ctx.src
    t = src.orders(25)
    first = _import(client, ctx, t, "incremental", "updated_at")
    assert first["status"] == "succeeded" and first["result"]["mode"] == "full_refresh" and first["result"]["fallback_reason"] is None
    ds_id, curated = first["result"]["dataset_id"], safe_table_name(t)
    assert _checkpoint(ds_id) == (BASE + timedelta(minutes=25)).isoformat(sep=" ")
    for i in range(26, 31):                                  # five new rows
        src.insert(t, i)
    for i, mins in ((3, 40), (9, 41), (12, 42)):             # three changed rows (watermark moves)
        src.touch(t, i, 999, mins)
    job = _sync(client, ctx, ds_id)
    res = job["result"]
    assert job["status"] == "succeeded" and res["mode"] == "incremental" and res["fallback_reason"] is None
    assert (res["records_inserted"], res["records_updated"], res["records_rejected"]) == (5, 3, 0)
    assert res["records_read"] == res["records_inserted"] + res["records_updated"] + res["records_skipped"]
    assert res["records_read"] < 30, "an incremental run must not re-read the whole table"
    rows = {r[0]: r for r in _rows(ds_id, curated)}
    assert len(rows) == 30 and rows[3][2] == 999 and rows[9][2] == 999 and rows[30][1] == "cust30" and rows[4][2] == 40
    assert _checkpoint(ds_id) == (BASE + timedelta(minutes=42)).isoformat(sep=" ")
    # re-running the same window changes nothing and duplicates nothing
    again = _sync(client, ctx, ds_id)["result"]
    assert (again["records_inserted"], again["records_updated"]) == (0, 0) and again["records_skipped"] == again["records_read"] >= 1
    assert len(_rows(ds_id, curated)) == 30
    runs = _runs(client, ctx.owner, ds_id)
    assert [r["status"] for r in runs] == ["succeeded"] * 3 and runs[0]["dataset_version"] == 3


def test_overlap_window_rereads_recent_rows_idempotently(client, ctx, monkeypatch):
    t = ctx.src.orders(10)
    ds_id = _import(client, ctx, t, "incremental", "updated_at")["result"]["dataset_id"]
    monkeypatch.setattr(config, "INCREMENTAL_OVERLAP_SECONDS", 300)        # five minutes: re-reads the last six rows
    res = _sync(client, ctx, ds_id)["result"]
    assert res["records_read"] == 6 and res["records_skipped"] == 6 and res["records_inserted"] == res["records_updated"] == 0


def test_integer_watermark(client, ctx):
    t = ctx.src.orders(12)
    ds_id = _import(client, ctx, t, "incremental", "id")["result"]["dataset_id"]
    assert _checkpoint(ds_id) == "12"
    for i in (13, 14):
        ctx.src.insert(t, i)
    res = _sync(client, ctx, ds_id)["result"]
    assert (res["mode"], res["records_inserted"], res["records_read"]) == ("incremental", 2, 3)


def test_incremental_is_refused_or_falls_back_with_a_reason(client, ctx):
    src = ctx.src
    nopk = src.orders(6, pk=False)
    bad = client._inner.post(f"/api/connections/{ctx.cid}/datasets", headers=ctx.owner["h"],
                             json={"table": nopk, "mode": "incremental", "watermark_column": "updated_at"})
    assert bad.status_code == 422 and bad.json()["error_code"] == "NO_PRIMARY_KEY"
    t = src.orders(5)
    bad = client._inner.post(f"/api/connections/{ctx.cid}/datasets", headers=ctx.owner["h"],
                             json={"table": t, "mode": "incremental", "watermark_column": "customer"})
    assert bad.status_code == 422 and bad.json()["error_code"] == "INVALID_WATERMARK"
    assert client._inner.post(f"/api/connections/{ctx.cid}/datasets", headers=ctx.owner["h"],
                              json={"table": "nope_nope", "mode": "full_refresh"}).status_code == 404
    # a table without a key still imports (streamed) as a full refresh, and "incremental" later falls back with its reason
    job = _import(client, ctx, nopk)
    assert job["status"] == "succeeded" and job["result"]["records_inserted"] == 6
    res = _sync(client, ctx, job["result"]["dataset_id"], "incremental")["result"]
    assert (res["mode"], res["fallback_reason"], res["records_inserted"]) == ("full_refresh", "no_watermark", 6)
    only = _import(client, ctx, t)["result"]["dataset_id"]
    res = _sync(client, ctx, only, "incremental")["result"]
    assert (res["mode"], res["fallback_reason"]) == ("full_refresh", "no_watermark")


def test_schema_changes_force_a_full_refresh_and_are_reported(client, ctx):
    src = ctx.src
    t = src.orders(8)
    ds_id = _import(client, ctx, t, "incremental", "updated_at")["result"]["dataset_id"]
    src.x(f"ALTER TABLE {t} ADD COLUMN extra integer")
    src.x(f"UPDATE {t} SET extra = id")
    res = _sync(client, ctx, ds_id)["result"]
    assert (res["mode"], res["fallback_reason"], res["records_inserted"]) == ("full_refresh", "schema_changed", 8)
    run = _runs(client, ctx.owner, ds_id)[0]
    assert {"table": safe_table_name(t), "change": "column_added", "column": "extra", "to": "INTEGER"} in run["schema_changes"]
    with SessionLocal() as db:
        assert db.get(Dataset, ds_id).schema_version == 2
        assert "ingestion.schema_change" in [a.action for a in db.query(AuditLog)]
    assert _sync(client, ctx, ds_id)["result"]["mode"] == "incremental"          # the new shape is now the baseline
    # an incompatible type change is reported, never coerced
    src.x(f"ALTER TABLE {t} ALTER COLUMN amount TYPE text" if src.pg else f"ALTER TABLE {t} MODIFY amount VARCHAR(40)")
    res = _sync(client, ctx, ds_id)["result"]
    assert res["fallback_reason"] == "schema_changed"
    changes = _runs(client, ctx.owner, ds_id)[0]["schema_changes"]
    assert {"table": safe_table_name(t), "change": "type_changed", "column": "amount", "from": "REAL", "to": "TEXT", "compatible": False} in changes


def test_unstorable_values_are_quarantined_with_a_reason(client, ctx):
    src = ctx.src
    t = src.orders(5)
    src.x(f"UPDATE {t} SET note = %s WHERE id = 2", ("x" * (config_limit() + 1),))
    job = _import(client, ctx, t)
    res = job["result"]
    assert job["status"] == "succeeded" and (res["records_read"], res["records_inserted"], res["records_rejected"]) == (5, 4, 1)
    ds_id = res["dataset_id"]
    run = _runs(client, ctx.owner, ds_id)[0]
    assert run["reject_reasons"] == {"value_too_large": 1}
    sample = client._inner.get(f"/api/datasets/{ds_id}/quarantine", headers=ctx.owner["h"]).json()["rows"]
    assert [x["reason"] for x in sample] == ["value_too_large"] and "xxxx" not in json.dumps(sample)


def config_limit() -> int:
    from connectors.database import MAX_TEXT_CELL
    return MAX_TEXT_CELL


def test_a_failed_run_keeps_the_previous_version_and_checkpoint_then_recovers(client, ctx, monkeypatch):
    from connectors.database import DatabaseConnector
    src = ctx.src
    t = src.orders(10)
    ds_id = _import(client, ctx, t, "incremental", "updated_at")["result"]["dataset_id"]
    curated = safe_table_name(t)
    with SessionLocal() as db:
        v1, ref = db.get(Dataset, ds_id).version, db.get(Dataset, ds_id).storage_ref
    cp = _checkpoint(ds_id)
    for i in range(11, 31):
        src.insert(t, i)
    real, calls = DatabaseConnector.read_keyset, []

    def dies_after_one_batch(self, *a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise ConnectorError("QUERY_FAILED", "The database could not run the query.")
        return real(self, *a, **k)
    monkeypatch.setattr(DatabaseConnector, "read_keyset", dies_after_one_batch)
    job = _sync(client, ctx, ds_id)
    monkeypatch.setattr(DatabaseConnector, "read_keyset", real)      # (monkeypatch.undo() would also undo the fixtures)
    assert job["status"] == "failed"
    with SessionLocal() as db:
        ds = db.get(Dataset, ds_id)
        assert ds.version == v1 and ds.storage_ref == ref and ds.status == "ready"
    assert os.path.exists(ref) and _checkpoint(ds_id) == cp and len(_rows(ds_id, curated)) == 10
    failed = _runs(client, ctx.owner, ds_id)[0]
    assert failed["status"] == "failed" and failed["error_code"] == "QUERY_FAILED"
    ok = _sync(client, ctx, ds_id)["result"]                       # a retry loses nothing and duplicates nothing
    assert ok["records_inserted"] == 20 and len(_rows(ds_id, curated)) == 30


def test_a_transient_failure_resumes_from_the_last_key_within_one_run(client, ctx, monkeypatch):
    from connectors.database import DatabaseConnector
    import ingest_db
    monkeypatch.setattr(ingest_db.time, "sleep", lambda s: None)
    t = ctx.src.orders(30)
    real, state = DatabaseConnector.read_keyset, {"n": 0, "afters": []}

    def flaky(self, conn, cfg, table, order, after, limit, watermark=None):
        state["n"] += 1
        state["afters"].append(after)
        if state["n"] == 3:
            raise ConnectorError("CONNECT_FAILED", "The database could not be reached.", transient=True)
        return real(self, conn, cfg, table, order, after, limit, watermark)
    monkeypatch.setattr(DatabaseConnector, "read_keyset", flaky)
    job = _import(client, ctx, t)
    assert job["status"] == "succeeded" and job["result"]["records_inserted"] == 30
    assert len(_rows(job["result"]["dataset_id"], safe_table_name(t))) == 30
    assert state["afters"][2] == state["afters"][3] and state["afters"][3] is not None, "the retry continued after the last key"


def test_a_permanent_failure_is_not_retried_and_a_wrong_password_is_a_code(client, ctx, src, monkeypatch):
    import ingest_db
    slept = []
    monkeypatch.setattr(ingest_db.time, "sleep", lambda s: slept.append(s))
    t = ctx.src.orders(3)
    assert client._inner.patch(f"/api/connections/{ctx.cid}", headers=ctx.owner["h"], json={"password": "wrong-password"}).status_code == 200
    r = client._inner.post(f"/api/connections/{ctx.cid}/test", headers=ctx.owner["h"]).json()
    assert r["ok"] is False and r["error_code"] == "AUTH_FAILED"
    res = client._inner.post(f"/api/connections/{ctx.cid}/datasets", headers=ctx.owner["h"], json={"table": t})
    assert res.status_code == 422 and res.json()["error_code"] == "AUTH_FAILED" and "wrong-password" not in res.text and not slept


def test_only_one_run_per_dataset_and_a_lost_job_frees_it(client, ctx, monkeypatch):
    import jobs
    t = ctx.src.orders(4)
    ds_id = _import(client, ctx, t)["result"]["dataset_id"]
    from datetime import timezone
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    lost = "lost-" + uuid.uuid4().hex[:10]
    with SessionLocal() as db:
        db.add(Job(id=lost, user_id=ctx.owner["id"], kind="dataset_ingestion", status="running", created_at=old, updated_at=old))
        db.commit()
        ingest.start_run(db, db.get(Dataset, ds_id), user_id=ctx.owner["id"], job_id=lost, connector=ctx.src.engine,
                         source_name="x", source_bytes=0, fp="")
    refused = client._inner.post(f"/api/datasets/{ds_id}/sync", headers=ctx.owner["h"], json={"mode": "full_refresh"})
    assert refused.status_code == 409 and refused.json()["error_code"] == "RUN_IN_PROGRESS"
    monkeypatch.setattr(jobs, "worker_alive", lambda: False)
    with SessionLocal() as db:
        assert jobs.fail_orphaned_jobs(db) >= 1
        assert db.query(IngestionRun).filter(IngestionRun.job_id == lost).one().status == "interrupted"
    assert _sync(client, ctx, ds_id, "full_refresh")["status"] == "succeeded"


def test_workspace_isolation_and_roles_for_connection_datasets(client, ctx):
    t = ctx.src.orders(6)
    ds_id = _import(client, ctx, t, "incremental", "updated_at")["result"]["dataset_id"]
    other = _user(client, "oth")
    for call in (lambda: client._inner.post(f"/api/datasets/{ds_id}/sync", headers=other["h"], json={"mode": "incremental"}),
                 lambda: client._inner.get(f"/api/datasets/{ds_id}/source", headers=other["h"]),
                 lambda: client._inner.post(f"/api/connections/{ctx.cid}/datasets", headers=other["h"], json={"table": t}),
                 lambda: client._inner.get(f"/api/connections/{ctx.cid}/schema", headers=other["h"])):
        assert call().status_code == 404
    src = client._inner.get(f"/api/datasets/{ds_id}/source", headers=ctx.owner["h"]).json()["source"]
    assert (src["kind"], src["table"], src["watermark_column"], src["primary_key"]) == ("database", t, "updated_at", ["id"])
    assert src["connection"]["name"] == "src" and src["checkpoint"]["value"] and "host" not in json.dumps(src)
    # file re-import is refused for a connection dataset
    up = client._inner.post(f"/api/datasets/{ds_id}/runs", headers=ctx.owner["h"], files={"file": ("a.csv", b"a\n1\n", "text/csv")})
    assert up.status_code == 409 and up.json()["error_code"] == "SOURCE_IS_CONNECTION"


def test_disabling_or_deleting_a_connection_stops_syncs_but_keeps_the_data(client, ctx):
    t = ctx.src.orders(5)
    ds_id = _import(client, ctx, t)["result"]["dataset_id"]
    client._inner.post(f"/api/connections/{ctx.cid}/disable", headers=ctx.owner["h"])
    assert client._inner.post(f"/api/datasets/{ds_id}/sync", headers=ctx.owner["h"], json={"mode": "full_refresh"}).status_code == 409
    client._inner.post(f"/api/connections/{ctx.cid}/enable", headers=ctx.owner["h"])
    assert _sync(client, ctx, ds_id, "full_refresh")["status"] == "succeeded"
    d = client._inner.delete(f"/api/connections/{ctx.cid}", headers=ctx.owner["h"]).json()
    assert d["datasets_detached"] == 1
    assert client._inner.post(f"/api/datasets/{ds_id}/sync", headers=ctx.owner["h"], json={"mode": "full_refresh"}).status_code == 409
    sess, sid = _open(client, ctx.owner, ds_id)
    assert _sql(client, ctx.owner, sid, f"SELECT COUNT(*) AS n FROM {safe_table_name(t)}").json()["rows"][0]["n"] == 5
    with SessionLocal() as db:
        assert db.query(Connection).filter(Connection.id == ctx.cid).count() == 0


def test_no_secret_reaches_runs_jobs_audit_or_quarantine(client, ctx):
    t = ctx.src.orders(4)
    job = _import(client, ctx, t, "incremental", "updated_at")
    ds_id = job["result"]["dataset_id"]
    blob = json.dumps([job, _runs(client, ctx.owner, ds_id), client._inner.get(f"/api/datasets/{ds_id}/source", headers=ctx.owner["h"]).json()])
    with SessionLocal() as db:
        blob += json.dumps([a.details for a in db.query(AuditLog)], default=str)
        blob += json.dumps([r.error_detail for r in db.query(IngestionRun)])
    assert ctx.src.pw not in blob
    lineage = sqlite3.connect(_curated_path(ds_id)).execute("SELECT source_type, connector, source_name FROM _qs_lineage").fetchall()
    assert lineage == [("database", ctx.src.engine, f"src.{t}")]


def test_connection_values_map_to_sqlite_types(client, ctx):
    src = ctx.src
    t = "t" + uuid.uuid4().hex[:8] + "_types"
    src.tables.append(t)
    if src.pg:
        src.x(f"CREATE TABLE {t} (id integer primary key, flag boolean, doc jsonb, payload bytea, ts timestamptz, d date)")
        src.x(f"INSERT INTO {t} VALUES (1, true, '{{\"a\": 1}}', '\\xdeadbeef', '2024-03-01 12:00:00+02', '2024-03-01')")
    else:
        src.x(f"CREATE TABLE {t} (id INT PRIMARY KEY, flag TINYINT(1), doc JSON, payload BLOB, ts DATETIME, d DATE)")
        src.x(f"INSERT INTO {t} VALUES (1, 1, '{{\"a\": 1}}', X'DEADBEEF', '2024-03-01 10:00:00', '2024-03-01')")
    ds_id = _import(client, ctx, t)["result"]["dataset_id"]
    row = sqlite3.connect(_curated_path(ds_id)).execute(f'SELECT flag, doc, payload, ts, d FROM "{safe_table_name(t)}"').fetchone()
    assert row == (1, '{"a": 1}', "deadbeef", "2024-03-01 10:00:00", "2024-03-01")        # timestamptz is stored as UTC


def test_queries_over_a_synced_dataset_are_blocked_from_internals(client, ctx):
    t = ctx.src.orders(4)
    ds_id = _import(client, ctx, t)["result"]["dataset_id"]
    sess, sid = _open(client, ctx.owner, ds_id)
    assert _sql(client, ctx.owner, sid, "SELECT * FROM _qs_quarantine").status_code >= 400
    assert time.time() > 0
