"""
Schema Engine
Loads SQLite databases and extracts rich schema intelligence for downstream agents.
"""

from __future__ import annotations

import os
import re
import sqlite3
from typing import Any, Dict, List
import pandas as pd

def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_")

def _table_columns(cur: sqlite3.Cursor, table: str) -> List[Dict[str, Any]]:
    cur.execute(f'PRAGMA table_info("{table}");')
    cols = cur.fetchall()
    return [
        {
            "cid": c[0],
            "name": c[1],
            "type": c[2] or "TEXT",
            "notnull": bool(c[3]),
            "default": c[4],
            "pk": bool(c[5]),
        }
        for c in cols
    ]


def _table_foreign_keys(cur: sqlite3.Cursor, table: str) -> List[Dict[str, Any]]:
    cur.execute(f'PRAGMA foreign_key_list("{table}");')
    fks = cur.fetchall()
    return [
        {"from": fk[3], "to_table": fk[2], "to_col": fk[4]}
        for fk in fks
    ]

def _column_stats(conn: sqlite3.Connection, table: str, columns: List[Dict[str, Any]]) -> Dict[str, Any]:
    stats: Dict[str, Any] = {}
    for col in columns:
        cname = col["name"]
        try:
            q = f'SELECT COUNT(*) AS n, COUNT("{cname}") AS nn, COUNT(DISTINCT "{cname}") AS nd FROM "{table}"'
            n, nn, nd = conn.execute(q).fetchone()
            nulls = max(n - nn, 0)
            stats[cname] = {
                "rows": int(n or 0),
                "non_null": int(nn or 0),
                "distinct": int(nd or 0),
                "null_count": int(nulls),
                "null_pct": round((nulls / n) * 100, 2) if n else 0.0,
                "uniqueness_pct": round((nd / n) * 100, 2) if n else 0.0,
            }
        except Exception:
            stats[cname] = {
                "rows": 0,
                "non_null": 0,
                "distinct": 0,
                "null_count": 0,
                "null_pct": 0.0,
                "uniqueness_pct": 0.0,
            }
    return stats


def _sample_rows(conn: sqlite3.Connection, table: str, limit: int = 5) -> List[Dict[str, Any]]:
    try:
        df = pd.read_sql_query(f'SELECT * FROM "{table}" LIMIT {int(limit)}', conn)
        return df.to_dict(orient="records")
    except Exception:
        return []

def load_database(db_path: str) -> Dict[str, Any]:
    """Load a SQLite database and return full schema intelligence."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;")
    tables = [row[0] for row in cur.fetchall()]

    schema: Dict[str, Any] = {}
    total_rows = 0
    for table in tables:
        columns = _table_columns(cur, table)
        foreign_keys = _table_foreign_keys(cur, table)
        row_count = int(cur.execute(f'SELECT COUNT(*) FROM "{table}";').fetchone()[0])
        total_rows += row_count

        try:
            df = pd.read_sql_query(f'SELECT * FROM "{table}" LIMIT 1', conn)
            dtypes = {c: str(t) for c, t in df.dtypes.items()}
        except Exception:
            dtypes = {c["name"]: c["type"] for c in columns}

        schema[table] = {
            "name": table,
            "safe_name": _safe_name(table),
            "columns": columns,
            "row_count": row_count,
            "foreign_keys": foreign_keys,
            "sample_data": _sample_rows(conn, table),
            "column_stats": _column_stats(conn, table, columns),
            "dtypes": dtypes,
        }

    relationships: List[Dict[str, Any]] = []
    for table, info in schema.items():
        for fk in info["foreign_keys"]:
            relationships.append(
                {
                    "from_table": table,
                    "from_col": fk["from"],
                    "to_table": fk["to_table"],
                    "to_col": fk["to_col"],
                }
            )

    size_bytes = os.path.getsize(db_path)
    size_str = f"{size_bytes / 1024:.1f} KB" if size_bytes < 1024 * 1024 else f"{size_bytes / (1024 * 1024):.1f} MB"
    conn.close()

    return {
        "tables": schema,
        "relationships": relationships,
        "table_count": len(tables),
        "total_rows": total_rows,
        "db_size": size_str,
        "db_path": db_path,
    }

def schema_to_prompt_text(schema: Dict[str, Any]) -> str:
    lines = ["DATABASE SCHEMA:\n"]
    for table, info in schema["tables"].items():
        lines.append(f"Table: {table} ({info['row_count']} rows)")
        for col in info["columns"]:
            pk_tag = " [PK]" if col["pk"] else ""
            nn_tag = " NOT NULL" if col["notnull"] else ""
            stats = info.get("column_stats", {}).get(col["name"], {})
            stat_txt = f" nulls={stats.get('null_pct', 0.0)}% distinct={stats.get('distinct', 0)}"
            lines.append(f" - {col['name']} ({col['type']}){pk_tag}{nn_tag}{stat_txt}")
        if info["foreign_keys"]:
            for fk in info["foreign_keys"]:
                lines.append(f" FK: {fk['from']} -> {fk['to_table']}.{fk['to_col']}")
        lines.append("")

    if schema.get("relationships"):
        lines.append("RELATIONSHIPS:")
        for r in schema["relationships"]:
            lines.append(f" {r['from_table']}.{r['from_col']} -> {r['to_table']}.{r['to_col']}")

    return "\n".join(lines)

def get_null_stats(db_path: str, table: str, column: str) -> float:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(f'SELECT COUNT(*) FROM "{table}";')
    total = int(cur.fetchone()[0] or 0)
    if total == 0:
        conn.close()
        return 0.0
    cur.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" IS NULL;')
    nulls = int(cur.fetchone()[0] or 0)
    conn.close()
    return round((nulls / total) * 100, 2)