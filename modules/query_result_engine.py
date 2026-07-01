"""
Query Result Engine
Safely executes validated SQL against a SQLite database and returns structured results plus profiling metadata.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from typing import Any, Dict, List


def _rows_to_dicts(cursor: sqlite3.Cursor, rows: list) -> List[Dict[str, Any]]:
    cols = [desc[0] for desc in cursor.description] if cursor.description else []
    return [dict(zip(cols, row)) for row in rows]


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
    if "timeout" in msg:
        return "timeout"
    return "execution_error"


def _apply_limit(sql: str, max_rows: int) -> str:
    stripped = sql.strip().rstrip(";")
    if re.search(r"\blimit\b", stripped, flags=re.IGNORECASE):
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
    timeout_seconds: int = 30,
) -> dict:
    """Execute SQL safely against SQLite and return structured results."""
    start = time.time()
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=timeout_seconds)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(f"PRAGMA busy_timeout = {timeout_seconds * 1000}")

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

        final_sql = _apply_limit(final_sql, max_rows)
        cur.execute(final_sql)
        rows = cur.fetchall()
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
                "truncated": len(data) >= max_rows,
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