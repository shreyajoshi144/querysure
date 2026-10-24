"""Values read from a user's database must be strictly JSON-serializable.

Sample rows and query results are stored in JSON columns. Two failure modes the
rest of the suite could not see:

  * BLOB columns -> ``bytes`` -> TypeError (a 500 even on the dev SQLite DB).
  * NULLs in numeric columns -> pandas NaN, and SQLite +Infinity -> the tokens
    ``NaN`` / ``Infinity``. SQLite JSON storage accepts them, so every test on
    the dev database passed, but PostgreSQL's json/jsonb types reject them, so
    the same upload failed only in production.

``allow_nan=False`` reproduces PostgreSQL's strictness without needing Postgres.
"""
from __future__ import annotations

import datetime
import decimal
import json
import sqlite3

import pytest

from modules.json_safe import json_safe
from modules.query_result_engine import execute_query
from modules.schema_engine import load_database


def _strict(obj):
    return json.dumps(obj, allow_nan=False)


@pytest.fixture()
def messy_db(tmp_path):
    path = str(tmp_path / "messy.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v REAL, img BLOB, note TEXT)")
    conn.execute("INSERT INTO t VALUES (1, NULL, x'ff00', 'a')")
    conn.execute("INSERT INTO t VALUES (2, 1e999, NULL, NULL)")  # +Infinity
    conn.execute("INSERT INTO t VALUES (3, 2.5, x'00', 'c')")
    conn.commit()
    conn.close()
    return path


def test_schema_profile_is_strict_json_with_blob_null_and_inf(messy_db):
    schema = load_database(messy_db)
    _strict(schema)  # must not raise
    sample = schema["tables"]["t"]["sample_data"]
    assert sample[0]["v"] is None            # NULL (pandas NaN) -> None
    assert sample[1]["v"] is None            # +Infinity -> None
    assert sample[0]["img"] == "<binary 2 bytes>"


def test_query_result_is_strict_json_with_blob_and_inf(messy_db):
    r = execute_query("SELECT id, v, img FROM t ORDER BY id", messy_db)
    assert r["success"] and r["row_count"] == 3
    _strict(r)
    assert r["rows"][0]["img"] == "<binary 2 bytes>"
    assert r["rows"][1]["v"] is None
    assert r["rows"][2]["v"] == 2.5          # ordinary values are untouched


@pytest.mark.parametrize("raw, expected", [
    (float("nan"), None),
    (float("inf"), None),
    (float("-inf"), None),
    (b"abc", "<binary 3 bytes>"),
    (bytearray(b"ab"), "<binary 2 bytes>"),
    (decimal.Decimal("1.5"), 1.5),
    (datetime.date(2024, 1, 2), "2024-01-02"),
    (datetime.datetime(2024, 1, 2, 3, 4, 5), "2024-01-02T03:04:05"),
    (True, True),
    (7, 7),
    (2.5, 2.5),
    ("text", "text"),
    (None, None),
])
def test_json_safe_scalars(raw, expected):
    got = json_safe(raw)
    assert got == expected and type(got) is type(expected)
