"""
Exercises the SQL validate -> execute stages directly against a hand-built
schema/SQL pair. These stages don't require an LLM provider, unlike
/api/generate-sql (see NOTE in the final report: no GROQ/OPENAI/ANTHROPIC
key is available in this environment, so the natural-language -> SQL step
itself could not be run end-to-end here). What IS covered end-to-end is
everything downstream of a generated SQL string: AST validation, read-only
execution, aggregation, filtering, grouping, ordering, and top-N -- the
same stages a real /api/generate-sql SQL string would pass through.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from modules.schema_engine import load_database
from modules.sql_validator import validate_sql
from modules.query_result_engine import execute_query


@pytest.fixture()
def sample_db():
    path = os.path.join(tempfile.mkdtemp(prefix="qs_query_test_"), "sample.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, product TEXT, "
        "amount REAL, sold_on TEXT)"
    )
    rows = [
        (1, "North", "Widget", 100.0, "2024-01-01"),
        (2, "North", "Gadget", 50.0, "2024-01-15"),
        (3, "South", "Widget", 75.0, "2024-02-01"),
        (4, "South", "Gadget", 125.0, "2024-02-15"),
        (5, "East", "Widget", 200.0, "2024-03-01"),
    ] + [(6 + i, "West", "Gizmo", float(i), "2024-04-01") for i in range(25)]  # 25 extra rows for top-N test
    conn.executemany("INSERT INTO sales VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def schema(sample_db):
    return load_database(sample_db)


def test_aggregation_sum(sample_db, schema):
    sql = "SELECT SUM(amount) AS total FROM sales"
    v = validate_sql(sql, schema)
    assert v["is_valid"], v
    r = execute_query(sql, sample_db)
    assert r["success"] is True
    assert r["rows"][0]["total"] == pytest.approx(sum(row[3] for row in [
        (1, "North", "Widget", 100.0), (2, "North", "Gadget", 50.0),
        (3, "South", "Widget", 75.0), (4, "South", "Gadget", 125.0),
        (5, "East", "Widget", 200.0),
    ]) + sum(range(25)))


def test_filtering(sample_db, schema):
    sql = "SELECT id, region, product, amount, sold_on FROM sales WHERE region = 'North'"
    assert validate_sql(sql, schema)["is_valid"]
    r = execute_query(sql, sample_db)
    assert r["success"] is True
    assert r["row_count"] == 2
    assert all(row["region"] == "North" for row in r["rows"])


def test_grouping(sample_db, schema):
    sql = "SELECT region, SUM(amount) AS total FROM sales GROUP BY region"
    assert validate_sql(sql, schema)["is_valid"]
    r = execute_query(sql, sample_db)
    assert r["success"] is True
    regions = {row["region"] for row in r["rows"]}
    assert regions == {"North", "South", "East", "West"}


def test_ordering_and_top_n_is_not_reduced_to_8(sample_db, schema):
    """The 30-row West/Gizmo block exists specifically to prove a 'top 20'
    style query returns all 20 rows through validation + execution, not an
    arbitrary 8/10 cap. (Any earlier row-count cap lived in the frontend
    chart renderer, not here -- see test_frontend_chart_limit.py.)"""
    sql = "SELECT id, amount FROM sales ORDER BY amount DESC LIMIT 20"
    assert validate_sql(sql, schema)["is_valid"]
    r = execute_query(sql, sample_db)
    assert r["success"] is True
    assert r["row_count"] == 20
    values = [row["amount"] for row in r["rows"]]
    assert values == sorted(values, reverse=True)


def test_multi_condition_filter(sample_db, schema):
    sql = "SELECT id, region, product, amount FROM sales WHERE region = 'South' AND amount > 100"
    assert validate_sql(sql, schema)["is_valid"]
    r = execute_query(sql, sample_db)
    assert r["success"] is True
    assert r["row_count"] == 1
    assert r["rows"][0]["product"] == "Gadget"


def test_empty_result_handled_cleanly(sample_db, schema):
    sql = "SELECT id, region, product, amount FROM sales WHERE region = 'Nonexistent'"
    assert validate_sql(sql, schema)["is_valid"]
    r = execute_query(sql, sample_db)
    assert r["success"] is True
    assert r["status"] == "empty_result"
    assert r["rows"] == []


# --- Existing SQL security/AST validation must still hold -----------------

@pytest.mark.parametrize("sql", [
    "DROP TABLE sales",
    "DELETE FROM sales",
    "UPDATE sales SET amount = 0",
    "SELECT id FROM sales; DROP TABLE sales;",
    "INSERT INTO sales VALUES (999, 'X', 'Y', 1.0, '2024-01-01')",
])
def test_mutating_sql_rejected_by_validator(schema, sql):
    v = validate_sql(sql, schema)
    assert v["is_valid"] is False


def test_only_select_with_executes(sample_db):
    r = execute_query("DROP TABLE sales", sample_db)
    assert r["success"] is False
    assert r["error_type"] == "not_select_query"
