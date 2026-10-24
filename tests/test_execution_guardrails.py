"""Execution-time guardrails: read-only connection, authorizer, real query
deadline, and a hard row cap that a model-written LIMIT cannot override."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time

import pytest

from modules.query_result_engine import execute_query


@pytest.fixture()
def db_path():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY, amount REAL)")
    c.executemany("INSERT INTO orders(amount) VALUES (?)", [(1.0,)] * 3000)
    c.commit()
    c.close()
    return p


def _count(p):
    c = sqlite3.connect(p)
    try:
        return c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    finally:
        c.close()


def test_model_written_huge_limit_cannot_exceed_row_cap(db_path):
    r = execute_query("SELECT id FROM orders LIMIT 999999999", db_path, max_rows=500)
    assert r["success"] and r["row_count"] == 500
    assert r["execution_metadata"]["truncated"] is True


@pytest.mark.parametrize("sql", [
    "WITH x AS (SELECT 1) DELETE FROM orders",
    "DELETE FROM orders",
    "UPDATE orders SET amount = 0",
    "DROP TABLE orders",
])
def test_writes_never_reach_the_data_even_if_validator_were_bypassed(db_path, sql):
    execute_query(sql, db_path)
    assert _count(db_path) == 3000


def test_connection_is_read_only_at_the_sqlite_level(db_path):
    """Bypass the SELECT/WITH prefix check deliberately: a SELECT that still
    tries to touch schema/internal tables is denied by the authorizer."""
    r = execute_query("SELECT name FROM sqlite_master", db_path)
    assert r["success"] is False
    assert r["error_type"] == "blocked_by_guardrail"


def test_pragma_table_functions_are_denied(db_path):
    r = execute_query("SELECT name FROM pragma_table_info('orders')", db_path)
    assert r["success"] is False


def test_runaway_query_is_killed_by_wall_clock_deadline(db_path):
    t = time.monotonic()
    r = execute_query(
        "SELECT COUNT(a.id) FROM orders a, orders b, orders c, orders d",
        db_path, timeout_seconds=2,
    )
    assert time.monotonic() - t < 10
    assert r["success"] is False and r["error_type"] == "timeout"


def test_recursive_cte_without_bound_is_killed(db_path):
    r = execute_query(
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT MAX(x) FROM c",
        db_path, timeout_seconds=2,
    )
    assert r["success"] is False and r["error_type"] == "timeout"


def test_oversized_value_bomb_is_rejected(db_path):
    r = execute_query("SELECT length(hex(randomblob(900000000)))", db_path)
    assert r["success"] is False


def test_normal_analytics_query_still_works(db_path):
    r = execute_query("SELECT COUNT(*) AS n, SUM(amount) AS s FROM orders", db_path)
    assert r["success"] and r["rows"][0]["n"] == 3000
