"""Compile a query with SQLite's OWN parser against an empty in-memory twin of the schema.

Why: sqlglot is lenient in places SQLite is not (e.g. an unquoted reserved word used as a
column: ``SELECT order FROM t`` parses in sqlglot but fails in SQLite), so a query could pass
validation and then fail at execution. EXPLAIN prepares the statement without running it, so
syntax errors, reserved-word identifiers, unknown/ambiguous columns and unknown tables are
caught by the same engine that will execute the SQL, independent of data and file location.

The twin is :memory:, holds no data, and has an authorizer that only allows read access, so
this adds no data exposure and cannot modify anything.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Dict, Optional, Tuple

SQLITE_KEYWORDS = frozenset("""
abort action add after all alter always analyze and as asc attach autoincrement before begin between by cascade case cast check
collate column commit conflict constraint create cross current current_date current_time current_timestamp database default
deferrable deferred delete desc detach distinct do drop each else end escape except exclusive exists explain fail filter first
following for foreign from full generated glob group groups having if ignore immediate in index indexed initially inner insert
instead intersect into is isnull join key last left like limit match materialized natural no not nothing notnull null nulls of
offset on or order others outer over partition plan pragma preceding primary query raise range recursive references regexp
reindex release rename replace restrict returning right rollback row rows savepoint select set table temp temporary then ties to
transaction trigger unbounded union unique update using vacuum values view virtual when where window with without
""".split())


def needs_quoting(name: str) -> bool:
    return (name or "").lower() in SQLITE_KEYWORDS or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or "")


def quote_ident(name: str) -> str:
    """Quote an identifier only when SQLite requires it (keyword or unusual characters)."""
    n = str(name)
    return '"' + n.replace('"', '""') + '"' if needs_quoting(n) else n


def _build_twin(schema: dict) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    for tname, tinfo in (schema.get("tables") or {}).items():
        cols = ", ".join(
            '"%s" %s' % (str(c["name"]).replace('"', '""'), re.sub(r"[^A-Za-z0-9 ()]", "", str(c.get("type") or "TEXT")) or "TEXT")
            for c in tinfo.get("columns", [])
        )
        if cols:
            con.execute('CREATE TABLE "%s" (%s)' % (str(tname).replace('"', '""'), cols))
    # Read-only from here on: allow SELECT/READ/FUNCTION/RECURSIVE only.
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", 33)}
    con.set_authorizer(lambda action, *_a: sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY)
    return con


def compile_check(sql: str, schema: dict) -> Tuple[bool, Optional[str]]:
    """Return (ok, error_message). Never raises."""
    try:
        con = _build_twin(schema)
    except Exception as exc:  # twin could not be built (exotic type names) -> do not block on our own limitation
        return True, None
    try:
        con.execute("EXPLAIN " + sql.strip().rstrip(";")).fetchall()
        return True, None
    except sqlite3.Error as exc:
        return False, str(exc)
    except Exception:
        return True, None
    finally:
        con.close()


def autoquote_reserved(sql: str, schema: dict) -> Optional[str]:
    """Deterministic repair for unquoted reserved-word identifiers that exist in the schema.

    Quotes tokens equal (case-insensitively) to a schema column/table that is an SQLite keyword,
    except where the keyword is plainly a clause keyword (ORDER/GROUP followed by BY). The
    candidate is returned ONLY if it compiles; otherwise None. Never changes string literals."""
    reserved = set()
    for tname, tinfo in (schema.get("tables") or {}).items():
        if needs_quoting(tname):
            reserved.add(str(tname).lower())
        for c in tinfo.get("columns", []):
            if needs_quoting(c["name"]):
                reserved.add(str(c["name"]).lower())
    reserved = {r for r in reserved if r in SQLITE_KEYWORDS}
    if not reserved:
        return None
    try:
        from sqlglot.tokens import Tokenizer, TokenType
        tokens = Tokenizer(dialect="sqlite").tokenize(sql)
    except Exception:
        return None
    out, last = [], 0
    for i, t in enumerate(tokens):
        txt = sql[t.start:t.end + 1]
        if t.token_type in (TokenType.STRING, TokenType.IDENTIFIER) or txt.lower() not in reserved:
            continue
        nxt = tokens[i + 1].text.lower() if i + 1 < len(tokens) else ""
        if txt.lower() in ("order", "group") and nxt == "by":
            continue
        out.append(sql[last:t.start]); out.append('"%s"' % txt); last = t.end + 1
    if not out:
        return None
    out.append(sql[last:])
    candidate = "".join(out)
    ok, _ = compile_check(candidate, schema)
    return candidate if ok else None


def requote_string_literals(sql: str, schema: dict) -> Optional[str]:
    """Deterministic repair for values written in double quotes (``WHERE status = "Unadjusted"``).

    In SQLite double quotes delimit identifiers; such a "value" only works by accident (when no column has that name),
    so the validator rejects it as an unknown column. A double-quoted name that is NOT a table, column or alias anywhere
    in the statement and that is compared with something (=, <>, IN, LIKE) is turned into a single-quoted string.
    The candidate is returned only when it differs; the caller re-validates it like any other SQL."""
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return None
    known = {str(t).lower() for t in (schema.get("tables") or {})}
    for tinfo in (schema.get("tables") or {}).values():
        known |= {str(c["name"]).lower() for c in tinfo.get("columns", [])}
    known |= {a.alias.lower() for a in tree.find_all(exp.Alias) if a.alias}
    known |= {t.alias_or_name.lower() for t in tree.find_all(exp.Table)}
    known |= {c.alias.lower() for c in tree.find_all(exp.CTE) if c.alias}
    changed = False
    for col in list(tree.find_all(exp.Column)):
        ident = col.this
        if col.table or not isinstance(ident, exp.Identifier) or not ident.quoted or ident.name.lower() in known:
            continue
        parent = col.parent
        compared = isinstance(parent, (exp.EQ, exp.NEQ, exp.Like, exp.ILike)) or (
            isinstance(parent, exp.In) and col is not parent.this)
        if not compared:
            continue
        col.replace(exp.Literal.string(ident.name))
        changed = True
    if not changed:
        return None
    return tree.sql(dialect="sqlite")
