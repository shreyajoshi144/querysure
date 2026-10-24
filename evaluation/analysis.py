"""
Independent SQL analysis for the evaluation.

Why this exists separately from modules/sql_validator.py
--------------------------------------------------------
The validator is part of the system under test. If the evaluation only
trusted the validator's verdict, a validator blind spot would show up as
"no hallucinations". So hallucination is measured here by two mechanisms
that do not depend on the validator's code:

  1. AST scope analysis (sqlglot): every table reference and every column
     reference is resolved against the *actual* database catalog.
  2. SQLite itself: `EXPLAIN <sql>` compiles the statement without running
     it; SQLite reports "no such table" / "no such column" authoritatively.

A case counts as hallucinated if either mechanism finds an identifier that
does not exist. The report also records how many hallucinated queries the
validator nevertheless let through (validator escape rate).

Known approximations (documented in the README):
  * Unqualified columns are resolved against the union of all real tables the
    statement references, not per-subquery scope. This can under-count a
    hallucination but never invents one.
  * A double-quoted token that matches no column is treated as a string
    literal, which is what SQLite does.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import sqlglot
from sqlglot import exp

from .db import open_readonly

DIALECT = "sqlite"
_ROWID_NAMES = {"rowid", "oid", "_rowid_"}

_DATE_FUNC_NAMES = {"strftime", "date", "datetime", "julianday", "time", "unixepoch", "timediff"}
_DATE_FUNC_TYPES = tuple(
    t for t in (
        getattr(exp, n, None) for n in (
            "TimeToStr", "Date", "DateStrToDate", "CurrentDate", "CurrentTimestamp",
            "CurrentTime", "Extract", "DateAdd", "DateSub", "DateDiff", "DateTrunc",
            "TsOrDsToDate", "TsOrDsToTimestamp", "Year", "Month", "Day", "Datetime",
            "StrToTime", "StrToDate",
        )
    ) if t is not None
)

Catalog = Dict[str, Set[str]]  # lower-case table -> lower-case column names


def build_catalog(schema: Dict[str, Any]) -> Catalog:
    """Catalog from the dict returned by modules.schema_engine.load_database()."""
    return {
        t.lower(): {c["name"].lower() for c in info.get("columns", [])}
        for t, info in schema.get("tables", {}).items()
    }


@dataclass
class SQLAnalysis:
    parse_ok: bool = False
    parse_error: Optional[str] = None
    statement_count: int = 0
    statement_type: str = ""
    tables: List[str] = field(default_factory=list)          # real tables referenced
    hallucinated_tables: List[str] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)         # distinct referenced column names
    hallucinated_columns: List[str] = field(default_factory=list)
    dqs_literals: List[str] = field(default_factory=list)    # "double-quoted" strings treated as literals
    table_refs: int = 0
    column_refs: int = 0
    features: Dict[str, bool] = field(default_factory=dict)

    @property
    def hallucinated_identifiers(self) -> List[str]:
        return [f"table:{t}" for t in self.hallucinated_tables] + [f"column:{c}" for c in self.hallucinated_columns]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "parse_ok": self.parse_ok, "parse_error": self.parse_error,
            "statement_count": self.statement_count, "statement_type": self.statement_type,
            "tables": self.tables, "hallucinated_tables": self.hallucinated_tables,
            "columns": self.columns, "hallucinated_columns": self.hallucinated_columns,
            "table_refs": self.table_refs, "column_refs": self.column_refs,
            "features": self.features,
        }


def _features(stmt: exp.Expression) -> Dict[str, bool]:
    func_names: Set[str] = set()
    has_date = False
    for f in stmt.find_all(exp.Func):
        if isinstance(f, exp.Anonymous):
            n = (f.name or "").lower()
            func_names.add(n)
            if n in _DATE_FUNC_NAMES:
                has_date = True
        elif _DATE_FUNC_TYPES and isinstance(f, _DATE_FUNC_TYPES):
            has_date = True
    selects = list(stmt.find_all(exp.Select))
    return {
        "aggregation": any(True for _ in stmt.find_all(exp.AggFunc)),
        "group_by": stmt.find(exp.Group) is not None,
        "having": stmt.find(exp.Having) is not None,
        "order_by": stmt.find(exp.Order) is not None,
        "limit": stmt.find(exp.Limit) is not None,
        "date_function": has_date,
        "join": stmt.find(exp.Join) is not None,
        "subquery_or_cte": stmt.find(exp.CTE) is not None or len(selects) > 1,
        "distinct": stmt.find(exp.Distinct) is not None,
    }


_DERIVED = "<derived>"
_UNKNOWN_TABLE = "<unknown-table>"


def _analyze_statement(stmt: exp.Expression, catalog: Catalog, out: SQLAnalysis) -> None:
    cte_names = {(c.alias or "").lower() for c in stmt.find_all(exp.CTE) if c.alias}
    subq_aliases = {(s.alias or "").lower() for s in stmt.find_all(exp.Subquery) if s.alias}

    # qualifier (alias or bare table name) -> real table | derived | unknown
    qualifier_map: Dict[str, str] = {}
    real_tables_in_scope: Set[str] = set()
    has_unknown_table = False

    for tbl in stmt.find_all(exp.Table):
        name = (tbl.name or "").lower()
        if not name:
            continue
        alias = (tbl.alias or tbl.name).lower()
        out.table_refs += 1
        if name in cte_names:
            qualifier_map[alias] = _DERIVED
            qualifier_map.setdefault(name, _DERIVED)
        elif name in catalog:
            qualifier_map[alias] = name
            qualifier_map.setdefault(name, name)
            real_tables_in_scope.add(name)
            if name not in out.tables:
                out.tables.append(name)
        else:
            qualifier_map[alias] = _UNKNOWN_TABLE
            qualifier_map.setdefault(name, _UNKNOWN_TABLE)
            has_unknown_table = True
            if name not in out.hallucinated_tables:
                out.hallucinated_tables.append(name)

    output_aliases: Set[str] = {(a.alias or "").lower() for a in stmt.find_all(exp.Alias) if a.alias}
    for cte in stmt.find_all(exp.CTE):
        table_alias = cte.args.get("alias")
        for col_alias in (table_alias.args.get("columns") or []) if table_alias else []:
            output_aliases.add(str(col_alias.name).lower())

    seen_cols: Set[str] = set()
    for col in stmt.find_all(exp.Column):
        if isinstance(col.this, exp.Star):
            continue
        name = (col.name or "").lower()
        if not name:
            continue
        qual = (col.table or "").lower()
        out.column_refs += 1
        if name not in seen_cols:
            seen_cols.add(name)
            if name not in out.columns:
                out.columns.append(name)

        if qual:
            target = qualifier_map.get(qual)
            if target is None:
                if qual in cte_names or qual in subq_aliases:
                    continue  # column of a derived relation
                _add_unique(out.hallucinated_columns, f"{qual}.{name}")  # unknown alias
            elif target in (_DERIVED, _UNKNOWN_TABLE):
                continue  # cannot verify against a derived/unknown relation
            elif name not in catalog[target] and name not in _ROWID_NAMES:
                _add_unique(out.hallucinated_columns, f"{target}.{name}")
            continue

        # unqualified
        if name in output_aliases or name in _ROWID_NAMES:
            continue
        if any(name in catalog[t] for t in real_tables_in_scope):
            continue
        if has_unknown_table and not real_tables_in_scope:
            continue  # column may belong to the unknown table; already counted there
        if cte_names or subq_aliases:
            # could be an output column of a CTE/subquery whose projection we
            # did not enumerate; only flag when no derived relation exists
            derived_cols = {
                (p.alias_or_name or "").lower()
                for sel in stmt.find_all(exp.Select) for p in sel.expressions
            }
            if name in derived_cols:
                continue
        ident = col.this
        if isinstance(ident, exp.Identifier) and ident.args.get("quoted"):
            _add_unique(out.dqs_literals, name)  # SQLite falls back to a string literal
            continue
        _add_unique(out.hallucinated_columns, name)


def _add_unique(lst: List[str], item: str) -> None:
    if item not in lst:
        lst.append(item)


def analyze_sql(sql: str, catalog: Catalog) -> SQLAnalysis:
    out = SQLAnalysis()
    text = (sql or "").strip()
    if not text:
        out.parse_error = "empty SQL"
        return out
    try:
        stmts = [s for s in sqlglot.parse(text, read=DIALECT) if s is not None]
    except Exception as e:  # sqlglot raises ParseError / TokenError
        out.parse_error = str(e).splitlines()[0][:300]
        return out
    if not stmts:
        out.parse_error = "no statement produced"
        return out

    out.parse_ok = True
    out.statement_count = len(stmts)
    out.statement_type = type(stmts[0]).__name__
    for s in stmts:
        _analyze_statement(s, catalog, out)
    out.features = _features(stmts[0])
    return out


# ---------------------------------------------------------------------------
# Second, independent opinion: SQLite's own compiler
# ---------------------------------------------------------------------------

@dataclass
class SqliteCheck:
    ok: bool
    kind: Optional[str] = None      # unknown_table | unknown_column | syntax | other
    message: Optional[str] = None

    @property
    def is_hallucination(self) -> bool:
        return self.kind in ("unknown_table", "unknown_column")


def sqlite_compile_check(sql: str, db_path: str) -> SqliteCheck:
    """Ask SQLite to compile (not run) the statement. Read-only connection;
    EXPLAIN never executes, so even mutation statements are harmless here."""
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        return SqliteCheck(False, "syntax", "empty SQL")
    conn = open_readonly(db_path)
    try:
        conn.execute("EXPLAIN " + text).fetchall()
        return SqliteCheck(True)
    except sqlite3.Error as e:
        msg = str(e)
        low = msg.lower()
        if "no such table" in low:
            return SqliteCheck(False, "unknown_table", msg)
        if "no such column" in low:
            return SqliteCheck(False, "unknown_column", msg)
        if "syntax error" in low or "unrecognized token" in low or "incomplete input" in low:
            return SqliteCheck(False, "syntax", msg)
        return SqliteCheck(False, "other", msg)
    except Exception as e:  # e.g. "You can only execute one statement at a time"
        return SqliteCheck(False, "other", str(e))
    finally:
        conn.close()
