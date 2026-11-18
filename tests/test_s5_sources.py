"""Sprint 5: REST and S3 sources and the scheduler. REST runs against a real local HTTP server; S3 against moto (an in-memory S3);
the scheduler against the real database. Nothing here needs an external service."""
from __future__ import annotations

import io
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

import config
import ingest
import scheduler
import secrets_store
from connectors import httpclient, netguard
from connectors.database import ConnectorError
from connectors.rest import RestConnector
from connectors.s3_source import S3SourceConnector
from db import AuditLog, Connection, Dataset, DatasetSchedule, IngestionCheckpoint, IngestionRun, Job, SessionLocal
from modules.ingestion import safe_table_name
from tests.test_s1_workspaces import _add, _team, _user, local_uploads  # noqa: F401
from tests.test_s3_ingestion import _curated_path, _open, _runs, _sql

TOKEN = "tok-Secret-4242"
BASE = datetime(2024, 3, 1, 8, 0, 0)


# ================================================================ a real local REST server

class State:
    def __init__(self):
        self.records = []
        self.requests = []
        self.flaky_left = 0

    def reset(self, n=25):
        self.records = [self.rec(i) for i in range(1, n + 1)]
        self.requests.clear()
        self.flaky_left = 0

    @staticmethod
    def rec(i, minutes=None, **extra):
        return {"id": i, "name": f"n{i}", "amount": i * 10, "updated_at": (BASE + timedelta(minutes=i if minutes is None else minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "meta": {"tier": "a"}, "tags": ["x"], **extra}


STATE = State()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        STATE.requests.append({"path": u.path, "query": q, "auth": self.headers.get("Authorization"), "host": self.headers.get("Host"),
                               "key": self.headers.get("X-Api-Key")})
        recs = list(STATE.records)
        if "updated_since" in q:
            floor = q["updated_since"]
            recs = [r for r in recs if r["updated_at"] >= floor]
        p = u.path
        if p == "/auth" or p == "/secure":
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self._send(401, {"error": "nope"})
            return self._send(200, {"data": recs})
        if p == "/page":
            size, page = int(q.get("per_page", 10)), int(q.get("page", 1))
            return self._send(200, {"data": recs[(page - 1) * size:page * size]})
        if p == "/offset":
            size, off = int(q.get("limit", 10)), int(q.get("offset", 0))
            return self._send(200, {"items": recs[off:off + size]})
        if p == "/cursor":
            size, cur = int(q.get("limit", 10)), int(q.get("cursor", 0))
            nxt = cur + size
            return self._send(200, {"results": recs[cur:nxt], "meta": {"next": str(nxt) if nxt < len(recs) else None}})
        if p == "/nexturl":
            size, page = 10, int(q.get("page", 1))
            chunk = recs[(page - 1) * size:page * size]
            return self._send(200, {"data": chunk, "links": {"next": f"/nexturl?page={page + 1}" if page * size < len(recs) else None}})
        if p == "/evilnext":
            return self._send(200, {"data": recs[:2], "links": {"next": "http://other.example.com/x"}})
        if p == "/plain":
            return self._send(200, recs)
        if p == "/flaky":
            if STATE.flaky_left > 0:
                STATE.flaky_left -= 1
                return self._send(503, {"e": 1}, extra={"Retry-After": "7"})
            return self._send(200, {"data": recs})
        if p == "/ratelimited":
            return self._send(429, {"e": 1}, extra={"Retry-After": "3"})
        if p == "/redirect":
            return self._send(302, b"", extra={"Location": "http://127.0.0.1:1/x"})
        if p == "/big":
            return self._send(200, b'{"data": [' + b'{"id": 1, "pad": "' + b"x" * 1_300_000 + b'"}]}')
        if p == "/html":
            return self._send(200, b"<html>hi</html>", "text/html")
        if p == "/empty":
            return self._send(200, {"data": []})
        if p == "/nokey":
            return self._send(200, {"data": [{"name": "a"}, {"name": "b"}]})
        if p == "/messy":
            return self._send(200, {"data": [self_rec for self_rec in recs] + [{"id": None, "name": "nokey", "updated_at": "2024-03-01T09:00:00Z"},
                                                                              {"id": 999, "name": "badts", "updated_at": "not-a-date"}]})
        return self._send(404, {"e": 1})


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


@pytest.fixture()
def api_srv(server):
    STATE.reset()
    return f"http://127.0.0.1:{server.server_address[1]}"


def _res(name="orders", path="/page", pagination=None, **over):
    r = {"name": name, "path": path, "records_path": "data", "primary_key": ["id"],
         "pagination": pagination or {"type": "page", "param": "page", "size_param": "per_page", "size": 10},
         "updated_since": {"param": "updated_since", "field": "updated_at", "format": "iso"}}
    r.update(over)
    return r


def _rest_cfg(base, *resources, auth=None, **over):
    return {"base_url": base, "auth": auth or {"type": "none"}, "resources": list(resources) or [_res()], **over}


# ================================================================ shared helpers

@pytest.fixture()
def me(client):
    owner = _user(client, "s5")
    ws = client._inner.get("/api/workspaces", headers=owner["h"]).json()["personal_workspace_id"]
    return owner, ws


def _mkconn(client, owner, ws, connector, cfg, password=None, name=None):
    r = client._inner.post("/api/connections", headers=owner["h"], json={
        "workspace_id": ws, "name": name or f"c{uuid.uuid4().hex[:6]}", "connector": connector, "config": cfg, "password": password})
    assert r.status_code == 201, r.text
    return r.json()["connection"]["id"]


def _job(client, owner, job_id):
    return client._inner.get(f"/api/jobs/{job_id}", headers=owner["h"]).json()


def _import(client, owner, cid, table, mode="full_refresh"):
    r = client._inner.post(f"/api/connections/{cid}/datasets", headers=owner["h"], json={"table": table, "mode": mode})
    assert r.status_code == 202, r.text
    return _job(client, owner, r.json()["job_id"])


def _sync(client, owner, ds_id, mode="incremental"):
    r = client._inner.post(f"/api/datasets/{ds_id}/sync", headers=owner["h"], json={"mode": mode})
    assert r.status_code == 202, r.text
    return _job(client, owner, r.json()["job_id"])


def _rows(ds_id, table, cols="id, name, amount"):
    con = sqlite3.connect(_curated_path(ds_id))
    try:
        return con.execute(f'SELECT {cols} FROM "{safe_table_name(table)}" ORDER BY 1').fetchall()
    finally:
        con.close()


def _failed_code(client, owner, ds_or_none=None):
    """Error code of the latest failed import. A failed FIRST import leaves no dataset (and so no run), but its audit entry remains."""
    with SessionLocal() as db:
        row = db.query(AuditLog).filter(AuditLog.action == "ingestion.fail").order_by(AuditLog.created_at.desc()).first()
        return (row.details or {}).get("error_code") if row else None


# ================================================================ REST: configuration

@pytest.mark.parametrize("bad", [
    {"base_url": "ftp://x.example.com", "resources": [_res()]},
    {"base_url": "https://user:pw@x.example.com", "resources": [_res()]},
    {"base_url": "https://x.example.com/?a=1", "resources": [_res()]},
    {"base_url": "https://x.example.com", "resources": []},
    {"base_url": "https://x.example.com", "resources": [_res(path="/a/../b")]},
    {"base_url": "https://x.example.com", "resources": [_res(path="//evil.example.com/x")]},
    {"base_url": "https://x.example.com", "resources": [_res(name="1bad")]},
    {"base_url": "https://x.example.com", "resources": [_res(), _res()]},
    {"base_url": "https://x.example.com", "resources": [_res()], "headers": {"Authorization": "x"}},
    {"base_url": "https://x.example.com", "resources": [_res()], "headers": {"X-A": "a\nb"}},
    {"base_url": "https://x.example.com", "resources": [_res()], "auth": {"type": "oauth"}},
    {"base_url": "https://x.example.com", "resources": [_res()], "auth": {"type": "header", "header": "Cookie"}},
    {"base_url": "https://x.example.com", "resources": [_res(pagination={"type": "cursor", "param": "c"})]},
    {"base_url": "https://x.example.com", "resources": [_res(pagination={"type": "weird"})]},
    {"base_url": "https://x.example.com", "resources": [_res(updated_since={"param": "s"})]},
    {"base_url": "https://x.example.com", "resources": [_res()], "surprise": 1},
])
def test_rest_config_validation_rejects_unsafe_or_incomplete_settings(bad):
    with pytest.raises(ValueError):
        RestConnector().validate_config(bad)


def test_rest_config_is_normalized_and_http_is_development_only(monkeypatch):
    ok = RestConnector().validate_config(_rest_cfg("https://api.example.com/v2/", auth={"type": "basic", "username": "svc"}))
    assert ok["base_url"] == "https://api.example.com/v2" and ok["resources"][0]["pagination"]["size"] == 10
    RestConnector().validate_config(_rest_cfg("http://localhost:8080"))              # development
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    with pytest.raises(ValueError):
        RestConnector().validate_config(_rest_cfg("http://api.example.com"))


# ================================================================ REST: pagination styles

@pytest.mark.parametrize("path, pagination, records_path", [
    ("/page", {"type": "page", "param": "page", "size_param": "per_page", "size": 10}, "data"),
    ("/offset", {"type": "offset", "param": "offset", "size_param": "limit", "size": 10}, "items"),
    ("/cursor", {"type": "cursor", "param": "cursor", "size_param": "limit", "size": 10, "next_path": "meta.next"}, "results"),
    ("/nexturl", {"type": "next_url", "next_path": "links.next"}, "data"),
    ("/plain", {"type": "none"}, ""),
])
def test_every_pagination_style_reads_all_records_once(client, me, api_srv, path, pagination, records_path):
    owner, ws = me
    cid = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv, _res(path=path, pagination=pagination, records_path=records_path)))
    job = _import(client, owner, cid, "orders")
    assert job["status"] == "succeeded", job
    res = job["result"]
    assert (res["records_read"], res["records_inserted"], res["records_rejected"]) == (25, 25, 0)
    assert [r[0] for r in _rows(res["dataset_id"], "orders")] == list(range(1, 26))
    pages = [r for r in STATE.requests if r["path"] == path]
    # the import first discovers the resource (one request for its first page), then reads every page
    assert len(pages) == 1 + {"/plain": 1, "/nexturl": 3, "/page": 3, "/offset": 3, "/cursor": 3}[path]


def test_nested_values_become_columns_and_json_text(client, me, api_srv):
    owner, ws = me
    cid = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv))
    ds_id = _import(client, owner, cid, "orders")["result"]["dataset_id"]
    row = sqlite3.connect(_curated_path(ds_id)).execute('SELECT meta_tier, tags FROM "orders" WHERE id = 1').fetchone()
    assert row == ("a", '["x"]')
    sess, sid = _open(client, owner, ds_id)
    assert _sql(client, owner, sid, "SELECT SUM(amount) AS s FROM orders").json()["rows"][0]["s"] == sum(i * 10 for i in range(1, 26))


def test_a_next_page_url_on_another_server_is_not_followed(client, me, api_srv):
    owner, ws = me
    cid = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv, _res(path="/evilnext", pagination={"type": "next_url", "next_path": "links.next"})))
    job = _import(client, owner, cid, "orders")
    assert job["status"] == "failed" and _failed_code(client, owner) == "NEXT_URL_REFUSED"
    assert not any(r["host"] and "other.example.com" in r["host"] for r in STATE.requests)


# ================================================================ REST: incremental

def test_rest_incremental_sends_the_watermark_and_upserts_by_key(client, me, api_srv, monkeypatch):
    monkeypatch.setattr(config, "INCREMENTAL_OVERLAP_SECONDS", 0)
    owner, ws = me
    cid = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv))
    first = _import(client, owner, cid, "orders", "incremental")
    res = first["result"]
    assert first["status"] == "succeeded" and (res["mode"], res["fallback_reason"], res["records_inserted"]) == ("full_refresh", None, 25)
    ds_id = res["dataset_id"]
    with SessionLocal() as db:
        assert db.query(IngestionCheckpoint).filter_by(dataset_id=ds_id).one().value == (BASE + timedelta(minutes=25)).isoformat(sep=" ")
    for i in range(26, 31):
        STATE.records.append(State.rec(i))
    STATE.records[2] = State.rec(3, minutes=40, amount=999)                       # a changed record
    STATE.requests.clear()
    job = _sync(client, owner, ds_id)
    r = job["result"]
    assert job["status"] == "succeeded" and r["mode"] == "incremental"
    assert (r["records_inserted"], r["records_updated"]) == (5, 1)
    assert r["records_read"] == r["records_inserted"] + r["records_updated"] + r["records_skipped"] and r["records_read"] <= 7
    assert STATE.requests[0]["query"]["updated_since"] == "2024-03-01T08:25:00Z"
    rows = {x[0]: x for x in _rows(ds_id, "orders")}
    assert len(rows) == 30 and rows[3][2] == 999
    again = _sync(client, owner, ds_id)["result"]
    assert (again["records_inserted"], again["records_updated"]) == (0, 0) and again["records_skipped"] == again["records_read"] >= 1
    assert len(_rows(ds_id, "orders")) == 30


def test_rest_syncs_get_a_quality_report_each(client, me, api_srv, monkeypatch):      # Sprint 6 hook on the records pipeline
    monkeypatch.setattr(config, "INCREMENTAL_OVERLAP_SECONDS", 0)
    owner, ws = me
    cid = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv))
    ds_id = _import(client, owner, cid, "orders", "incremental")["result"]["dataset_id"]
    for i in range(26, 31):
        STATE.records.append(State.rec(i))
    assert _sync(client, owner, ds_id)["status"] == "succeeded"
    body = client._inner.get(f"/api/datasets/{ds_id}/quality", headers=owner["h"]).json()
    assert len(body["history"]) == 2 and body["latest"]["profile"]["orders"]["rows"] == 30 and body["latest"]["status"] == "ok"


def test_a_new_field_in_the_api_becomes_a_reported_column(client, me, api_srv, monkeypatch):
    monkeypatch.setattr(config, "INCREMENTAL_OVERLAP_SECONDS", 0)
    owner, ws = me
    cid = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv))
    ds_id = _import(client, owner, cid, "orders", "incremental")["result"]["dataset_id"]
    STATE.records.append(State.rec(26, region="emea"))
    assert _sync(client, owner, ds_id)["status"] == "succeeded"
    changes = _runs(client, owner, ds_id)[0]["schema_changes"]
    assert {"table": "orders", "change": "column_added", "column": "region", "to": "TEXT"} in changes
    assert _rows(ds_id, "orders", "id, region")[-1] == (26, "emea")
    with SessionLocal() as db:
        assert db.get(Dataset, ds_id).schema_version == 2


def test_rest_incremental_needs_a_key_and_a_watermark_else_it_falls_back(client, me, api_srv):
    owner, ws = me
    plain = _res(name="plain", path="/plain", pagination={"type": "none"}, records_path="", updated_since=None)
    cid = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv, plain, _res()))
    bad = client._inner.post(f"/api/connections/{cid}/datasets", headers=owner["h"], json={"table": "plain", "mode": "incremental"})
    assert bad.status_code == 422 and bad.json()["error_code"] == "INCREMENTAL_NOT_AVAILABLE"
    ds_id = _import(client, owner, cid, "plain")["result"]["dataset_id"]
    res = _sync(client, owner, ds_id, "incremental")["result"]
    assert (res["mode"], res["fallback_reason"]) == ("full_refresh", "no_watermark")
    tables = {t["name"]: t for t in client._inner.get(f"/api/connections/{cid}/schema", headers=owner["h"]).json()["tables"]}
    assert tables["plain"]["incremental_ready"] is False and tables["orders"]["incremental_ready"] is True and tables["orders"]["watermark"] == "updated_at"


def test_records_that_cannot_be_keyed_or_timed_are_quarantined(client, me, api_srv):
    owner, ws = me
    cid = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv, _res(path="/messy", pagination={"type": "none"})))
    job = _import(client, owner, cid, "orders")
    r = job["result"]
    assert job["status"] == "succeeded" and (r["records_read"], r["records_inserted"], r["records_rejected"]) == (27, 25, 2)
    assert _runs(client, owner, r["dataset_id"])[0]["reject_reasons"] == {"missing_key": 1, "invalid_watermark": 1}


def test_a_missing_primary_key_field_or_an_empty_resource_is_a_clear_failure(client, me, api_srv):
    owner, ws = me
    cid = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv, _res(name="nokey", path="/nokey", pagination={"type": "none"}),
                                                        _res(name="empty", path="/empty", pagination={"type": "none"})))
    assert _import(client, owner, cid, "nokey")["status"] == "failed" and _failed_code(client, owner) == "INVALID_RESPONSE"
    assert _import(client, owner, cid, "empty")["status"] == "failed" and _failed_code(client, owner) == "EMPTY_SOURCE"
    with SessionLocal() as db:
        assert db.query(Dataset).filter(Dataset.created_by == owner["id"]).count() == 0     # a failed first import leaves nothing behind


# ================================================================ REST: credentials, retries, safety

def test_the_token_is_sent_as_configured_and_never_exposed(client, me, api_srv):
    owner, ws = me
    cid = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv, _res(path="/secure", pagination={"type": "none"}), auth={"type": "bearer"}), TOKEN)
    job = _import(client, owner, cid, "orders")
    assert job["status"] == "succeeded" and STATE.requests[-1]["auth"] == f"Bearer {TOKEN}"
    ds_id = job["result"]["dataset_id"]
    with SessionLocal() as db:
        blob = json.dumps([job, _runs(client, owner, ds_id)]) + json.dumps([a.details for a in db.query(AuditLog)], default=str)
        blob += client._inner.get(f"/api/connections/{cid}", headers=owner["h"]).text + json.dumps([r.error_detail for r in db.query(IngestionRun)])
        assert TOKEN not in db.get(Connection, cid).secret_ciphertext
    assert TOKEN not in blob
    hdr = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv, _res(path="/plain", pagination={"type": "none"}, records_path=""),
                                                      auth={"type": "header", "header": "X-Api-Key"}), "k-123")
    assert client._inner.post(f"/api/connections/{hdr}/test", headers=owner["h"]).json()["ok"] is True
    assert STATE.requests[-1]["key"] == "k-123"


def test_a_wrong_or_missing_token_is_a_code(client, me, api_srv):
    owner, ws = me
    cid = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv, _res(path="/secure", pagination={"type": "none"}), auth={"type": "bearer"}), "wrong")
    t = client._inner.post(f"/api/connections/{cid}/test", headers=owner["h"]).json()
    assert t["ok"] is False and t["error_code"] == "AUTH_FAILED" and "wrong" not in json.dumps(t)
    nokey = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv, _res(path="/secure", pagination={"type": "none"}), auth={"type": "bearer"}))
    assert client._inner.post(f"/api/connections/{nokey}/test", headers=owner["h"]).json()["error_code"] == "AUTH_REQUIRED"


def test_moving_a_rest_connection_requires_the_secret_again(client, me, api_srv):
    owner, ws = me
    cid = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv, auth={"type": "bearer"}), TOKEN)
    patch = lambda **b: client._inner.patch(f"/api/connections/{cid}", headers=owner["h"], json=b)  # noqa: E731
    assert patch(config=_rest_cfg("http://127.0.0.1:9", auth={"type": "bearer"})).json()["error_code"] == "PASSWORD_REQUIRED"
    assert patch(config=_rest_cfg(api_srv, auth={"type": "header", "header": "X-Key"})).json()["error_code"] == "PASSWORD_REQUIRED"
    assert patch(config=_rest_cfg(api_srv, _res(name="other"), auth={"type": "bearer"})).status_code == 200      # resources may change freely


def test_transient_failures_are_retried_with_backoff_and_retry_after(client, me, api_srv, monkeypatch):
    import connectors.rest as rest_mod
    sleeps = []
    monkeypatch.setattr(rest_mod.time, "sleep", lambda s: sleeps.append(s))
    owner, ws = me
    STATE.flaky_left = 2
    cid = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv, _res(path="/flaky", pagination={"type": "none"})))
    t = client._inner.post(f"/api/connections/{cid}/test", headers=owner["h"]).json()
    assert t["ok"] is True and sleeps == [7.0, 7.0]                          # Retry-After honoured; the third attempt succeeded
    assert len([r for r in STATE.requests if r["path"] == "/flaky"]) == 3
    limited = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv, _res(path="/ratelimited", pagination={"type": "none"})))
    sleeps.clear()
    assert client._inner.post(f"/api/connections/{limited}/test", headers=owner["h"]).json()["error_code"] == "RATE_LIMITED"
    assert len(sleeps) == 2                                                  # bounded: three attempts, never an endless loop


@pytest.mark.parametrize("path, code", [("/redirect", "REDIRECT_REFUSED"), ("/html", "INVALID_RESPONSE"), ("/big", "RESPONSE_TOO_LARGE")])
def test_unsafe_or_unusable_responses_are_refused(client, me, api_srv, monkeypatch, path, code):
    monkeypatch.setattr(config, "SOURCE_MAX_RESPONSE_MB", 1)
    owner, ws = me
    cid = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv, _res(path=path, pagination={"type": "none"})))
    assert client._inner.post(f"/api/connections/{cid}/test", headers=owner["h"]).json()["error_code"] == code
    assert all("127.0.0.1:1" not in str(r["host"]) for r in STATE.requests)


def test_too_many_pages_stops_the_run(client, me, api_srv, monkeypatch):
    monkeypatch.setattr(config, "SOURCE_MAX_PAGES", 2)
    owner, ws = me
    cid = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv))
    assert _import(client, owner, cid, "orders")["status"] == "failed" and _failed_code(client, owner) == "TOO_MANY_PAGES"


def test_internal_addresses_are_refused_and_the_validated_address_is_the_one_used(client, me, api_srv, monkeypatch, server):
    owner, ws = me
    cid = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv))
    monkeypatch.setattr(config, "CONNECTOR_ALLOW_PRIVATE_HOSTS", False)
    t = client._inner.post(f"/api/connections/{cid}/test", headers=owner["h"]).json()
    assert t["ok"] is False and t["error_code"] == "HOST_NOT_ALLOWED"
    monkeypatch.setattr(config, "CONNECTOR_ALLOW_PRIVATE_HOSTS", True)
    # a host name is resolved once, then connected to by address, with the name kept for the Host header / TLS
    import socket
    real = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: real("127.0.0.1", *a, **k) if host == "pinned.test" else real(host, *a, **k))
    data, _ = httpclient.get_json(f"http://pinned.test:{server.server_address[1]}/plain", {})
    assert isinstance(data, list) and STATE.requests[-1]["host"] == f"pinned.test:{server.server_address[1]}"


# ================================================================ S3 (moto)

@pytest.fixture()
def s3(monkeypatch):
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "server-own-credentials-must-not-be-used")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
        client.create_bucket(Bucket="src-bucket")
        yield client


KEYID, SECRET = "AKIATESTKEY12345", "s3-secret-access-KEY-777"


def _s3_cfg(*resources, **over):
    return {"bucket": "src-bucket", "region": "us-east-1", "access_key_id": KEYID,
            "resources": list(resources) or [{"name": "events", "prefix": "exports/", "format": "csv"}], **over}


def _csv_obj(rows, cols="id,name,qty"):
    return (cols + "\n" + "\n".join(rows) + "\n").encode()


def _put(s3, key, body):
    s3.put_object(Bucket="src-bucket", Key=key, Body=body)


def _s3_rows(ds_id):
    con = sqlite3.connect(_curated_path(ds_id))
    try:
        return con.execute('SELECT source_object, id, name, qty FROM "events" ORDER BY source_object, id').fetchall()
    finally:
        con.close()


@pytest.mark.parametrize("bad", [
    {"bucket": "UPPER", "access_key_id": KEYID, "resources": [{"name": "a", "prefix": "", "format": "csv"}]},
    {"bucket": "src-bucket", "access_key_id": "x", "resources": [{"name": "a", "prefix": "", "format": "csv"}]},
    {"bucket": "src-bucket", "access_key_id": KEYID, "resources": [{"name": "a", "prefix": "/abs", "format": "csv"}]},
    {"bucket": "src-bucket", "access_key_id": KEYID, "resources": [{"name": "a", "prefix": "a/../b", "format": "csv"}]},
    {"bucket": "src-bucket", "access_key_id": KEYID, "resources": [{"name": "a", "prefix": "", "format": "xml"}]},
    {"bucket": "src-bucket", "access_key_id": KEYID, "resources": []},
    {"bucket": "src-bucket", "access_key_id": KEYID, "endpoint_url": "ftp://x.example.com", "resources": [{"name": "a", "prefix": "", "format": "csv"}]},
    {"bucket": "src-bucket", "access_key_id": KEYID, "resources": [{"name": "a", "prefix": "", "format": "csv"}], "secret": "x"},
])
def test_s3_config_validation(bad):
    with pytest.raises(ValueError):
        S3SourceConnector().validate_config(bad)


def test_s3_errors_become_codes():
    from botocore.exceptions import ClientError
    c = S3SourceConnector()
    err = lambda code: ClientError({"Error": {"Code": code, "Message": "secret details"}}, "ListObjectsV2")  # noqa: E731
    assert c.classify(err("InvalidAccessKeyId")).code == "AUTH_FAILED"
    assert c.classify(err("AccessDenied")).code == "PERMISSION_DENIED"
    assert c.classify(err("NoSuchBucket")).code == "BUCKET_NOT_FOUND"
    assert c.classify(err("SlowDown")).transient is True
    assert "secret details" not in c.classify(err("Weird")).message


def test_s3_credentials_are_explicit_and_the_endpoint_is_guarded(client, me, s3, monkeypatch):
    import boto3
    owner, ws = me
    seen = {}
    real = boto3.client
    monkeypatch.setattr(boto3, "client", lambda *a, **k: (seen.update(k), real(*a, **k))[1])
    cid = _mkconn(client, owner, ws, "s3", _s3_cfg(), SECRET)
    assert client._inner.post(f"/api/connections/{cid}/test", headers=owner["h"]).json()["ok"] is True
    assert seen["aws_access_key_id"] == KEYID and seen["aws_secret_access_key"] == SECRET        # never the server's own credentials
    nosecret = _mkconn(client, owner, ws, "s3", _s3_cfg())
    assert client._inner.post(f"/api/connections/{nosecret}/test", headers=owner["h"]).json()["error_code"] == "AUTH_REQUIRED"
    monkeypatch.setattr(config, "CONNECTOR_ALLOW_PRIVATE_HOSTS", False)
    meta = _mkconn(client, owner, ws, "s3", _s3_cfg(endpoint_url="https://169.254.169.254"), SECRET)
    assert client._inner.post(f"/api/connections/{meta}/test", headers=owner["h"]).json()["error_code"] == "HOST_NOT_ALLOWED"
    cfg = client._inner.get(f"/api/connections/{cid}", headers=owner["h"]).text
    assert SECRET not in cfg and KEYID not in cfg and "…" in cfg                                  # the key id is shown masked


def test_s3_full_import_tags_every_row_with_its_object(client, me, s3):
    owner, ws = me
    _put(s3, "exports/a.csv", _csv_obj(["1,x,5", "2,y,6"]))
    _put(s3, "exports/b.csv", _csv_obj(["3,z,7"]))
    _put(s3, "exports/notes.txt", b"ignored")
    _put(s3, "other/c.csv", _csv_obj(["9,no,9"]))
    cid = _mkconn(client, owner, ws, "s3", _s3_cfg(), SECRET)
    tables = client._inner.get(f"/api/connections/{cid}/schema", headers=owner["h"]).json()["tables"]
    assert tables[0]["name"] == "events" and tables[0]["incremental_ready"] is True and tables[0]["columns"][-1]["name"] == "source_modified"
    job = _import(client, owner, cid, "events")
    r = job["result"]
    assert job["status"] == "succeeded" and (r["records_read"], r["records_inserted"]) == (3, 3)
    ds_id = r["dataset_id"]
    assert _s3_rows(ds_id) == [("exports/a.csv", 1, "x", 5), ("exports/a.csv", 2, "y", 6), ("exports/b.csv", 3, "z", 7)]
    sess, sid = _open(client, owner, ds_id)
    assert _sql(client, owner, sid, "SELECT SUM(qty) AS s FROM events").json()["rows"][0]["s"] == 18
    lineage = sqlite3.connect(_curated_path(ds_id)).execute("SELECT source_type, connector, source_name FROM _qs_lineage").fetchall()
    assert lineage == [("object_storage", "s3", "s3://src-bucket/exports/")]


def test_s3_incremental_picks_up_only_new_changed_and_removed_objects(client, me, s3, monkeypatch):
    owner, ws = me
    _put(s3, "exports/a.csv", _csv_obj(["1,x,5", "2,y,6"]))
    _put(s3, "exports/b.csv", _csv_obj(["3,z,7"]))
    cid = _mkconn(client, owner, ws, "s3", _s3_cfg(), SECRET)
    first = _import(client, owner, cid, "events", "incremental")
    assert first["result"]["mode"] == "full_refresh" and first["result"]["fallback_reason"] is None
    ds_id = first["result"]["dataset_id"]
    with SessionLocal() as db:
        assert set(db.query(IngestionCheckpoint).filter_by(dataset_id=ds_id).one().state["objects"]) == {"exports/a.csv", "exports/b.csv"}
    reads = []
    real = S3SourceConnector.read_object
    monkeypatch.setattr(S3SourceConnector, "read_object", lambda self, cfg, sec, res, obj: (reads.append(obj["key"]), real(self, cfg, sec, res, obj))[1])
    _put(s3, "exports/c.csv", _csv_obj(["4,n,1", "5,m,2"]))                 # new
    _put(s3, "exports/a.csv", _csv_obj(["1,x,50"]))                          # changed (one row fewer, one value different)
    s3.delete_object(Bucket="src-bucket", Key="exports/b.csv")               # removed
    job = _sync(client, owner, ds_id)
    r = job["result"]
    assert job["status"] == "succeeded" and r["mode"] == "incremental" and r["fallback_reason"] is None
    assert sorted(reads) == ["exports/a.csv", "exports/c.csv"], "the unchanged-and-removed objects are not downloaded"
    assert (r["records_inserted"], r["records_updated"]) == (2, 1)
    assert _s3_rows(ds_id) == [("exports/a.csv", 1, "x", 50), ("exports/c.csv", 4, "n", 1), ("exports/c.csv", 5, "m", 2)]
    assert _runs(client, owner, ds_id)[0]["table_stats"]["events"]["deleted"] == 1
    # nothing changed: the run is recorded as skipped and no version is created
    with SessionLocal() as db:
        v = db.get(Dataset, ds_id).version
    reads.clear()
    again = _sync(client, owner, ds_id)
    assert again["status"] == "succeeded" and again["result"]["run_status"] == "skipped" and reads == []
    with SessionLocal() as db:
        assert db.get(Dataset, ds_id).version == v
    assert len(_s3_rows(ds_id)) == 3


def test_s3_a_changed_column_set_forces_a_full_refresh(client, me, s3):
    owner, ws = me
    _put(s3, "exports/a.csv", _csv_obj(["1,x,5"]))
    cid = _mkconn(client, owner, ws, "s3", _s3_cfg(), SECRET)
    ds_id = _import(client, owner, cid, "events", "incremental")["result"]["dataset_id"]
    _put(s3, "exports/a.csv", _csv_obj(["1,x,5,eu"], cols="id,name,qty,region"))
    res = _sync(client, owner, ds_id)["result"]
    assert (res["mode"], res["fallback_reason"]) == ("full_refresh", "schema_changed")
    run = _runs(client, owner, ds_id)[0]
    assert any(c["change"] == "column_added" and c["column"] == "region" for c in run["schema_changes"])
    con = sqlite3.connect(_curated_path(ds_id))
    assert con.execute('SELECT id, region FROM "events"').fetchall() == [(1, "eu")]
    _put(s3, "exports/b.csv", _csv_obj(["2,y,6,us"], cols="id,name,qty,region"))
    assert _sync(client, owner, ds_id)["result"]["mode"] == "incremental"


def test_s3_json_and_parquet_resources(client, me, s3):
    import pandas as pd
    owner, ws = me
    buf = io.BytesIO()
    pd.DataFrame({"id": [1, 2], "name": ["a", "b"], "qty": [3, 4]}).to_parquet(buf, index=False)
    _put(s3, "pq/p.parquet", buf.getvalue())
    _put(s3, "js/j.json", json.dumps([{"id": 7, "name": "j", "qty": 1}]).encode())
    cfg = _s3_cfg({"name": "events", "prefix": "pq/", "format": "parquet"}, {"name": "jevents", "prefix": "js/", "format": "json"})
    cid = _mkconn(client, owner, ws, "s3", cfg, SECRET)
    a = _import(client, owner, cid, "events")["result"]["dataset_id"]
    b = _import(client, owner, cid, "jevents")["result"]["dataset_id"]
    assert [r[1] for r in _s3_rows(a)] == [1, 2]
    assert sqlite3.connect(_curated_path(b)).execute('SELECT id, name FROM "jevents"').fetchall() == [(7, "j")]


def test_s3_limits_and_failures_are_codes(client, me, s3, monkeypatch):
    owner, ws = me
    for i in range(3):
        _put(s3, f"exports/o{i}.csv", _csv_obj([f"{i},x,1"]))
    cid = _mkconn(client, owner, ws, "s3", _s3_cfg(), SECRET)
    monkeypatch.setattr(config, "S3_SOURCE_MAX_OBJECTS", 2)
    assert client._inner.post(f"/api/connections/{cid}/test", headers=owner["h"]).json()["error_code"] == "TOO_MANY_OBJECTS"
    monkeypatch.setattr(config, "S3_SOURCE_MAX_OBJECTS", 200)
    _put(s3, "big/x.csv", b"id,pad\n" + b"".join(b"%d,%s\n" % (i, b"x" * 200) for i in range(6500)))
    big = _mkconn(client, owner, ws, "s3", _s3_cfg({"name": "big", "prefix": "big/", "format": "csv"}), SECRET)
    monkeypatch.setattr(config, "S3_SOURCE_MAX_OBJECT_MB", 1)
    refused = client._inner.post(f"/api/connections/{big}/datasets", headers=owner["h"], json={"table": "big"})     # caught while browsing
    assert refused.status_code == 422 and refused.json()["error_code"] == "OBJECT_TOO_LARGE"
    monkeypatch.setattr(config, "S3_SOURCE_MAX_OBJECT_MB", 10)
    monkeypatch.setattr(config, "S3_SOURCE_MAX_TOTAL_MB", 1)
    assert _import(client, owner, big, "big")["status"] == "failed" and _failed_code(client, owner) == "TOO_MUCH_DATA"
    _put(s3, "bad/x.csv", b"")
    bad = _mkconn(client, owner, ws, "s3", _s3_cfg({"name": "bad", "prefix": "bad/", "format": "csv"}), SECRET)
    unreadable = client._inner.post(f"/api/connections/{bad}/datasets", headers=owner["h"], json={"table": "bad"})
    assert unreadable.status_code == 422 and unreadable.json()["error_code"] == "INVALID_OBJECT"
    empty = _mkconn(client, owner, ws, "s3", _s3_cfg({"name": "nothing", "prefix": "none/", "format": "csv"}), SECRET)
    assert _import(client, owner, empty, "nothing")["status"] == "failed" and _failed_code(client, owner) == "EMPTY_SOURCE"
    with SessionLocal() as db:
        assert db.query(Dataset).filter(Dataset.created_by == owner["id"]).count() == 0


def test_s3_secret_never_reaches_runs_jobs_or_audit(client, me, s3):
    owner, ws = me
    _put(s3, "exports/a.csv", _csv_obj(["1,x,5"]))
    cid = _mkconn(client, owner, ws, "s3", _s3_cfg(), SECRET)
    job = _import(client, owner, cid, "events")
    with SessionLocal() as db:
        blob = json.dumps([job, _runs(client, owner, job["result"]["dataset_id"])]) + json.dumps([a.details for a in db.query(AuditLog)], default=str)
        assert SECRET not in db.get(Connection, cid).secret_ciphertext
    assert SECRET not in blob and KEYID not in blob


# ================================================================ scheduler

@pytest.fixture()
def sched(client, me, api_srv, monkeypatch):
    """A synced REST dataset plus a due schedule."""
    import connectors.rest as rest_mod
    monkeypatch.setattr(rest_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "INCREMENTAL_OVERLAP_SECONDS", 0)
    owner, ws = me
    cid = _mkconn(client, owner, ws, "rest", _rest_cfg(api_srv))
    ds_id = _import(client, owner, cid, "orders", "incremental")["result"]["dataset_id"]

    class S:
        pass
    s = S()
    s.owner, s.ws, s.cid, s.ds = owner, ws, cid, ds_id
    return s


def _due(ds_id, minutes_ago=1):
    with SessionLocal() as db:
        row = db.query(DatasetSchedule).filter_by(dataset_id=ds_id).one()
        row.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        db.commit()


def _put_schedule(client, owner, ds_id, minutes=15, mode="incremental"):
    return client._inner.put(f"/api/datasets/{ds_id}/schedule", headers=owner["h"], json={"interval_minutes": minutes, "mode": mode})


def _runs_count(ds_id):
    with SessionLocal() as db:
        return db.query(IngestionRun).filter_by(dataset_id=ds_id).count()


def test_schedule_api_validation_and_roles(client, sched, api_srv):
    s = sched
    assert _put_schedule(client, s.owner, s.ds, 1).status_code == 422                         # below the minimum interval
    assert _put_schedule(client, s.owner, s.ds, 60 * 24 * 8).status_code == 422
    assert _put_schedule(client, s.owner, s.ds, 15, "weird").status_code == 422
    r = _put_schedule(client, s.owner, s.ds, 15)
    body = r.json()["schedule"]
    assert r.status_code == 200 and body["interval_minutes"] == 15 and body["enabled"] and body["next_run_at"]
    assert client._inner.get(f"/api/datasets/{s.ds}/schedule", headers=s.owner["h"]).json()["schedule"]["interval_minutes"] == 15
    other = _user(client, "oth5")
    assert client._inner.get(f"/api/datasets/{s.ds}/schedule", headers=other["h"]).status_code == 404
    assert _put_schedule(client, other, s.ds).status_code == 404
    assert client._inner.delete(f"/api/datasets/{s.ds}/schedule", headers=other["h"]).status_code == 404
    up = client._inner.post("/api/datasets", headers=s.owner["h"], files={"file": ("a.csv", io.BytesIO(b"a\n1\n"), "text/csv")})
    file_ds = _job(client, s.owner, up.json()["job_id"])["result"]["dataset_id"]
    assert _put_schedule(client, s.owner, file_ds).json()["error_code"] == "NOT_A_CONNECTION_DATASET"
    assert client._inner.delete(f"/api/datasets/{s.ds}/schedule", headers=s.owner["h"]).json()["deleted"] is True
    assert client._inner.get(f"/api/datasets/{s.ds}/schedule", headers=s.owner["h"]).json()["schedule"] is None
    with SessionLocal() as db:
        acts = [a.action for a in db.query(AuditLog)]
    assert "schedule.set" in acts and "schedule.delete" in acts


def test_viewers_cannot_schedule_but_can_see_the_schedule(client, team5):
    ws_id, users, ds_id = team5
    assert _put_schedule(client, users["analyst"], ds_id).status_code == 200
    assert _put_schedule(client, users["viewer"], ds_id).status_code == 403
    assert client._inner.get(f"/api/datasets/{ds_id}/schedule", headers=users["viewer"]["h"]).status_code == 200
    assert client._inner.delete(f"/api/datasets/{ds_id}/schedule", headers=users["viewer"]["h"]).status_code == 403


@pytest.fixture()
def team5(client, api_srv):
    users = {r: _user(client, r) for r in ("owner", "analyst", "viewer")}
    ws_id = _team(client, users["owner"])
    for role in ("analyst", "viewer"):
        assert _add(client, users["owner"], ws_id, users[role], role).status_code == 201
    cid = _mkconn(client, users["owner"], ws_id, "rest", _rest_cfg(api_srv))
    ds_id = _import(client, users["owner"], cid, "orders")["result"]["dataset_id"]
    return ws_id, users, ds_id


def test_a_due_schedule_fires_once_then_waits_for_its_next_interval(client, sched):
    s = sched
    _put_schedule(client, s.owner, s.ds, 15)
    assert scheduler.tick() == 0                                  # not due yet
    STATE.records.append(State.rec(26))
    _due(s.ds)
    before = _runs_count(s.ds)
    assert scheduler.tick() == 1
    assert _runs_count(s.ds) == before + 1
    assert scheduler.tick() == 0 and _runs_count(s.ds) == before + 1                 # claimed: the next interval has not come
    with SessionLocal() as db:
        row = db.query(DatasetSchedule).filter_by(dataset_id=s.ds).one()
        nxt = row.next_run_at if row.next_run_at.tzinfo else row.next_run_at.replace(tzinfo=timezone.utc)
        assert row.last_status == "succeeded" and row.consecutive_failures == 0
        assert timedelta(minutes=14) < nxt - datetime.now(timezone.utc) <= timedelta(minutes=15)
    assert _rows(s.ds, "orders")[-1][0] == 26 and _runs(client, s.owner, s.ds)[0]["mode"] == "incremental"


def test_two_schedulers_cannot_fire_the_same_schedule_twice(client, sched):
    s = sched
    _put_schedule(client, s.owner, s.ds, 15)
    _due(s.ds)
    before = _runs_count(s.ds)
    barrier = threading.Barrier(2)
    fired = []

    def go():
        barrier.wait()
        try:
            fired.append(scheduler.tick())
        except Exception:  # noqa: BLE001 - a locked SQLite database is also "did not fire"
            fired.append(0)
    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(fired) == 1 and _runs_count(s.ds) == before + 1


def test_a_schedule_never_doubles_an_active_run(client, sched):
    s = sched
    _put_schedule(client, s.owner, s.ds, 15)
    with SessionLocal() as db:
        ingest.start_run(db, db.get(Dataset, s.ds), user_id=s.owner["id"], job_id="busy-" + uuid.uuid4().hex[:6], connector="rest",
                         source_name="x", source_bytes=0, fp="")
    _due(s.ds)
    before = _runs_count(s.ds)
    assert scheduler.tick() == 0 and _runs_count(s.ds) == before
    with SessionLocal() as db:
        assert db.query(DatasetSchedule).filter_by(dataset_id=s.ds).one().last_status == "skipped"


def test_after_downtime_a_schedule_fires_once_not_once_per_missed_interval(client, sched):
    s = sched
    _put_schedule(client, s.owner, s.ds, 15)
    _due(s.ds, minutes_ago=15 * 10)
    before = _runs_count(s.ds)
    assert scheduler.tick() == 1 and scheduler.tick() == 0
    assert _runs_count(s.ds) == before + 1


def test_repeated_failures_disable_the_schedule_and_say_why(client, sched, monkeypatch):
    s = sched
    monkeypatch.setattr(config, "SCHEDULER_MAX_CONSECUTIVE_FAILURES", 2)
    _put_schedule(client, s.owner, s.ds, 15)
    STATE.flaky_left = 10 ** 6
    with SessionLocal() as db:                                      # point the resource at the failing endpoint
        c = db.get(Connection, s.cid)
        cfg = json.loads(json.dumps(c.config))
        cfg["resources"][0]["path"] = "/flaky"
        c.config = cfg
        db.commit()
    for _ in range(2):
        _due(s.ds)
        scheduler.tick()
    with SessionLocal() as db:
        row = db.query(DatasetSchedule).filter_by(dataset_id=s.ds).one()
        assert (row.enabled, row.disabled_reason, row.consecutive_failures) == (False, "too_many_failures", 2)
        audit = [(a.action, a.actor_type, a.details) for a in db.query(AuditLog) if a.action == "schedule.disable"]
    assert audit and audit[0][1] == "system" and audit[0][2]["reason"] == "too_many_failures"
    _due_ok = scheduler.tick()
    assert _due_ok == 0                                             # a disabled schedule never fires


@pytest.mark.parametrize("breakage, reason", [("disable_connection", "connection_unavailable"), ("delete_connection", "connection_unavailable"),
                                              ("lose_access", "owner_access_lost")])
def test_a_schedule_stops_itself_when_it_may_no_longer_run(client, team5, breakage, reason):
    ws_id, users, ds_id = team5
    cid = client._inner.get(f"/api/datasets/{ds_id}/source", headers=users["owner"]["h"]).json()["source"]["connection"]["id"]
    assert _put_schedule(client, users["analyst"], ds_id).status_code == 200
    if breakage == "disable_connection":
        assert client._inner.post(f"/api/connections/{cid}/disable", headers=users["owner"]["h"]).status_code == 200
    elif breakage == "delete_connection":
        assert client._inner.delete(f"/api/connections/{cid}", headers=users["owner"]["h"]).status_code == 200
    else:
        assert client.delete(f"/api/workspaces/{ws_id}/members/{users['analyst']['id']}", headers=users["owner"]["h"]).status_code == 200
    _due(ds_id)
    before = _runs_count(ds_id)
    assert scheduler.tick() == 0 and _runs_count(ds_id) == before
    with SessionLocal() as db:
        row = db.query(DatasetSchedule).filter_by(dataset_id=ds_id).one()
        assert row.enabled is False and row.disabled_reason == reason


def test_schedules_survive_a_restart_and_the_loop_writes_its_heartbeat(client, sched, monkeypatch, tmp_path):
    s = sched
    _put_schedule(client, s.owner, s.ds, 15)
    beat = tmp_path / "alive"
    monkeypatch.setattr(scheduler, "HEARTBEAT_FILE", str(beat))
    stop = threading.Event()
    _due(s.ds)
    before = _runs_count(s.ds)
    t = threading.Thread(target=scheduler.run_forever, args=(stop, 0.05))
    t.start()
    for _ in range(100):
        if beat.exists() and _runs_count(s.ds) > before:
            break
        time.sleep(0.05)
    stop.set()
    t.join(5)
    assert beat.exists() and float(beat.read_text()) > 0 and _runs_count(s.ds) == before + 1     # state came from the database
    assert not t.is_alive()


def test_deleting_data_connections_and_workspaces_removes_schedules_checkpoints_and_secrets(client, team5):
    ws_id, users, ds_id = team5
    cid = client._inner.get(f"/api/datasets/{ds_id}/source", headers=users["owner"]["h"]).json()["source"]["connection"]["id"]
    assert _put_schedule(client, users["analyst"], ds_id).status_code == 200
    assert _sync(client, users["owner"], ds_id)["status"] == "succeeded"
    assert client.delete(f"/api/workspaces/{ws_id}", headers=users["owner"]["h"]).status_code == 200
    with SessionLocal() as db:
        assert db.get(Connection, cid) is None                                              # the encrypted secret is gone with it
        assert db.query(DatasetSchedule).filter_by(dataset_id=ds_id).count() == 0
        ds = db.get(Dataset, ds_id)
        assert ds is not None and ds.connection_id is None                                  # the data stays, detached
    other = _user(client, "x5")
    cid2 = _mkconn(client, other, client._inner.get("/api/workspaces", headers=other["h"]).json()["personal_workspace_id"], "rest",
                   _rest_cfg("http://127.0.0.1:9"))
    assert client._inner.get(f"/api/connections/{cid2}", headers=other["h"]).status_code == 200


def test_deleting_a_dataset_removes_its_checkpoint_and_schedule(client, sched):
    s = sched
    _put_schedule(client, s.owner, s.ds)
    assert client._inner.delete(f"/api/datasets/{s.ds}", headers=s.owner["h"]).status_code == 200
    with SessionLocal() as db:
        assert db.query(IngestionCheckpoint).filter_by(dataset_id=s.ds).count() == 0
        assert db.query(DatasetSchedule).filter_by(dataset_id=s.ds).count() == 0


def test_the_connector_listing_names_every_source_and_its_capabilities(client):
    u = _user(client, "lst")
    names = {c["name"]: c for c in client._inner.get("/api/connectors", headers=u["h"]).json()["connectors"]}
    assert {"rest", "s3"} <= set(names)
    assert names["rest"]["kind"] == "api" and names["s3"]["kind"] == "object_storage"
    assert all(names[n]["incremental"] and names[n]["requires_credentials"] for n in ("rest", "s3", "postgres", "mysql"))


def test_connection_errors_and_secrets_helpers_stay_consistent(monkeypatch):
    assert secrets_store.configured() and netguard.validate_port(443) == 443
    with pytest.raises(ConnectorError):
        httpclient.get_json("ftp://x/y", {})
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    with pytest.raises(ConnectorError) as exc:
        httpclient.get_json("http://example.com/x", {})
    assert exc.value.code == "TLS_REQUIRED"
    assert Job and ingest                                                     # imported for the fixtures above
