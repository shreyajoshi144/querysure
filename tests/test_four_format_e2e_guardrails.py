from __future__ import annotations

from tests.auth_test_utils import verify_registered_email

import io
import json
import sqlite3
import uuid

import openpyxl
import pandas as pd
import pytest


def _register(client):
    email = f"four-format-{uuid.uuid4().hex[:10]}@example.com"
    pw = "TestPassword123!"
    assert client.post("/api/auth/register", json={"email": email, "password": pw}).status_code == 200
    verify_registered_email(client, email)
    r = client.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _xlsx_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()


def _xlsm_bytes(df: pd.DataFrame) -> bytes:
    # openpyxl can read an xlsm container even when no VBA project is present;
    # this exercises the .xlsm ingestion route without introducing a new format.
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()


def _sqlite_bytes(table: str, df: pd.DataFrame, path_suffix: str = ".sqlite3") -> bytes:
    path = f"/tmp/qs-{uuid.uuid4().hex}{path_suffix}"
    conn = sqlite3.connect(path)
    df.to_sql(table, conn, index=False)
    conn.close()
    with open(path, "rb") as f:
        data = f.read()
    import os
    os.remove(path)
    return data


FORMATS = [
    ("csv", "alpha.csv", "text/csv"),
    ("json", "beta.json", "application/json"),
    ("xlsx", "gamma.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("xlsm", "delta.xlsm", "application/vnd.ms-excel.sheet.macroEnabled.12"),
    ("sqlite", "epsilon.sqlite3", "application/octet-stream"),
]


def _payload(kind: str):
    if kind == "csv":
        return b"alpha_id,alpha_value\n1,10.5\n2,20.0\n3,30.5\n"
    if kind == "json":
        return json.dumps([{"beta_id": 11, "beta_region": "N", "beta_amount": 7.5}, {"beta_id": 12, "beta_region": "S", "beta_amount": 9.5}]).encode()
    if kind == "xlsx":
        return _xlsx_bytes(pd.DataFrame({"gamma_id": [21, 22, 23], "gamma_name": ["a", "b", "c"], "gamma_value": [1.0, 2.0, 3.0]}))
    if kind == "xlsm":
        return _xlsm_bytes(pd.DataFrame({"delta_id": [31, 32], "delta_name": ["x", "y"], "delta_value": [4.0, 5.0]}))
    return _sqlite_bytes("epsilon_data", pd.DataFrame({"epsilon_id": [41, 42, 43, 44], "epsilon_amount": [2.0, 4.0, 6.0, 8.0]}))


def _upload(client, headers, kind, filename, session_id=None):
    data = {"session_id": session_id} if session_id else None
    r = client.post("/api/connect/upload", headers=headers, data=data,
                    files={"file": (filename, _payload(kind), "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def _schema(client, headers, sid):
    r = client.post("/api/schema-intelligence", headers=headers, json={"session_id": sid})
    assert r.status_code == 200, r.text
    return r.json()


def _retrieve(client, headers, sid, question):
    r = client.post("/api/retrieve-tables", headers=headers,
                    json={"session_id": sid, "question": question})
    assert r.status_code == 200, r.text
    return r.json()


def _execute(client, headers, sid, sql):
    return client.post("/api/execute-sql", headers=headers,
                       json={"session_id": sid, "sql": sql})


def test_all_four_formats_have_correct_schema_retrieval_and_guardrails(client):
    headers = _register(client)
    cases = [
        ("csv", "alpha.csv", "alpha_id", "alpha_value", 3, "SELECT alpha_id, alpha_value FROM alpha"),
        ("json", "beta.json", "beta_id", "beta_amount", 2, "SELECT beta_id, beta_amount FROM beta"),
        ("xlsx", "gamma.xlsx", "gamma_id", "gamma_value", 3, "SELECT gamma_id, gamma_value FROM gamma"),
        ("xlsm", "delta.xlsm", "delta_id", "delta_value", 2, "SELECT delta_id, delta_value FROM delta"),
        ("sqlite", "epsilon.sqlite3", "epsilon_id", "epsilon_amount", 4, "SELECT epsilon_id, epsilon_amount FROM epsilon_data"),
    ]
    for kind, filename, id_col, value_col, rows, good_sql in cases:
        body = _upload(client, headers, kind, filename)
        sid = body["session_id"]
        assert body["table_count"] == 1
        assert body["total_rows"] == rows
        schema = _schema(client, headers, sid)
        assert schema["table_profiles"][0]["row_count"] == rows
        table = schema["table_profiles"][0]["table"]
        assert table

        retrieval = _retrieve(client, headers, sid, f"show {value_col} by {id_col}")
        blob = json.dumps(retrieval).lower()
        assert id_col.lower() in blob and value_col.lower() in blob
        assert "unnamed:" not in blob

        good = _execute(client, headers, sid, good_sql)
        assert good.status_code == 200, good.text
        assert good.json()["success"] is True
        assert good.json()["row_count"] == rows

        bad = _execute(client, headers, sid, f"SELECT does_not_exist FROM {table}")
        assert bad.status_code == 422
        assert bad.json()["success"] is False

        for sql in [
            f"INSERT INTO {table} ({id_col}) VALUES (999)",
            f"UPDATE {table} SET {value_col}=0",
            f"DELETE FROM {table}",
            f"DROP TABLE {table}",
            f"ALTER TABLE {table} ADD COLUMN hacked TEXT",
            f"SELECT {id_col} FROM {table}; DROP TABLE {table};",
        ]:
            r = _execute(client, headers, sid, sql)
            assert r.status_code == 422, (kind, sql, r.text)


def test_excel_blank_headers_do_not_become_unnamed_columns(client):
    headers = _register(client)
    buf = io.BytesIO()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["id", None, "amount"])
    ws.append([1, "x", 10])
    ws.append([2, "y", 20])
    wb.save(buf)
    r = client.post("/api/connect/upload", headers=headers,
                    files={"file": ("headers.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    schema = _schema(client, headers, sid)
    # The blank source header is retained as deterministic data, not exposed as
    # pandas' parser artifact.
    assert _retrieve(client, headers, sid, "show all columns")["success"] is True
    r2 = client.post("/api/schema-intelligence", headers=headers, json={"session_id": sid})
    assert "Unnamed:" not in r2.text


def test_json_with_multiple_top_level_arrays_is_rejected(client):
    headers = _register(client)
    payload = json.dumps({"rows": [{"a": 1}], "metadata": [{"b": 2}]}).encode()
    r = client.post("/api/connect/upload", headers=headers,
                    files={"file": ("ambiguous.json", payload, "application/json")})
    assert r.status_code == 422
    assert "Ambiguous JSON structure" in r.json()["error"]


def test_dataset_a_b_c_d_current_binding_and_flush(client):
    headers = _register(client)
    sid = _upload(client, headers, "csv", "a.csv")["session_id"]
    assert "alpha" in json.dumps(_retrieve(client, headers, sid, "alpha value")).lower()

    transitions = [
        ("json", "b.json", "beta_id", "beta_amount"),
        ("xlsx", "c.xlsx", "gamma_id", "gamma_value"),
        ("sqlite", "d.sqlite3", "epsilon_id", "epsilon_amount"),
    ]
    prior_columns = []
    for kind, filename, current_id, current_value in transitions:
        body = _upload(client, headers, kind, filename, session_id=sid)
        assert body["session_id"] == sid
        current = _retrieve(client, headers, sid, f"show {current_value} and {current_id}")
        blob = json.dumps(current).lower()
        assert current_id in blob and current_value in blob
        for stale_id, stale_value in prior_columns:
            assert stale_id not in blob and stale_value not in blob
        prior_columns.append((current_id, current_value))

    # Final dataset is SQLite D; its table/column are the only valid query target.
    good = _execute(client, headers, sid, "SELECT epsilon_id, epsilon_amount FROM epsilon_data")
    assert good.status_code == 200 and good.json()["row_count"] == 4
    stale = _execute(client, headers, sid, "SELECT alpha_id FROM alpha")
    assert stale.status_code == 422

    # Flush must clear dataset + all derived pipeline state for the active session.
    flushed = client.post("/api/session/flush", headers=headers, json={"session_id": sid})
    assert flushed.status_code == 200
    assert flushed.json()["flushed"] is True
    no_schema = client.post("/api/schema-intelligence", headers=headers, json={"session_id": sid})
    assert no_schema.status_code == 400 and no_schema.json()["success"] is False
    no_db = _execute(client, headers, sid, "SELECT epsilon_id FROM epsilon_data")
    assert no_db.status_code == 400
    assert no_db.json().get("error_code") == "NO_DATABASE"
    trace = client.get("/api/answer-trace", headers=headers, params={"session_id": sid})
    assert trace.status_code == 200
    assert "epsilon" not in trace.text.lower()


def test_csv_delimiter_duplicate_headers_and_encoding(client):
    headers = _register(client)
    payload = "id;name;name\n1;Jos\xe9;Paris\n2;Ana;Delhi\n".encode("cp1252")
    r = client.post("/api/connect/upload", headers=headers, files={"file": ("dup.csv", payload, "text/csv")})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    schema = _schema(client, headers, sid)
    cols = [c["name"] for c in schema["table_profiles"][0].get("columns", [])]
    assert "unnamed:" not in r.text.lower()
    # Schema endpoint exposes normalized columns through semantic preview.
    sem = client.post("/api/semantic-layer", headers=headers, json={"session_id": sid})
    assert sem.status_code == 200
    # pandas renames the repeated header to name.1 and normalisation makes that
    # name_1: all three source columns survive with unique names and their data.
    tbl = next(iter(sem.json()["tables"].values()))
    assert [c["column"] for c in tbl["columns"]] == ["id", "name", "name_1"]
    got = _execute(client, headers, sid, "SELECT name, name_1 FROM dup ORDER BY id")
    assert got.status_code == 200
    assert [(r["name"], r["name_1"]) for r in got.json()["rows"]] == [("José", "Paris"), ("Ana", "Delhi")]


def test_cross_user_session_id_cannot_execute_other_users_dataset(client):
    user_a = _register(client)
    user_b = _register(client)
    sid_a = _upload(client, user_a, "csv", "alpha.csv")["session_id"]
    # Sprint 2: a foreign session ID is indistinguishable from a missing one -> 404, never A's data.
    r = _execute(client, user_b, sid_a, "SELECT alpha_id FROM alpha")
    assert r.status_code == 404
    assert r.json().get("error_code") == "SESSION_NOT_FOUND"

@pytest.mark.parametrize("filename,payload", [
    ("empty.csv", b""),
    ("bad.json", b"{not-json"),
    ("bad.xlsx", b"not an excel workbook"),
    ("bad.sqlite3", b"not sqlite"),
])
def test_malformed_supported_files_are_rejected(client, filename, payload):
    headers = _register(client)
    r = client.post("/api/connect/upload", headers=headers,
                    files={"file": (filename, payload, "application/octet-stream")})
    assert r.status_code == 422, (filename, r.text)


def test_wrong_extension_is_rejected(client):
    headers = _register(client)
    r = client.post("/api/connect/upload", headers=headers,
                    files={"file": ("sales.txt", b"a,b\n1,2\n", "text/plain")})
    assert r.status_code == 400
