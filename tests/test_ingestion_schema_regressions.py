"""Regression tests for the file-ingestion / schema-extraction layer.

Scope: what an uploaded CSV / JSON / Excel / SQLite file turns into -- the
column names, the rows, and the schema QuerySure exposes about them. Each test
names the defect it pins and goes through the real ingestion code (or the real
upload endpoint), so it fails if the defect comes back.

Part A pins defects that silently corrupted or dropped source data.
Part B is an explicit rows-and-columns round trip for every supported format.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import uuid
import zipfile

import openpyxl
import pytest

import api as api_module
from modules.ingestion import ingest_to_sqlite
from modules.schema_engine import load_database
from tests.auth_test_utils import verify_registered_email


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _user(client):
    email = f"ingest-{uuid.uuid4().hex[:10]}@example.com"
    pw = "TestPassword123!"
    assert client.post("/api/auth/register", json={"email": email, "password": pw}).status_code == 200
    verify_registered_email(client, email)
    tok = client.post("/api/auth/login", json={"email": email, "password": pw}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def _upload(client, headers, name, data, session_id=None):
    return client.post("/api/connect/upload", headers=headers,
                       data={"session_id": session_id} if session_id else None,
                       files={"file": (name, data, "application/octet-stream")})


def _ok(client, headers, name, data):
    r = _upload(client, headers, name, data)
    assert r.status_code == 200, r.text
    return r.json()


def _sql(client, headers, sid, sql):
    return client.post("/api/execute-sql", headers=headers, json={"session_id": sid, "sql": sql})


def _xlsx(rows=None, sheets=None) -> bytes:
    wb = openpyxl.Workbook()
    if sheets:
        wb.remove(wb.active)
        for title, srows in sheets.items():
            ws = wb.create_sheet(title)
            for r in srows:
                ws.append(r)
    else:
        for r in rows:
            wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _xlsx_with_cached_formulas() -> bytes:
    """A workbook as Excel itself saves it: each formula carries its result.

    openpyxl cannot write cached results, so patch them into the sheet XML.
    """
    raw = _xlsx([["a", "b", "total"], [1, 2, "=A2+B2"], [3, 4, "=A3+B3"]])
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                xml = data.decode()
                for formula, value in (("A2+B2", "3"), ("A3+B3", "7")):
                    for empty in ("<v></v>", "<v />"):
                        xml = xml.replace(f"<f>{formula}</f>{empty}", f"<f>{formula}</f><v>{value}</v>")
                assert "<v>3</v>" in xml and "<v>7</v>" in xml, "failed to inject cached formula results"
                data = xml.encode()
            zout.writestr(item, data)
    return out.getvalue()


def _sqlite_file(tmp_path, builder) -> bytes:
    p = tmp_path / f"{uuid.uuid4().hex}.db"
    con = sqlite3.connect(p)
    try:
        builder(con)
        con.commit()
    finally:
        con.close()
    return p.read_bytes()


# ===========================================================================
# PART A -- defects that silently corrupted or dropped source data
# ===========================================================================

# --- CSV --------------------------------------------------------------------
def test_csv_row_with_extra_field_is_rejected_not_silently_shifted(client):
    """`a,b / 1,2,3` used to load as (a=2, b=3): pandas swallowed the `1` as an
    implicit index and shifted every value one column left."""
    r = _upload(client, _user(client), "r.csv", b"a,b\n1,2,3\n4,5\n")
    assert r.status_code == 422 and "row 2" in r.json()["error"].lower()


def test_csv_leading_zeros_survive_ingestion(client):
    h = _user(client)
    body = _ok(client, h, "zips.csv", b"zip,n\n00123,1\n02134,2\n")
    got = _sql(client, h, body["session_id"], "SELECT zip, n FROM zips ORDER BY n")
    assert got.status_code == 200
    assert [r["zip"] for r in got.json()["rows"]] == ["00123", "02134"]


def test_csv_signed_leading_zero_values_survive(client):
    """`+0123` (phone-style) used to collapse to the integer 123."""
    h = _user(client)
    body = _ok(client, h, "ph.csv", b"phone,n\n+0123,1\n+0456,2\n")
    got = _sql(client, h, body["session_id"], "SELECT phone FROM ph ORDER BY n")
    assert [r["phone"] for r in got.json()["rows"]] == ["+0123", "+0456"]


def test_csv_ordinary_numbers_stay_numeric(client):
    """The leading-zero guard must not turn normal numeric columns into text."""
    h = _user(client)
    body = _ok(client, h, "num.csv", b"id,price,ratio\n1,10.5,0.25\n2,20,0.5\n")
    got = _sql(client, h, body["session_id"], "SELECT SUM(price) AS s, typeof(id) AS t FROM num")
    row = got.json()["rows"][0]
    assert row["s"] == 30.5 and row["t"] == "integer"


def test_csv_trailing_delimiter_does_not_create_a_false_column(client):
    body = _ok(client, _user(client), "tr.csv", b"id,name,\n1,a,\n2,b,\n")
    assert [c["name"] for c in body["tables"][0]["columns"]] == ["id", "name"]


def test_csv_column_that_has_data_but_no_header_is_kept_and_named(client):
    body = _ok(client, _user(client), "nh.csv", b"id,name,\n1,a,x\n2,b,\n")
    assert [c["name"] for c in body["tables"][0]["columns"]] == ["id", "name", "column_3"]


# --- JSON -------------------------------------------------------------------
def test_json_column_that_is_null_in_every_record_is_still_a_column(client):
    """Only a *phantom* (unnamed + empty) column is dropped; a real named
    column with no values yet is part of the table's structure."""
    payload = json.dumps([{"a": 1, "b": None}, {"a": 2, "b": None}]).encode()
    body = _ok(client, _user(client), "nulls.json", payload)
    assert [c["name"] for c in body["tables"][0]["columns"]] == ["a", "b"]
    assert body["tables"][0]["rows"] == 2


# --- Excel ------------------------------------------------------------------
def test_excel_text_cells_with_leading_zeros_survive(client):
    h = _user(client)
    body = _ok(client, h, "x.xlsx", _xlsx([["zip", "n"], ["00123", 1], ["02134", 2]]))
    got = _sql(client, h, body["session_id"], "SELECT zip FROM x ORDER BY n")
    assert [r["zip"] for r in got.json()["rows"]] == ["00123", "02134"]


def test_excel_with_never_calculated_formulas_is_rejected_not_loaded_as_empty(client):
    """A program-generated workbook has formulas but no cached results; every
    formula column used to load as NULL with no error."""
    r = _upload(client, _user(client), "f.xlsx", _xlsx([["a", "b", "total"], [1, 2, "=A2+B2"], [3, 4, "=A3+B3"]]))
    assert r.status_code == 422 and "formula" in r.json()["error"].lower()


def test_excel_saved_by_excel_with_cached_formula_results_still_loads(client):
    """Control for the guard above: real Excel-saved files must NOT be rejected,
    and the formula results must come through as data."""
    h = _user(client)
    body = _ok(client, h, "calc.xlsx", _xlsx_with_cached_formulas())
    got = _sql(client, h, body["session_id"], "SELECT a, b, total FROM calc ORDER BY a")
    assert [(r["a"], r["b"], r["total"]) for r in got.json()["rows"]] == [(1, 2, 3), (3, 4, 7)]


def test_excel_without_formulas_is_unaffected_by_the_formula_guard(client):
    body = _ok(client, _user(client), "ok.xlsx", _xlsx([["a", "b"], [1, 2.5], [3, 4.5]]))
    assert body["tables"][0]["rows"] == 2


def test_excel_empty_unheaded_column_is_not_exposed_as_a_field(client):
    body = _ok(client, _user(client), "gap.xlsx",
               _xlsx([["id", "name", None, "x"], [1, "a", None, 5], [2, "b", None, 6]]))
    assert [c["name"] for c in body["tables"][0]["columns"]] == ["id", "name", "x"]


# --- SQLite -----------------------------------------------------------------
def test_sqlite_identifier_with_double_quote_is_profiled_safely(client, tmp_path):
    """Table/column names were interpolated into SQL unescaped: a `"` in either
    broke profiling (and in a hostile upload, could break out of the quoting)."""
    def build(con):
        con.execute('CREATE TABLE "we""ird"("co""l" INTEGER, ok TEXT)')
        con.execute('INSERT INTO "we""ird" VALUES (1, \'x\')')
    body = _ok(client, _user(client), "q.db", _sqlite_file(tmp_path, build))
    t = body["tables"][0]
    assert t["name"] == 'we"ird' and t["rows"] == 1
    assert [c["name"] for c in t["columns"]] == ['co"l', "ok"]


def test_sqlite_with_too_many_columns_is_rejected_with_422(client, tmp_path):
    from modules.schema_engine import MAX_COLUMNS_PER_TABLE
    cols = ", ".join(f"c{i} INTEGER" for i in range(MAX_COLUMNS_PER_TABLE + 1))
    before = set(os.listdir(api_module.UPLOAD_DIR))
    r = _upload(client, _user(client), "wide.db",
                _sqlite_file(tmp_path, lambda con: con.execute(f"CREATE TABLE wide({cols})")))
    assert r.status_code == 422 and "columns" in r.json()["error"]
    assert set(os.listdir(api_module.UPLOAD_DIR)) == before


def test_sqlite_with_too_many_tables_is_rejected_with_422(client, tmp_path):
    from modules.schema_engine import MAX_TABLES

    def build(con):
        for i in range(MAX_TABLES + 1):
            con.execute(f"CREATE TABLE t{i}(a INTEGER)")
    before = set(os.listdir(api_module.UPLOAD_DIR))
    r = _upload(client, _user(client), "many.db", _sqlite_file(tmp_path, build))
    assert r.status_code == 422 and "tables" in r.json()["error"]
    assert set(os.listdir(api_module.UPLOAD_DIR)) == before


@pytest.mark.parametrize("name,payload", [
    ("fake.db", lambda tp: b"not a sqlite database at all " * 40),
    ("zero_tables.sqlite", lambda tp: _sqlite_file(tp, lambda con: None)),
    ("views_only.sqlite3", lambda tp: _sqlite_file(tp, lambda con: con.execute("CREATE VIEW v AS SELECT 1 AS x"))),
])
def test_unusable_sqlite_uploads_are_422_and_leave_no_file(client, tmp_path, name, payload):
    before = set(os.listdir(api_module.UPLOAD_DIR))
    r = _upload(client, _user(client), name, payload(tmp_path))
    assert r.status_code == 422 and r.json()["success"] is False
    assert set(os.listdir(api_module.UPLOAD_DIR)) == before


def test_profiling_opens_uploaded_database_read_only(tmp_path):
    p = tmp_path / "ro.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE t(a INTEGER)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()
    before = p.read_bytes()
    load_database(str(p))
    assert p.read_bytes() == before


# --- Upload endpoint --------------------------------------------------------
def test_conversion_and_profiling_run_off_the_event_loop(client, monkeypatch):
    """pandas/openpyxl/profiling are CPU+I/O heavy; run on the event loop, one
    large upload froze every other user's requests. A worker thread has no
    running loop, so that is what we assert."""
    seen: dict[str, bool] = {}

    def on_loop() -> bool:
        try:
            asyncio.get_running_loop()
            return True
        except RuntimeError:
            return False

    real_ingest, real_load = api_module.ingest_to_sqlite, api_module.load_database

    def spy_ingest(*a, **k):
        seen["ingest_on_loop"] = on_loop()
        return real_ingest(*a, **k)

    def spy_load(*a, **k):
        seen["load_on_loop"] = on_loop()
        return real_load(*a, **k)

    import jobs as jobs_module
    monkeypatch.setattr(jobs_module, "ingest_to_sqlite", spy_ingest)
    monkeypatch.setattr(jobs_module, "load_database", spy_load)
    _ok(client, _user(client), "t.csv", b"a,b\n1,2\n")
    assert seen == {"ingest_on_loop": False, "load_on_loop": False}


# ===========================================================================
# PART B -- explicit rows + columns round trip, every supported format
# ===========================================================================
def _stored_rows(db_path: str, table: str):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
    finally:
        con.close()


def _ingest_and_profile(tmp_path, filename: str, payload: bytes):
    src = tmp_path / filename
    src.write_bytes(payload)
    ext = src.suffix.lower()
    if ext in {".db", ".sqlite", ".sqlite3"}:
        dest = str(src)
    else:
        dest = str(tmp_path / f"{src.stem}.out.db")
    ingest_to_sqlite(str(src), ext, dest, src.stem)
    return load_database(dest), dest


def _sq_orders(con):
    con.execute("CREATE TABLE cust(id INTEGER PRIMARY KEY, name TEXT)")
    con.execute("CREATE TABLE ord(id INTEGER PRIMARY KEY, cust_id INTEGER REFERENCES cust(id), amt REAL)")
    con.executemany("INSERT INTO cust VALUES (?,?)", [(1, "Asha"), (2, "Ravi")])
    con.executemany("INSERT INTO ord VALUES (?,?,?)", [(1, 1, 9.5), (2, 2, 3.0), (3, 1, 1.0)])


ROUNDTRIP_CASES = {
    "csv_messy_headers": (
        "orders.csv",
        lambda tp: b"Order ID,Unit Price ($),  Ship Date \n1,2.5,2024-01-01\n2,4.0,2024-02-01\n",
        {"orders": (["order_id", "unit_price", "ship_date"],
                    [(1, 2.5, "2024-01-01"), (2, 4.0, "2024-02-01")])},
    ),
    "csv_semicolon_cp1252_duplicate_headers": (
        "dup.csv",
        lambda tp: "id;name;name\n1;Jos\xe9;Paris\n2;Ana;Delhi\n".encode("cp1252"),
        {"dup": (["id", "name", "name_1"], [(1, "José", "Paris"), (2, "Ana", "Delhi")])},
    ),
    "json_records_with_null": (
        "people.json",
        lambda tp: json.dumps([{"id": 1, "city": "Pune", "note": None},
                               {"id": 2, "city": "Indore", "note": "x"}]).encode(),
        {"people": (["id", "city", "note"], [(1, "Pune", None), (2, "Indore", "x")])},
    ),
    "json_nested_value_kept_as_text": (
        "n.json",
        lambda tp: json.dumps([{"id": 1, "addr": {"city": "X"}}]).encode(),
        {"n": (["id", "addr"], [(1, '{"city":"X"}')])},
    ),
    "xlsx_title_rows_above_header": (
        "report.xlsx",
        lambda tp: _xlsx([["Quarterly Report", None], [None, None], ["id", "name"], [1, "a"], [2, "b"]]),
        {"report": (["id", "name"], [(1, "a"), (2, "b")])},
    ),
    "xlsx_every_sheet_becomes_its_own_table": (
        "book.xlsx",
        lambda tp: _xlsx(sheets={"Sales": [["id", "v"], [1, 2], [2, 3]], "Costs": [["id", "c"], [1, 5]]}),
        {"book_sales": (["id", "v"], [(1, 2), (2, 3)]), "book_costs": (["id", "c"], [(1, 5)])},
    ),
    "sqlite_multi_table_untouched": (
        "shop.sqlite3",
        lambda tp: _sqlite_file(tp, _sq_orders),
        {"cust": (["id", "name"], [(1, "Asha"), (2, "Ravi")]),
         "ord": (["id", "cust_id", "amt"], [(1, 1, 9.5), (2, 2, 3.0), (3, 1, 1.0)])},
    ),
}


@pytest.mark.parametrize("case", list(ROUNDTRIP_CASES))
def test_every_format_preserves_columns_rows_and_exposes_them_in_the_schema(case, tmp_path):
    filename, make_payload, expected = ROUNDTRIP_CASES[case]
    schema, db_path = _ingest_and_profile(tmp_path, filename, make_payload(tmp_path))

    # Exactly the expected tables -- nothing dropped, nothing invented.
    assert sorted(schema["tables"]) == sorted(expected)
    assert schema["table_count"] == len(expected)

    for table, (want_cols, want_rows) in expected.items():
        info = schema["tables"][table]
        # Exposed schema: column names, in source order, and the row count.
        assert [c["name"] for c in info["columns"]] == want_cols
        assert info["row_count"] == len(want_rows)
        # Exposed per-column stats cover every column (no silent gaps).
        assert set(info["column_stats"]) == set(want_cols)
        # The stored data itself: every value, in source order.
        assert _stored_rows(db_path, table) == want_rows
        # And the preview rows the UI/LLM sees agree with what is stored.
        for sample, stored in zip(info["sample_data"], want_rows):
            assert [sample[c] for c in want_cols] == list(stored)


def test_sqlite_foreign_keys_are_exposed_as_relationships(tmp_path):
    schema, _ = _ingest_and_profile(tmp_path, "shop.sqlite3", _sqlite_file(tmp_path, _sq_orders))
    assert schema["relationships"] == [
        {"from_table": "ord", "from_col": "cust_id", "to_table": "cust", "to_col": "id"}
    ]
