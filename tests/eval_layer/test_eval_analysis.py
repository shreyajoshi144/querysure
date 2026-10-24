"""Independent hallucination detection + structural feature extraction."""
from __future__ import annotations

import pytest

from evaluation.analysis import analyze_sql, build_catalog, sqlite_compile_check


@pytest.fixture(scope="module")
def catalog(env):
    return env.catalog


def test_catalog_is_built_from_the_real_schema(catalog):
    assert set(catalog) == {"stores", "products", "customers", "orders", "order_items"}
    assert "loyalty_tier" in catalog["customers"]


@pytest.mark.parametrize("sql", [
    "SELECT store_name FROM stores",
    "SELECT s.store_name, COUNT(o.order_id) FROM stores s JOIN orders o ON o.store_id = s.store_id GROUP BY s.store_name",
    "SELECT strftime('%Y-%m', order_date) AS m, COUNT(*) FROM orders GROUP BY m",         # alias used in GROUP BY
    "SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY n DESC",            # alias in ORDER BY
    "WITH t AS (SELECT store_id, COUNT(*) AS n FROM orders GROUP BY store_id) "
    "SELECT s.store_name, t.n FROM stores s JOIN t ON t.store_id = s.store_id",            # CTE
    "SELECT x.a FROM (SELECT product_id AS a FROM products) AS x",                         # derived table
    "SELECT customer_id FROM customers WHERE customer_id NOT IN (SELECT customer_id FROM orders)",
    "SELECT rowid, store_name FROM stores",                                                # implicit rowid
    "SELECT CASE WHEN status = 'returned' THEN 1 ELSE 0 END FROM orders",                  # string literals
    "SELECT COUNT(*) FROM orders",
])
def test_valid_sql_has_no_false_hallucination(catalog, sql, eval_db):
    a = analyze_sql(sql, catalog)
    assert a.parse_ok
    assert a.hallucinated_tables == [] and a.hallucinated_columns == [], a.hallucinated_identifiers
    assert sqlite_compile_check(sql, eval_db).ok


@pytest.mark.parametrize("sql, tables, columns", [
    ("SELECT nope FROM stores", [], ["nope"]),                                          # unqualified unknown column
    ("SELECT s.nope FROM stores s", [], ["stores.nope"]),                               # qualified unknown column
    ("SELECT phone FROM customers", [], ["phone"]),
    ("SELECT o.store_name FROM orders o", [], ["orders.store_name"]),                   # real column, wrong table
    ("SELECT * FROM employees", ["employees"], []),                                     # unknown table
    ("SELECT e.salary FROM employees e", ["employees"], []),                            # column of an unknown table: not double-counted
    ("SELECT z.a FROM stores s", [], ["z.a"]),                                          # unknown alias
    ("SELECT store_name FROM stores s JOIN ghost g ON g.id = s.store_id", ["ghost"], []),
])
def test_hallucinations_are_detected(catalog, sql, tables, columns):
    a = analyze_sql(sql, catalog)
    assert a.parse_ok
    assert a.hallucinated_tables == tables
    assert a.hallucinated_columns == columns


def test_double_quoted_unknown_token_is_treated_as_a_string_literal_like_sqlite(catalog, eval_db):
    sql = 'SELECT store_name FROM stores WHERE region = "North"'
    a = analyze_sql(sql, catalog)
    assert a.hallucinated_columns == [] and a.dqs_literals == ["north"]
    assert sqlite_compile_check(sql, eval_db).ok


@pytest.mark.parametrize("sql, kind", [
    ("SELECT nope FROM stores", "unknown_column"),
    ("SELECT * FROM employees", "unknown_table"),
    ("SELEC store_name FROM stores", "syntax"),
    ("SELECT store_name FROM", "syntax"),
    ("", "syntax"),
])
def test_sqlite_compile_check_classifies_errors(eval_db, sql, kind):
    c = sqlite_compile_check(sql, eval_db)
    assert not c.ok and c.kind == kind
    assert c.is_hallucination == (kind in ("unknown_column", "unknown_table"))


def test_sqlite_compile_check_never_executes_or_modifies(eval_db):
    """EXPLAIN only compiles. A destructive statement passes/fails the compile
    step without touching data, and the connection is read-only regardless."""
    import sqlite3
    before = sqlite3.connect(eval_db).execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    sqlite_compile_check("DELETE FROM orders", eval_db)
    sqlite_compile_check("DROP TABLE orders", eval_db)
    after = sqlite3.connect(eval_db).execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert before == after > 0


def test_unparseable_and_empty_sql(catalog):
    for bad in ("", "   ", "this is not sql at all", "SELECT FROM WHERE"):
        a = analyze_sql(bad, catalog)
        assert not a.parse_ok and a.parse_error


def test_stacked_statements_are_visible(catalog):
    a = analyze_sql("SELECT COUNT(*) FROM orders; DROP TABLE order_items", catalog)
    assert a.parse_ok and a.statement_count == 2


def test_identifier_reference_counts(catalog):
    a = analyze_sql("SELECT o.order_id, s.store_name FROM orders o JOIN stores s ON s.store_id = o.store_id", catalog)
    assert a.table_refs == 2 and a.column_refs == 4


def test_structural_features(catalog):
    f = analyze_sql(
        "SELECT s.region, SUM(oi.quantity) AS q FROM order_items oi JOIN orders o ON o.order_id = oi.order_id "
        "JOIN stores s ON s.store_id = o.store_id WHERE strftime('%Y', o.order_date) = '2023' "
        "GROUP BY s.region HAVING SUM(oi.quantity) > 1 ORDER BY q DESC LIMIT 3", catalog).features
    assert f == {"aggregation": True, "group_by": True, "having": True, "order_by": True, "limit": True,
                 "date_function": True, "join": True, "subquery_or_cte": False, "distinct": False}
    g = analyze_sql("SELECT DISTINCT status FROM orders", catalog).features
    assert g["distinct"] and not g["aggregation"] and not g["join"] and not g["date_function"]
    h = analyze_sql("WITH t AS (SELECT 1 AS x) SELECT x FROM t", catalog).features
    assert h["subquery_or_cte"]
    assert analyze_sql("SELECT julianday(order_date) FROM orders", catalog).features["date_function"]
