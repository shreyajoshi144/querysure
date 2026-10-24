"""
Schema Engine
Loads SQLite databases and extracts rich schema intelligence for downstream agents.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List
import pandas as pd

from .json_safe import json_safe_rows

def _q(name: str) -> str:
    """Quote an identifier for SQLite. Doubling embedded quotes is what stops a
    table/column name containing `"` (legitimate, or hostile in an uploaded .db)
    from breaking out of the quoting."""
    return '"' + str(name).replace('"', '""') + '"'


# An uploaded .db controls its own schema. Bound how much work profiling it can
# cost (profiling is O(tables x columns) full scans).
MAX_TABLES = 200
MAX_COLUMNS_PER_TABLE = 200


class SchemaTooLarge(ValueError):
    """The database has more tables/columns than we are willing to profile."""


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_")

def _table_columns(cur: sqlite3.Cursor, table: str) -> List[Dict[str, Any]]:
    cur.execute(f'PRAGMA table_info({_q(table)});')
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
    cur.execute(f'PRAGMA foreign_key_list({_q(table)});')
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
            q = f'SELECT COUNT(*) AS n, COUNT({_q(cname)}) AS nn, COUNT(DISTINCT {_q(cname)}) AS nd FROM {_q(table)}'
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
        df = pd.read_sql_query(f'SELECT * FROM {_q(table)} LIMIT {int(limit)}', conn)
        # NaN / bytes / timestamps are not storable in the session's JSON column
        # (see modules/json_safe.py) -- sanitize at the source.
        return json_safe_rows(df.to_dict(orient="records"))
    except Exception:
        return []

def _file_fingerprint(db_path: str) -> str:
    h = hashlib.sha256()
    with open(db_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()



_INT_TYPES = ("INT",)
_TEXT_TYPES = ("CHAR", "TEXT", "CLOB", "STRING")
_INFER_MIN_CONTAINMENT = 0.99
# <table>Id / <table>_id pointing at that table's key is strong NAME evidence; real databases (Northwind: 29 of 830
# orders reference a missing customer) contain orphans, so that evidence needs only a clear majority of matches.
_INFER_MIN_CONTAINMENT_NAMED_FK = 0.80


def _name_words(name: str) -> List[str]:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(name))
    return [w for w in re.split(r"[^a-z0-9]+", s.lower()) if w]


def _type_family(decl: str) -> str:
    d = (decl or "").upper()
    if any(t in d for t in _INT_TYPES):
        return "int"
    if any(t in d for t in _TEXT_TYPES) or not d:
        return "text"
    return "other"


def _norm_ident(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ses") and len(word) > 4:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _is_unique_column(stats: Dict[str, Any], col: Dict[str, Any]) -> bool:
    st = stats.get(col["name"], {})
    return bool(st.get("non_null")) and st.get("distinct") == st.get("non_null") == st.get("rows")


def _infer_relationships(conn: sqlite3.Connection, schema: Dict[str, Any],
                         declared: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Conservatively infer joins that the file does not declare as foreign keys.

    A join is inferred only when ALL of these hold (name evidence + type
    evidence + data evidence), so an arbitrary column pair is never allowed:
      * the source column is named <table>Id / <table>_id (singular or plural
        table name, case/underscore-insensitive) or has the same name as the
        target's key column;
      * the target column is a primary key or a fully unique, non-null column;
      * both columns have the same type family (int/text);
      * at least 99% of the source's non-null values exist in the target.
    """
    known = {(r["from_table"], r["from_col"], r["to_table"], r["to_col"]) for r in declared}
    inferred: List[Dict[str, Any]] = []
    # candidate key columns per table
    keys: Dict[str, List[Dict[str, Any]]] = {}
    for t, info in schema.items():
        stats = info.get("column_stats", {})
        keys[t] = [c for c in info["columns"] if (c["pk"] or _is_unique_column(stats, c))]
    for src, sinfo in schema.items():
        declared_cols = {fk["from"] for fk in sinfo["foreign_keys"]}
        for col in sinfo["columns"]:
            if col["name"] in declared_cols:
                continue
            cn = _norm_ident(col["name"])
            fam = _type_family(col["type"])
            if fam == "other" or not cn:
                continue
            for dst, dinfo in schema.items():
                if dst == src or not dinfo["row_count"] or not sinfo["row_count"]:
                    continue
                tn = _norm_ident(dst)
                prefixes = {tn, _singular(tn)}
                for tcol in keys[dst]:
                    if _type_family(tcol["type"]) != fam:
                        continue
                    kn = _norm_ident(tcol["name"])
                    min_containment = _INFER_MIN_CONTAINMENT
                    if kn in ("id", "key", "code"):
                        name_ok = any(cn == p + kn for p in prefixes)
                        if name_ok and tcol["pk"]:
                            min_containment = _INFER_MIN_CONTAINMENT_NAMED_FK
                        if not name_ok and tcol["pk"] and not col["pk"]:
                            # ShipVia -> Shipper.Id: a word of the column names the target table. Weaker name evidence,
                            # so every non-null value must exist in the target key.
                            name_ok = any(len(w) >= 4 and (tn.startswith(w) or _singular(tn).startswith(w))
                                          for w in _name_words(col["name"]))
                            min_containment = 1.0
                    else:
                        name_ok = cn == kn  # same specific key name (e.g. CategoryId, sku)
                    if not name_ok:
                        continue
                    if (src, col["name"], dst, tcol["name"]) in known:
                        continue
                    try:
                        bad = conn.execute(
                            f"SELECT COUNT(*) FROM {_q(src)} WHERE {_q(col['name'])} IS NOT NULL "
                            f"AND {_q(col['name'])} NOT IN (SELECT {_q(tcol['name'])} FROM {_q(dst)} "
                            f"WHERE {_q(tcol['name'])} IS NOT NULL)"
                        ).fetchone()[0]
                    except Exception:
                        continue
                    nn = sinfo.get("column_stats", {}).get(col["name"], {}).get("non_null", 0)
                    if nn and (nn - bad) / nn >= min_containment:
                        rel = {"from_table": src, "from_col": col["name"], "to_table": dst, "to_col": tcol["name"], "inferred": True}
                        if bad:
                            rel["unmatched_rows"] = int(bad)   # an inner join drops these rows; surfaced to the user
                        inferred.append(rel)
                        known.add((src, col["name"], dst, tcol["name"]))
    return inferred


_PCT_NAME_RE = re.compile(r"(pct|percent|rate|ratio|share|margin|growth|discount|yield|conversion|churn|utili[sz]ation)", re.I)
_ISO_DATE_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*"
MAX_PROFILE_VALUES = 40
MAX_PROFILE_VALUE_LEN = 300


def _column_profile(conn: sqlite3.Connection, table: str, columns: List[Dict[str, Any]],
                    stats: Dict[str, Any]) -> Dict[str, Any]:
    """Per-column facts the SQL generator needs to avoid silent semantic errors:
    numeric range + percentage scale (0-1 fraction vs 0-100 points), ISO date
    range, and the full value list for low-cardinality text columns."""
    out: Dict[str, Any] = {}
    for col in columns:
        name, fam = col["name"], _type_family(col["type"])
        st = stats.get(name, {})
        nn = st.get("non_null", 0)
        if not nn:
            continue
        q = _q(name)
        try:
            if fam in ("int", "other"):
                mn, mx, av, nonint = conn.execute(
                    f"SELECT MIN({q}), MAX({q}), AVG({q}), "
                    f"SUM(CASE WHEN typeof({q}) = 'real' AND {q} <> CAST({q} AS INTEGER) THEN 1 ELSE 0 END) "
                    f"FROM {_q(table)} WHERE typeof({q}) IN ('integer','real') AND {q} BETWEEN -1e308 AND 1e308"
                ).fetchone()
                if mn is None:
                    continue
                entry: Dict[str, Any] = {"kind": "numeric", "min": mn, "max": mx, "avg": round(av, 4)}
                pct_like = bool(_PCT_NAME_RE.search(name))
                if mn >= -1.0 and mx <= 1.0 and (nonint or pct_like) and not col["pk"]:
                    entry["scale"] = "fraction_0_1"
                    entry["note"] = "values are fractions: 10% is stored as 0.10 (multiply by 100 for display)"
                elif pct_like and mn >= -100 and 1.0 < mx <= 100:
                    entry["scale"] = "percent_0_100"
                    entry["note"] = "values are percentage points: 10% is stored as 10"
                out[name] = entry
            else:
                if fam == "text":
                    iso = conn.execute(
                        f"SELECT COUNT(*) FROM {_q(table)} WHERE {q} IS NOT NULL AND {q} GLOB '{_ISO_DATE_GLOB}'"
                    ).fetchone()[0]
                    if iso == nn:
                        mn, mx, with_time = conn.execute(
                            f"SELECT MIN({q}), MAX({q}), MAX(length({q}) > 10) FROM {_q(table)}"
                        ).fetchone()
                        out[name] = {"kind": "date_text", "min": mn, "max": mx, "has_time": bool(with_time),
                                     "note": "ISO text; filter with >= start AND < next-day, or date(col) BETWEEN ..."
                                     if with_time else "ISO text YYYY-MM-DD; BETWEEN is inclusive of both ends"}
                        continue
            if fam == "text" and 0 < st.get("distinct", 0) <= MAX_PROFILE_VALUES:
                vals = [r[0] for r in conn.execute(
                    f"SELECT {q} FROM {_q(table)} WHERE {q} IS NOT NULL GROUP BY {q} ORDER BY COUNT(*) DESC, {q} LIMIT {MAX_PROFILE_VALUES}"
                )]
                full = [str(v) for v in vals]
                out[name] = {"kind": "categorical", "values": [v for v in full if len(v) <= MAX_PROFILE_VALUE_LEN],
                             "long_values_omitted": sum(1 for v in full if len(v) > MAX_PROFILE_VALUE_LEN)}
        except Exception:
            continue
    return out


def load_database(db_path: str) -> Dict[str, Any]:
    """Load a SQLite database and return full schema intelligence."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found: {db_path}")

    # Profiling only reads. Open read-only so an uploaded file cannot be modified
    # (hot-journal rollback, triggers) just by being profiled.
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;")
    tables = [row[0] for row in cur.fetchall()]
    if len(tables) > MAX_TABLES:
        conn.close()
        raise SchemaTooLarge(f"This database has {len(tables)} tables; the maximum supported is {MAX_TABLES}.")

    schema: Dict[str, Any] = {}
    total_rows = 0
    for table in tables:
        columns = _table_columns(cur, table)
        if len(columns) > MAX_COLUMNS_PER_TABLE:
            conn.close()
            raise SchemaTooLarge(
                f"Table {table!r} has {len(columns)} columns; the maximum supported is {MAX_COLUMNS_PER_TABLE}."
            )
        foreign_keys = _table_foreign_keys(cur, table)
        row_count = int(cur.execute(f'SELECT COUNT(*) FROM {_q(table)};').fetchone()[0])
        total_rows += row_count

        try:
            df = pd.read_sql_query(f'SELECT * FROM {_q(table)} LIMIT 1', conn)
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
        schema[table]["column_profile"] = _column_profile(conn, table, columns, schema[table]["column_stats"])
        try:   # statistics exports: hierarchy levels / versions that must never be added together
            from .long_format import detect as _detect_long_format
            lf = _detect_long_format(conn, table, columns, schema[table]["column_stats"], row_count)
        except Exception:  # noqa: BLE001 - structure detection is best-effort
            lf = None
        if lf:
            schema[table]["long_format"] = lf

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

    try:
        relationships.extend(_infer_relationships(conn, schema, relationships))
    except Exception:
        pass  # inference is best-effort; declared FKs remain authoritative

    size_bytes = os.path.getsize(db_path)
    dataset_id = _file_fingerprint(db_path)
    size_str = f"{size_bytes / 1024:.1f} KB" if size_bytes < 1024 * 1024 else f"{size_bytes / (1024 * 1024):.1f} MB"
    conn.close()

    return {
        "tables": schema,
        "relationships": relationships,
        "table_count": len(tables),
        "total_rows": total_rows,
        "db_size": size_str,
        "db_path": db_path,
        "dataset_id": dataset_id,
        "dataset_fingerprint": dataset_id,
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
            tag = " (inferred from names+data)" if r.get("inferred") else ""
            lines.append(f" {r['from_table']}.{r['from_col']} -> {r['to_table']}.{r['to_col']}{tag}")

    return "\n".join(lines)

def get_null_stats(db_path: str, table: str, column: str) -> float:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(f'SELECT COUNT(*) FROM {_q(table)};')
    total = int(cur.fetchone()[0] or 0)
    if total == 0:
        conn.close()
        return 0.0
    cur.execute(f'SELECT COUNT(*) FROM {_q(table)} WHERE {_q(column)} IS NULL;')
    nulls = int(cur.fetchone()[0] or 0)
    conn.close()
    return round((nulls / total) * 100, 2)