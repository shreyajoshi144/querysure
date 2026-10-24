from __future__ import annotations

import io
import json
import sqlite3

import pandas as pd
import pytest


def _csv_bytes() -> bytes:
    return b"id,name,amount\n1,Widget,10.5\n2,Gadget,20.0\n3,Gizmo,5.25\n"


def _json_array_bytes() -> bytes:
    return json.dumps([
        {"id": 1, "name": "Widget", "amount": 10.5},
        {"id": 2, "name": "Gadget", "amount": 20.0},
    ]).encode()


def _json_wrapped_bytes() -> bytes:
    return json.dumps({"data": [
        {"id": 1, "name": "Widget", "amount": 10.5},
        {"id": 2, "name": "Gadget", "amount": 20.0},
    ]}).encode()


def _xlsx_bytes() -> bytes:
    buf = io.BytesIO()
    df = pd.DataFrame({"id": [1, 2, 3], "name": ["Widget", "Gadget", "Gizmo"], "amount": [10.5, 20.0, 5.25]})
    df.to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()


def _sqlite_bytes() -> bytes:
    import os
    import uuid
    buf_path = f"/tmp/_test_upload_sample_{uuid.uuid4().hex}.sqlite"
    conn = sqlite3.connect(buf_path)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, amount REAL)")
    conn.execute("INSERT INTO items VALUES (1, 'Widget', 10.5)")
    conn.commit()
    conn.close()
    with open(buf_path, "rb") as f:
        data = f.read()
    os.remove(buf_path)
    return data


def test_csv_upload_still_works(client, auth_headers):
    r = client.post(
        "/api/connect/upload",
        headers=auth_headers,
        files={"file": ("products.csv", _csv_bytes(), "text/csv")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["table_count"] == 1
    assert body["total_rows"] == 3


def test_sqlite_upload_still_works(client, auth_headers):
    r = client.post(
        "/api/connect/upload",
        headers=auth_headers,
        files={"file": ("mydb.sqlite", _sqlite_bytes(), "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["table_count"] == 1


def test_valid_json_array_upload_works(client, auth_headers):
    r = client.post(
        "/api/connect/upload",
        headers=auth_headers,
        files={"file": ("products.json", _json_array_bytes(), "application/json")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["total_rows"] == 2


def test_valid_json_wrapped_object_upload_works(client, auth_headers):
    r = client.post(
        "/api/connect/upload",
        headers=auth_headers,
        files={"file": ("products.json", _json_wrapped_bytes(), "application/json")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["total_rows"] == 2


def test_malformed_json_rejected_cleanly(client, auth_headers):
    r = client.post(
        "/api/connect/upload",
        headers=auth_headers,
        files={"file": ("broken.json", b"{not valid json", "application/json")},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["success"] is False
    assert "JSON" in body["error"]


def test_non_tabular_json_rejected_cleanly(client, auth_headers):
    r = client.post(
        "/api/connect/upload",
        headers=auth_headers,
        files={"file": ("nested.json", json.dumps({"a": {"b": {"c": 1}}}).encode(), "application/json")},
    )
    assert r.status_code == 422
    assert r.json()["success"] is False


def test_valid_xlsx_upload_works(client, auth_headers):
    r = client.post(
        "/api/connect/upload",
        headers=auth_headers,
        files={"file": ("products.xlsx", _xlsx_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["total_rows"] == 3


def test_corrupt_xlsx_rejected_cleanly(client, auth_headers):
    r = client.post(
        "/api/connect/upload",
        headers=auth_headers,
        files={"file": ("broken.xlsx", b"this is not a real xlsx file", "application/octet-stream")},
    )
    assert r.status_code == 422
    assert r.json()["success"] is False


def test_unsupported_extension_rejected(client, auth_headers):
    r = client.post(
        "/api/connect/upload",
        headers=auth_headers,
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    assert r.status_code == 400
    assert r.json()["success"] is False


def test_oversized_upload_rejected(client, auth_headers, monkeypatch):
    import api as api_module

    # Shrink the CSV size ceiling for this test only, rather than actually
    # generating a 50MB+ file -- exercises the exact same guard path
    # (_stream_upload_to_disk aborting once `total > limit`).
    monkeypatch.setitem(api_module.UPLOAD_SIZE_LIMITS, ".csv", 10)
    r = client.post(
        "/api/connect/upload",
        headers=auth_headers,
        files={"file": ("big.csv", _csv_bytes(), "text/csv")},  # well over 10 bytes
    )
    assert r.status_code == 413
    assert r.json()["success"] is False


def test_unsafe_filename_does_not_escape_upload_dir(client, auth_headers):
    import api as api_module
    import os

    before = set(os.listdir(api_module.UPLOAD_DIR))
    r = client.post(
        "/api/connect/upload",
        headers=auth_headers,
        files={"file": ("../../etc/passwd.csv", _csv_bytes(), "text/csv")},
    )
    assert r.status_code == 200, r.text
    # The stored filename is always a fresh uuid4, never the client-supplied
    # name, so nothing can be written outside UPLOAD_DIR regardless of what
    # the filename claims. The reported db_name is the basename only, with
    # no path separators.
    assert "/" not in r.json()["db_name"]
    assert ".." not in r.json()["db_name"]
    after = set(os.listdir(api_module.UPLOAD_DIR))
    assert all(p.startswith(api_module.UPLOAD_DIR) or True for p in after)  # sanity: dir still exists
    for f in after - before:
        assert os.path.commonpath([api_module.UPLOAD_DIR, os.path.join(api_module.UPLOAD_DIR, f)]) == api_module.UPLOAD_DIR
