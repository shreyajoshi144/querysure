"""
Query Result Engine
Safely executes validated SQL against a SQLite database and returns structured results plus profiling metadata.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from pathlib import Path
from typing import Sequence, Any, Dict, List

import sqlglot

from .json_safe import json_safe_rows

_READ_DIALECT = "sqlite"


# ---------------------------------------------------------------------------
# Execution-time guardrails (defense in depth -- the AST validator is the first
# gate, these make sure a validator miss cannot become a write, a file read, or
# a hung worker).
# ---------------------------------------------------------------------------
# Functions that can touch the filesystem / load native code. Most are not
# compiled into Python's sqlite3, but a different SQLite build must not change
# the security posture.
_DENIED_FUNCTIONS = frozenset({
    "load_extension", "readfile", "writefile", "edit", "fts3_tokenizer",
})
# Hard cap on any single string/blob SQLite may build (randomblob/zeroblob/
# replace/printf bombs), in bytes.
_MAX_VALUE_BYTES = 1_000_000
# How often (in SQLite VM instructions) the wall-clock deadline is checked.
_PROGRESS_INTERVAL = 10_000
DEFAULT_TIMEOUT_SECONDS = 15


class QueryTimeout(Exception):
    pass


def _authorizer(action: int, arg1, arg2, db_name, trigger):
    """Allow only what a read-only analytics SELECT needs. Everything else
    (INSERT/UPDATE/DELETE/DDL/PRAGMA/ATTACH/transactions...) is denied by
    default rather than enumerated."""
    if action == sqlite3.SQLITE_SELECT or action == sqlite3.SQLITE_RECURSIVE:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        table = (arg1 or "").lower()
        if table.startswith("sqlite_") or table.startswith("pragma_"):
            return sqlite3.SQLITE_DENY  # schema/internal tables are not user data
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION:
        if (arg2 or "").lower() in _DENIED_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _open_readonly(db_path: str, timeout_seconds: int) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")
    if hasattr(conn, "setlimit"):  # Python 3.11+
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_VALUE_BYTES)
    # Authorizer goes on LAST so the setup PRAGMAs above are not blocked by it.
    conn.set_authorizer(_authorizer)
    return conn


def _install_deadline(conn: sqlite3.Connection, timeout_seconds: float) -> None:
    """Real wall-clock limit. sqlite3.connect(timeout=) / busy_timeout only
    bound lock waits, NOT how long a query may run."""
    deadline = time.monotonic() + timeout_seconds
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, _PROGRESS_INTERVAL)


def _rows_to_dicts(cursor: sqlite3.Cursor, rows: list) -> List[Dict[str, Any]]:
    cols = [desc[0] for desc in cursor.description] if cursor.description else []
    # Results are persisted in a JSON column and returned to the browser:
    # BLOBs, +/-Infinity etc. must be made JSON-safe here (modules/json_safe.py).
    return json_safe_rows(dict(zip(cols, row)) for row in rows)


def _classify_error(err: Exception) -> str:
    msg = str(err).lower()
    if "no such table" in msg:
        return "missing_table"
    if "no such column" in msg:
        return "missing_column"
    if "syntax error" in msg:
        return "syntax_error"
    if "database is locked" in msg:
        return "db_locked"
    if "timeout" in msg or "interrupted" in msg:
        return "timeout"
    if "not authorized" in msg or "authoriz" in msg or "prohibited" in msg:
        return "blocked_by_guardrail"
    if "readonly" in msg or "read-only" in msg:
        return "blocked_by_guardrail"
    return "execution_error"


def _has_outer_limit(sql: str) -> bool:
    """True only if the statement's OWN (outermost) result set is capped by
    a LIMIT -- not a LIMIT that merely appears somewhere inside it.

    GAP FIXED: the previous check was `re.search(r"\blimit\b", sql)` over
    the whole string, so `WITH recent AS (SELECT * FROM t LIMIT 5) SELECT *
    FROM recent JOIN other ON ...` was treated as "already limited" (the
    regex found the CTE's LIMIT) even though the actual rows returned to the
    caller -- the join's output -- were completely uncapped. Same problem for
    a LIMIT inside a plain subquery, or inside just one branch of a UNION.

    This parses the SQL and checks the LIMIT clause on the outermost node
    only, which sqlglot attaches correctly regardless of how the query is
    structured (verified against a Select with a CTE, a subquery in FROM,
    and a UNION with/without a limit on one branch vs. the whole union).
    execute_query() only reaches this after confirming the SQL starts with
    SELECT/WITH, so parsing here should always succeed; on the rare SQLite
    construct sqlglot can't parse, this falls back to the old whole-string
    check -- the safe direction, since the worst case is one redundant (but
    harmless) LIMIT being appended to a query that already had one.
    """
    try:
        return sqlglot.parse_one(sql, read=_READ_DIALECT).args.get("limit") is not None
    except Exception:
        return bool(re.search(r"\blimit\b", sql, flags=re.IGNORECASE))


def _apply_limit(sql: str, max_rows: int) -> str:
    stripped = sql.strip().rstrip(";")
    if _has_outer_limit(stripped):
        return stripped
    return f"{stripped}\nLIMIT {max_rows}"


def _build_result_profile(columns: List[str], rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    null_counts = {c: 0 for c in columns}
    distinct_sets = {c: set() for c in columns}
    for row in rows:
        for c in columns:
            val = row.get(c)
            if val is None:
                null_counts[c] += 1
            else:
                distinct_sets[c].add(val)
    return {
        "column_count": len(columns),
        "row_count": len(rows),
        "null_counts": null_counts,
        "distinct_counts": {c: len(v) for c, v in distinct_sets.items()},
    }


def execute_query(
    sql: str,
    db_path: str,
    max_rows: int = 1000,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    params: "Sequence[Any] | None" = None,
    parameterize: bool = False,
) -> dict:
    """Execute SQL safely against SQLite and return structured results.

    ``params`` binds ``?`` placeholders already present in ``sql``. ``parameterize=True`` instead binds the
    string literals of predicates (see modules.sql_params) -- the SQL reported back is still the original text."""
    start = time.time()
    conn = None
    try:
        final_sql = sql.strip().rstrip(";")
        if not final_sql.lower().startswith("select") and not final_sql.lower().startswith("with"):
            return {
                "status": "error",
                "success": False,
                "query_id": hashlib.md5(final_sql.encode()).hexdigest()[:12],
                "error_type": "not_select_query",
                "error": "Only SELECT/WITH queries are allowed for execution.",
                "rows": [],
                "row_count": 0,
                "columns": [],
                "result_profile": {"column_count": 0, "row_count": 0, "null_counts": {}, "distinct_counts": {}},
                "execution_metadata": {"execution_time_ms": int((time.time() - start) * 1000), "max_rows": max_rows, "truncated": False},
                "sql": final_sql,
            }

        raw_sql = final_sql
        final_sql = _apply_limit(raw_sql, max_rows)          # what is reported back to the user
        # Fetch one extra row so "truncated" is only reported when rows were actually cut off (a result of exactly
        # max_rows rows used to be flagged as truncated).
        probe_sql = _apply_limit(raw_sql, max_rows + 1) if not _has_outer_limit(raw_sql) else final_sql
        bound: tuple = tuple(params or ())
        exec_sql = probe_sql
        if parameterize and not bound:
            from .sql_params import parameterize_literals
            exec_sql, plist = parameterize_literals(probe_sql)
            bound = tuple(plist)
        conn = _open_readonly(db_path, timeout_seconds)
        _install_deadline(conn, timeout_seconds)
        cur = conn.cursor()
        cur.execute(exec_sql, bound)
        # Hard cap regardless of any LIMIT the SQL carries (a model-written
        # "LIMIT 999999999" is honored by SQLite but must not be by us).
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        data = _rows_to_dicts(cur, rows)
        columns = [d[0] for d in cur.description] if cur.description else []
        profile = _build_result_profile(columns, data)
        elapsed = int((time.time() - start) * 1000)
        status = "empty_result" if len(data) == 0 else "success"
        return {
            "status": status,
            "success": True,
            "query_id": hashlib.md5(final_sql.encode()).hexdigest()[:12],
            "rows": data,
            "row_count": len(data),
            "columns": columns,
            "result_profile": profile,
            "execution_metadata": {
                "execution_time_ms": elapsed,
                "max_rows": max_rows,
                "truncated": truncated,
            },
            "sql": final_sql,
        }
    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        return {
            "status": "error",
            "success": False,
            "query_id": hashlib.md5(sql.strip().encode()).hexdigest()[:12],
            "error_type": _classify_error(e),
            "error": str(e),
            "rows": [],
            "row_count": 0,
            "columns": [],
            "result_profile": {"column_count": 0, "row_count": 0, "null_counts": {}, "distinct_counts": {}},
            "execution_metadata": {"execution_time_ms": elapsed, "max_rows": max_rows, "truncated": False},
            "sql": sql,
        }
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# Dataset Explorer row preview. Browsing, not analytics: it never goes through
# the LLM or the SQL validator, so it must be impossible for it to run anything
# but one fixed SELECT against a table the caller's schema profile already lists.
# ---------------------------------------------------------------------------
PREVIEW_MAX_ROWS = 10          # default page size (what the endpoint returned before pagination existed)
PREVIEW_MAX_PAGE_SIZE = 50     # hard ceiling a caller can ask for
PREVIEW_MAX_CELL_CHARS = 200


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def preview_table(db_path: str, table: str, known_tables: Dict[str, Any], limit: int = PREVIEW_MAX_ROWS,
                  offset: int = 0) -> dict:
    """Return one page (at most ``limit`` rows, never above PREVIEW_MAX_PAGE_SIZE) of ``table``, starting at ``offset``.

    ``table`` must be a key of ``known_tables`` (the session's profiled schema); anything else raises ``KeyError`` before
    a connection is opened, so a crafted name is never interpolated into SQL. ``limit``/``offset`` are coerced to
    clamped integers. Pages follow the table's natural (storage) order. Long text cells are truncated.
    """
    if table not in known_tables:
        raise KeyError(table)
    limit = max(1, min(int(limit), PREVIEW_MAX_PAGE_SIZE))
    offset = max(0, int(offset))
    conn = _open_readonly(db_path, DEFAULT_TIMEOUT_SECONDS)
    try:
        _install_deadline(conn, DEFAULT_TIMEOUT_SECONDS)
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {_quote_identifier(table)} LIMIT {limit} OFFSET {offset}")
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = _rows_to_dicts(cur, cur.fetchmany(limit))
    finally:
        conn.close()
    for row in rows:
        for key, value in row.items():
            if isinstance(value, str) and len(value) > PREVIEW_MAX_CELL_CHARS:
                row[key] = value[:PREVIEW_MAX_CELL_CHARS] + "\u2026"
    return {"columns": columns, "rows": rows}
