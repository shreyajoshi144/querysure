"""SQLite validator / schema consistency: positive and negative regression tests (joins, aliases, CTEs, nested queries,
ambiguous columns, invalid relationships) plus metadata accuracy."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "fixtures"))
import build_northwind_small as bnw  # noqa: E402
from modules.query_result_engine import execute_query  # noqa: E402
from modules.schema_engine import load_database  # noqa: E402
from modules.sql_validator import validate_sql  # noqa: E402


@pytest.fixture(scope="module")
def nw(tmp_path_factory):
    p = tmp_path_factory.mktemp("nw") / "northwind_small.sqlite"
    bnw.build(str(p))
    return str(p), load_database(str(p))


POSITIVE = {
    # SELECT * is a read-only projection: accepted with a warning since the v1.0.8 accuracy pass (was rejected before)
    "select_star": "SELECT * FROM Product",
    "inferred_join_chain": 'SELECT p.ProductName, SUM(od.Quantity) FROM OrderDetail od JOIN Product p ON od.ProductId = p.Id JOIN "Order" o ON od.OrderId = o.Id GROUP BY p.ProductName',
    "cte": "WITH t AS (SELECT od.ProductId AS pid, SUM(od.Quantity) AS q FROM OrderDetail od GROUP BY od.ProductId) SELECT p.ProductName, t.q FROM t JOIN Product p ON t.pid = p.Id ORDER BY t.q DESC LIMIT 3",
    "two_ctes": 'WITH a AS (SELECT o.Id AS oid, o.CustomerId AS cid FROM "Order" o), b AS (SELECT a.cid, COUNT(*) AS n FROM a GROUP BY a.cid) SELECT c.CompanyName, b.n FROM b JOIN Customer c ON b.cid = c.Id',
    "derived_table": "SELECT x.CategoryId, x.n FROM (SELECT p.CategoryId, COUNT(*) AS n FROM Product p GROUP BY p.CategoryId) x WHERE x.n > 2",
    "in_subquery": "SELECT p.ProductName FROM Product p WHERE p.Id IN (SELECT od.ProductId FROM OrderDetail od WHERE od.Quantity > 20)",
    "scalar_subquery": "SELECT p.ProductName, (SELECT AVG(p2.UnitPrice) FROM Product p2) AS avg_price FROM Product p",
    "exists_correlated": 'SELECT c.CompanyName FROM Customer c WHERE EXISTS (SELECT 1 FROM "Order" o WHERE o.CustomerId = c.Id)',
    "derived_join_derived": "SELECT a.Id FROM (SELECT p.Id FROM Product p) a JOIN (SELECT od.ProductId AS Id FROM OrderDetail od) b ON a.Id = b.Id",
    "same_alias_in_two_scopes": "SELECT p.ProductName FROM Product p WHERE p.Id IN (SELECT p.ProductId FROM OrderDetail p)",
    "column_alias_in_order_by": "SELECT p.ProductName AS name, p.UnitPrice * 2 AS dbl FROM Product p ORDER BY dbl DESC",
    "self_join_on_key": "SELECT e1.LastName FROM Employee e1 JOIN Employee e2 ON e1.Id = e2.Id",
    "union": "SELECT c.Country FROM Customer c UNION SELECT s.Country FROM Supplier s",
    "window": "SELECT p.ProductName, RANK() OVER (ORDER BY p.UnitPrice DESC) AS r FROM Product p",
    "quoted_identifiers": 'SELECT "o"."Id" FROM "Order" AS "o"',
    "case_insensitive_names": "select p.productname from product p join category c on p.categoryid = c.id",
    "no_alias_qualified": "SELECT Product.ProductName FROM Product JOIN Category ON Product.CategoryId = Category.Id",
    "having": "SELECT p.CategoryId, COUNT(*) FROM Product p GROUP BY p.CategoryId HAVING COUNT(*) > 3",
    "comma_join_with_where_link": "SELECT p.ProductName FROM Product p, Category c WHERE p.CategoryId = c.Id",
}
NEGATIVE = {
    "unknown_column": "SELECT p.Nope FROM Product p",
    "unknown_table": "SELECT x.Id FROM Nope x",
    "ambiguous_column": "SELECT Id FROM Product p JOIN Category c ON p.CategoryId = c.Id",
    "unrelated_join_columns": "SELECT p.ProductName FROM OrderDetail od JOIN Product p ON od.Quantity = p.Id",
    "type_valid_but_unrelated": 'SELECT o.Id FROM "Order" o JOIN Product p ON o.Freight = p.UnitPrice',
    "wrong_fk_target": 'SELECT o.Id FROM "Order" o JOIN Customer c ON o.EmployeeId = c.Id',
    "cte_bad_column": "WITH t AS (SELECT p.Id AS pid FROM Product p) SELECT t.nope FROM t",
    "cte_laundered_bad_join": "WITH t AS (SELECT od.Quantity AS q FROM OrderDetail od) SELECT p.ProductName FROM Product p JOIN t ON t.q = p.Id",
    "derived_laundered_bad_join": "SELECT p.ProductName FROM Product p JOIN (SELECT od.Quantity AS q FROM OrderDetail od) d ON d.q = p.Id",
    "derived_bad_column": "SELECT x.zzz FROM (SELECT p.Id FROM Product p) x",
    "cartesian_comma": "SELECT p.Id FROM Product p, Category c",
    "cartesian_cross_join": "SELECT p.Id FROM Product p CROSS JOIN Category c",
    "delete": "DELETE FROM Product",
    "drop": "DROP TABLE Product",
    "stacked": "SELECT 1; SELECT 2",
    "pragma": "PRAGMA table_info(Product)",
    "attach": "ATTACH DATABASE 'x.db' AS x",
    "sqlite_master": "SELECT name FROM sqlite_master",
    "load_extension": "SELECT load_extension('x')",
    "reserved_word_unquoted": "SELECT order FROM Product",
    "empty": "   ",
}


@pytest.mark.parametrize("name", sorted(POSITIVE))
def test_valid_sql_is_accepted(nw, name):
    r = validate_sql(POSITIVE[name], nw[1])
    assert r["is_valid"], [i["message"] for i in r["issues"]]


@pytest.mark.parametrize("name", sorted(NEGATIVE))
def test_invalid_or_unsafe_sql_is_rejected(nw, name):
    assert not validate_sql(NEGATIVE[name], nw[1])["is_valid"]


@pytest.mark.parametrize("name", sorted(POSITIVE))
def test_everything_the_validator_accepts_also_executes(nw, name):
    """Validation must understand the SQL exactly as execution will: no accepted query may fail in SQLite."""
    res = execute_query(POSITIVE[name], nw[0])
    assert res["success"], res.get("error")


def test_fanout_risk_is_still_reported_for_many_side_chains(nw):
    r = validate_sql(POSITIVE["inferred_join_chain"], nw[1])
    assert r["fanout_risk"] in ("Medium", "High") or r["warnings"]


def test_declared_primary_and_foreign_keys_are_reflected(tmp_path):
    p = tmp_path / "fk.sqlite"
    con = sqlite3.connect(p)
    con.executescript("""CREATE TABLE Parent(Id INTEGER PRIMARY KEY, Name TEXT);
    CREATE TABLE Child(Id INTEGER PRIMARY KEY, ParentRef INTEGER REFERENCES Parent(Id), Note TEXT);
    INSERT INTO Parent VALUES (1,'a'),(2,'b'); INSERT INTO Child VALUES (1,1,'x'),(2,2,'y');""")
    con.commit(); con.close()
    s = load_database(str(p))
    assert [c["name"] for c in s["tables"]["Parent"]["columns"] if c["pk"]] == ["Id"]
    assert s["tables"]["Child"]["foreign_keys"] == [{"from": "ParentRef", "to_table": "Parent", "to_col": "Id"}]
    assert validate_sql("SELECT c.Note FROM Child c JOIN Parent p ON c.ParentRef = p.Id", s)["is_valid"]   # name is NOT <table>Id


def test_schema_always_reflects_the_active_database_not_a_stale_copy(tmp_path):
    p = tmp_path / "live.sqlite"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE T(Id INTEGER PRIMARY KEY, A TEXT)"); con.execute("INSERT INTO T VALUES (1,'x')"); con.commit()
    s1 = load_database(str(p))
    assert not validate_sql("SELECT t.B FROM T t", s1)["is_valid"]
    con.execute("ALTER TABLE T ADD COLUMN B TEXT"); con.execute("CREATE TABLE U(Id INTEGER PRIMARY KEY, TId INTEGER)")
    con.execute("INSERT INTO U VALUES (1,1)"); con.commit(); con.close()
    s2 = load_database(str(p))
    assert s2["dataset_fingerprint"] != s1["dataset_fingerprint"]
    assert validate_sql("SELECT t.B FROM T t JOIN U u ON u.TId = t.Id", s2)["is_valid"]


def test_validator_does_not_depend_on_the_database_file(nw, tmp_path):
    """Compile check uses an in-memory twin built from the schema: deleting/moving the file changes nothing."""
    s = dict(nw[1]); s["db_path"] = str(tmp_path / "gone.sqlite")
    assert validate_sql(POSITIVE["cte"], s)["is_valid"]
