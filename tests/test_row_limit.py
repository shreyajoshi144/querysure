"""
IMP-5: _apply_limit() used to check the whole SQL string for the word
"limit", so a query with no LIMIT of its own but an inner LIMIT (in a CTE,
a subquery, or one branch of a UNION) was wrongly treated as already
capped -- the actual rows returned to the caller were unbounded. These
tests check both the AST-level decision (_has_outer_limit) and, more
importantly, the real row count SQLite hands back through execute_query().
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from modules.query_result_engine import _apply_limit, _has_outer_limit, execute_query


# ===================================================================
# Unit level: does the AST check land on the right node in each shape?
# ===================================================================

@pytest.mark.parametrize("sql,expected", [
    ("SELECT * FROM t LIMIT 10", True),
    ("SELECT * FROM t ORDER BY x LIMIT 10 OFFSET 5", True),
    ("WITH a AS (SELECT * FROM t) SELECT * FROM a LIMIT 3", True),
    ("SELECT * FROM t1 UNION SELECT * FROM t2 LIMIT 5", True),
    # The bug: an inner LIMIT must NOT count as an outer one.
    ("WITH recent AS (SELECT * FROM t ORDER BY id DESC LIMIT 5) "
     "SELECT * FROM recent JOIN other ON recent.id = other.id", False),
    ("SELECT * FROM (SELECT * FROM t LIMIT 5) sub", False),
    ("SELECT * FROM t1 UNION SELECT * FROM t2 WHERE 1=1", False),
    ("SELECT * FROM t1 UNION (SELECT * FROM t2 LIMIT 5)", False),  # limit on one branch only
    ("SELECT * FROM t", False),
])
def test_has_outer_limit_matches_the_outermost_clause_only(sql, expected):
    assert _has_outer_limit(sql) is expected


def test_apply_limit_appends_when_only_an_inner_limit_exists():
    sql = ("WITH recent AS (SELECT * FROM t ORDER BY id DESC LIMIT 5) "
           "SELECT * FROM recent JOIN other ON recent.id = other.id")
    result = _apply_limit(sql, 100)
    assert "LIMIT 100" in result
    assert result.count("LIMIT") == 2  # the CTE's own LIMIT 5 is untouched


def test_apply_limit_does_not_double_limit_a_capped_query():
    result = _apply_limit("SELECT * FROM t LIMIT 10", 1000)
    assert result == "SELECT * FROM t LIMIT 10"


def test_has_outer_limit_falls_back_safely_on_unparseable_sql():
    """Parsing fails -> fall back to the old whole-string check, which can
    only err by appending a redundant (harmless) LIMIT, never by leaving a
    query uncapped."""
    garbage = "SELECT ??? FROM !!! LIMIT ((("
    assert _has_outer_limit(garbage) is True  # word "limit" present -> old behavior kept
    assert _has_outer_limit("SELECT ??? FROM !!!") is False


# ===================================================================
# Integration level: the ACTUAL row count execute_query() returns
# ===================================================================

@pytest.fixture()
def wide_db():
    """250 rows in `t`, joined 1:1 against `other` -- enough that an
    unbounded join is clearly distinguishable from a capped one."""
    path = os.path.join(tempfile.mkdtemp(prefix="qs_limit_test_"), "wide.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val REAL)")
    conn.execute("CREATE TABLE other (id INTEGER PRIMARY KEY, tag TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [(i, float(i)) for i in range(250)])
    conn.executemany("INSERT INTO other VALUES (?, ?)", [(i, f"tag{i}") for i in range(250)])
    conn.commit()
    conn.close()
    return path


def test_query_with_only_an_inner_cte_limit_is_still_capped(wide_db):
    """This is the exact shape the bug missed: no LIMIT on the outer query,
    only on the CTE feeding it -- the join's actual output was unbounded."""
    sql = ("WITH recent AS (SELECT * FROM t ORDER BY id DESC LIMIT 5) "
           "SELECT t.id, other.tag FROM t JOIN other ON t.id = other.id")
    result = execute_query(sql, wide_db, max_rows=10)
    assert result["success"] is True
    assert len(result["rows"]) == 10
    assert result["execution_metadata"]["truncated"] is True


def test_query_with_limited_subquery_in_from_is_still_capped(wide_db):
    """The subquery's own LIMIT 5 doesn't bound the outer result on its
    own here -- a separate fan-out table has 3 matches per id, so the join
    produces up to 15 rows, well past max_rows, unless the OUTER cap is
    actually applied."""
    conn = sqlite3.connect(wide_db)
    conn.execute("CREATE TABLE fanout (t_id INTEGER, tag TEXT)")
    conn.executemany("INSERT INTO fanout VALUES (?, ?)",
                     [(i, f"dup{n}") for i in range(5) for n in range(3)])  # 3 matches per id
    conn.commit()
    conn.close()

    sql = "SELECT * FROM (SELECT * FROM t LIMIT 5) sub JOIN fanout ON sub.id = fanout.t_id"
    unbounded_rows = len(sqlite3.connect(wide_db).execute(sql).fetchall())
    assert unbounded_rows == 15, "test setup should produce more rows than max_rows below"

    result = execute_query(sql, wide_db, max_rows=10)
    assert result["success"] is True
    assert len(result["rows"]) == 10


def test_query_with_a_real_outer_limit_is_not_double_capped(wide_db):
    result = execute_query("SELECT * FROM t LIMIT 20", wide_db, max_rows=1000)
    assert result["success"] is True
    assert len(result["rows"]) == 20
