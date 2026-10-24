"""
SQL Validation Agent (AST-based)
Validates generated SQL for safety, schema consistency, and analytics correctness
using sqlglot's parser + optimizer instead of regex pattern matching.

Why this replaced the old regex version:
Regex keyword matching (`\bDROP\b`, `\bDELETE\b`, ...) only catches a mutation
keyword if it appears as a bare word in the raw text. It has no real concept of
"table", "column", or "join" -- the old validator reconstructed those with
hand-written regexes over raw text, which is why its own docstring called it
"regex-based, not a full AST parser." That approach:
  - misses stacked queries ("SELECT 1; DROP TABLE users;") since it never checks
    whether the input contains more than one statement,
  - can't validate unqualified columns at all (e.g. `SELECT made_up_col FROM t`
    with no table prefix was invisible to it),
  - resolves CTEs and aliases by pattern-matching text instead of actually
    tracking scope.

This version parses with sqlglot, then uses `sqlglot.optimizer.qualify` -- the
same column-resolution engine used by real query engines/linters -- to fully
qualify every column against the actual schema. That correctly threads through
CTEs, joins, and aliases, and it raises a real error the moment a column or
table can't be resolved, rather than silently passing through anything the
regex didn't happen to catch.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.errors import OptimizeError, ParseError

_READ_DIALECT = "sqlite"

_DISALLOWED_STATEMENTS: Tuple[type, ...] = tuple(
    t for t in (
        getattr(exp, "Drop", None),
        getattr(exp, "Delete", None),
        getattr(exp, "Insert", None),
        getattr(exp, "Update", None),
        getattr(exp, "Alter", None),
        getattr(exp, "Create", None),
        getattr(exp, "Command", None),  # PRAGMA, ATTACH, DETACH, VACUUM, etc.
    ) if t is not None
)


# Same set the execution sandbox denies via the SQLite authorizer (query_result_engine._DENIED_FUNCTIONS);
# rejected here too so a prompt-injected "SELECT load_extension(...)" is flagged at validation time,
# not only blocked at execution time.
_DENIED_FUNCTION_NAMES = frozenset({"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"})


def _denied_function_calls(statement: exp.Expression) -> List[str]:
    found = []
    for fn in statement.find_all(exp.Func):
        name = (getattr(fn, "name", "") or "").lower()
        if not name and isinstance(fn, exp.Anonymous):
            name = str(fn.this).lower()
        if name in _DENIED_FUNCTION_NAMES:
            found.append(name)
    return found


def _parse_statements(sql: str) -> Tuple[Optional[List[exp.Expression]], Optional[str]]:
    """Parse the *entire* input (not just the first statement) so stacked
    queries ("SELECT 1; DROP TABLE x;") are visible instead of silently
    truncated to whichever statement a naive parser lands on first."""
    try:
        statements = sqlglot.parse(sql, read=_READ_DIALECT)
        statements = [s for s in statements if s is not None]
        return statements, None
    except ParseError as e:
        return None, str(e)
    except Exception as e:
        return None, str(e)


def _cte_names(statement: exp.Expression) -> set:
    return {c.alias for c in statement.find_all(exp.CTE) if c.alias}


def _all_tables(statement: exp.Expression) -> List[Tuple[str, str]]:
    """Returns (alias, real_name) for every table reference, including
    references to a CTE by name (those get filtered out by the caller using
    _cte_names, since a CTE isn't a schema table)."""
    out = []
    for table in statement.find_all(exp.Table):
        name = table.name
        alias = table.alias or name
        out.append((alias, name))
    return out


def _all_joins(statement: exp.Expression) -> List[exp.Join]:
    return list(statement.find_all(exp.Join))


def _join_equalities(join: exp.Join) -> List[Tuple[str, str, str, str]]:
    on = join.args.get("on")
    if on is None:
        return []
    pairs = []
    for eq in on.find_all(exp.EQ):
        left, right = eq.this, eq.expression
        if isinstance(left, exp.Column) and isinstance(right, exp.Column):
            if left.table and right.table:
                pairs.append((left.table, left.name, right.table, right.name))
    return pairs


def _has_select_star(statement: exp.Expression) -> bool:
    for select in statement.find_all(exp.Select):
        for e in select.expressions:
            if isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)):
                return True
    return False


_AGG_FUNC_TYPES = (exp.Sum, exp.Avg, exp.Count, exp.Min, exp.Max)


def _is_aggregated_expr(e: exp.Expression) -> bool:
    return any(e.find(f) is not None for f in _AGG_FUNC_TYPES)


def _root_select(statement: exp.Expression) -> Optional[exp.Select]:
    if isinstance(statement, exp.Select):
        return statement
    if isinstance(statement, exp.With):
        inner = statement.this
        return inner if isinstance(inner, exp.Select) else None
    if isinstance(statement, exp.Union):
        left = statement.left
        return left if isinstance(left, exp.Select) else None
    return None


def _group_by_column_names(select: exp.Select) -> List[str]:
    group = select.args.get("group")
    if not group:
        return []
    names = []
    for e in group.expressions:
        names.append(e.name if isinstance(e, exp.Column) else e.sql(dialect=_READ_DIALECT))
    return names


def _projection_alias_or_name(e: exp.Expression) -> str:
    if isinstance(e, exp.Alias):
        return e.alias_or_name
    if isinstance(e, exp.Column):
        return e.name
    return e.sql(dialect=_READ_DIALECT)


def _origin(scope, col: exp.Column, depth: int = 0):
    """Trace a qualified column to its (base_table, base_column) through CTEs / derived tables.
    Returns None when the column is computed (aggregate/expression) or cannot be traced."""
    if depth > 8 or not col.table:
        return None
    src = scope.sources.get(col.table)
    if isinstance(src, exp.Table):
        return (src.name, col.name)
    if src is None or not hasattr(src, "expression"):
        return None
    selects = getattr(src.expression, "selects", None) or []
    for proj in selects:
        if proj.alias_or_name.lower() == col.name.lower():
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            if isinstance(inner, exp.Column) and inner.table:
                return _origin(src, inner, depth + 1)
            if inner.find(exp.AggFunc) is not None:
                return ("(aggregate)", col.name)  # joining on an aggregate output is never a real relationship
            return None
    return None


def _scoped_join_equalities(statement: exp.Expression):
    """[(origin_left, origin_right, text)] for every join equality, resolved per scope so the same alias
    reused in different subqueries cannot be confused, and CTE/derived-table keys are traced to base columns."""
    from sqlglot.optimizer.scope import traverse_scope
    out, cartesian = [], []
    for scope in traverse_scope(statement):
        node = scope.expression
        if not isinstance(node, exp.Select):
            continue
        where = node.args.get("where")
        where_eqs = list(where.this.find_all(exp.EQ)) if where is not None else []
        for join in node.args.get("joins") or []:
            on = join.args.get("on")
            eqs = list(on.find_all(exp.EQ)) if on is not None else []
            joined_alias = join.this.alias_or_name if join.this is not None else None
            if on is None and not join.args.get("using"):
                # comma / CROSS join: only acceptable if WHERE links the joined table
                linked = [e for e in where_eqs if isinstance(e.this, exp.Column) and isinstance(e.expression, exp.Column)
                          and joined_alias in (e.this.table, e.expression.table)]
                if not linked:
                    cartesian.append(joined_alias)
                eqs = linked
            for eq in eqs:
                l, r = eq.this, eq.expression
                if isinstance(l, exp.Column) and isinstance(r, exp.Column) and l.table and r.table:
                    out.append((_origin(scope, l), _origin(scope, r), f"{l.table}.{l.name} = {r.table}.{r.name}",
                                _grouped_key(scope, l) and _grouped_key(scope, r)))
    return out, cartesian


def _grouped_key(scope, col: exp.Column) -> bool:
    """True when ``col`` is the only GROUP BY key of the CTE/subquery it comes from, so it is unique there: joining two
    such sources (e.g. per-industry sales and per-industry profit) is one-to-one even without a declared relationship."""
    from sqlglot.optimizer.scope import Scope
    src = scope.sources.get(col.table)
    sel = src.expression if isinstance(src, Scope) else None
    group = sel.args.get("group") if isinstance(sel, exp.Select) else None
    if group is None or len(group.expressions) != 1:
        return False
    key = group.expressions[0]
    for proj in sel.expressions:
        if proj.alias_or_name.lower() != col.name.lower():
            continue
        inner = proj.this if isinstance(proj, exp.Alias) else proj
        if inner == key:
            return True
        # GROUP BY may name the output alias, or the qualifier may have rewritten one side
        return isinstance(key, exp.Column) and (key.name.lower() == col.name.lower()
                                                or (isinstance(inner, exp.Column) and key.name.lower() == inner.name.lower()))
    return False


def issues_block_scope(issues: list) -> bool:
    return any(i["type"] in {"unknown_table", "unknown_column", "unknown_alias", "schema_resolution_error", "dangerous_sql"} for i in issues)


def _is_unique_col(schema: dict, table: str, col: str, canon: dict) -> bool:
    t = (schema.get("tables") or {}).get(table) or (schema.get("tables") or {}).get(canon.get(str(table).lower(), ""))
    if not t:
        return False
    for c in t.get("columns", []):
        if c["name"].lower() == str(col).lower():
            st = (t.get("column_stats") or {}).get(c["name"], {})
            return bool(c.get("pk")) or (st.get("rows") and st.get("distinct") == st.get("non_null") == st.get("rows"))
    return False


def _long_format_key_join(schema: dict, left, right, canon: dict) -> bool:
    """A long-format table joined with itself on one of its key columns (period = period, industry = industry): how
    one variable is put next to another (salaries / sales) without conditional aggregation."""
    if str(left[0]).lower() != str(right[0]).lower() or str(left[1]).lower() != str(right[1]).lower():
        return False
    tables = schema.get("tables") or {}
    info = (tables.get(left[0]) or tables.get(canon.get(str(left[0]).lower(), "")) or {}).get("long_format")
    if not info:
        return False
    keys = {k.lower() for k in info["key"]} | {a.lower() for c in info["columns"] for a in c.get("aliases", [])}
    return str(left[1]).lower() in keys


def _relationship_pairs(schema: dict) -> set:
    pairs = set()
    for r in schema.get("relationships", []):
        # SQLite identifiers are case-insensitive and sqlglot's qualifier lower-cases
        # unquoted names, so compare case-folded.
        a = (str(r["from_table"]).lower(), str(r["from_col"]).lower(),
             str(r["to_table"]).lower(), str(r["to_col"]).lower())
        pairs.add(a)
        pairs.add((a[2], a[3], a[0], a[1]))
    return pairs


def _schema_map_for_qualify(schema: dict) -> Dict[str, Dict[str, str]]:
    return {
        tname: {c["name"]: (c["type"] or "TEXT") for c in tinfo.get("columns", [])}
        for tname, tinfo in schema.get("tables", {}).items()
    }


_UNRESOLVED_COL_RE = re.compile(r"Column '([^']+)' could not be resolved")


def _metric_validation(sql: str, semantic: dict | None) -> List[str]:
    warnings = []
    if not semantic:
        return warnings
    lower = sql.lower()
    for metric_name, definition in semantic.get("metric_definitions", {}).items():
        if metric_name in lower:
            if metric_name == "total_revenue":
                if "unit_price" in definition.lower() and "quantity" in definition.lower():
                    if "unit_price" in lower and "quantity" not in lower:
                        warnings.append("Potential metric mismatch: total_revenue usually requires quantity and unit_price.")
            if metric_name == "return_rate" and "returned" not in lower:
                warnings.append("Potential metric mismatch: return_rate may require returned-status filtering.")
    return warnings


def _invalid(reason_type: str, message: str, normalized: str = "") -> dict:
    return {
        "is_valid": False,
        "validation_score": 0,
        "risk_score": 100,
        "hallucination_score": 1.0,
        "issues": [{"type": reason_type, "message": message}],
        "warnings": [],
        "validated_sql": normalized,
        "join_validation": {"valid": False},
        "aggregation_validation": {"valid": False},
        "fanout_risk": "High",
    }


def validate_sql(sql: str, schema: dict, semantic: dict | None = None) -> dict:
    """Validate SQL against schema and analytics guardrails using an AST parse
    instead of regex. Return contract matches the previous regex-based version."""
    normalized = sql.strip()
    if not normalized:
        return _invalid("empty_sql", "SQL is empty.")

    issues: List[Dict[str, Any]] = []
    warnings: List[str] = []

    statements, parse_error = _parse_statements(normalized)
    if parse_error is not None or not statements:
        return _invalid("parse_error", f"SQL failed to parse: {parse_error or 'no statement produced'}", normalized)

    if len(statements) > 1:
        issues.append({
            "type": "multiple_statements",
            "message": f"Input contains {len(statements)} SQL statements; only one is allowed per request.",
        })

    statement = statements[0]

    if isinstance(statement, _DISALLOWED_STATEMENTS) or not isinstance(statement, (exp.Select, exp.With, exp.Union)):
        issues.append({"type": "dangerous_sql", "message": f"Statement type not allowed for execution: {type(statement).__name__}"})

    for denied in _denied_function_calls(statement):
        issues.append({"type": "dangerous_sql", "message": f"Function not allowed: {denied}"})

    has_select_star = _has_select_star(statement)
    if has_select_star:
        # A star is a read-only projection of known tables: it cannot write, reach other tables or bypass the
        # authorizer, so it is reported, not rejected ("show all tasks for Marketing" is a legitimate listing).
        warnings.append("SELECT * returns every column; explicit columns are preferred.")

    cte_names = _cte_names(statement)
    alias_map: Dict[str, str] = {}
    for alias, real in _all_tables(statement):
        alias_map[alias] = real
        alias_map[real] = real

    # SQLite identifiers are case-insensitive: resolve table references to the schema's canonical spelling.
    _canon = {}
    for _t in (schema.get("tables") or {}):
        _canon.setdefault(_t.lower(), _t)
    for _k, _v in list(alias_map.items()):
        if _v not in (schema.get("tables") or {}) and _v.lower() in _canon:
            alias_map[_k] = _canon[_v.lower()]
    table_names = list(dict.fromkeys(alias_map.values()))
    available_tables = set(schema.get("tables", {}).keys())
    unknown_tables = [t for t in table_names if t not in available_tables and t not in cte_names]
    for t in unknown_tables:
        issues.append({"type": "unknown_table", "message": f"Table not found in schema: {t}"})

    # Column resolution: only trustworthy once every table reference is known
    # (qualify() can't validate columns against a table it's never heard of).
    qualified_statement = statement
    if not unknown_tables and not any(i["type"] == "dangerous_sql" for i in issues):
        try:
            schema_map = _schema_map_for_qualify(schema)
            qualified_statement = qualify(
                statement.copy(),
                schema=schema_map,
                dialect=_READ_DIALECT,
                validate_qualify_columns=True,
                quote_identifiers=False,
            )
        except OptimizeError as e:
            msg = str(e)
            m = _UNRESOLVED_COL_RE.search(msg)
            col_name = m.group(1) if m else "?"
            issues.append({"type": "unknown_column", "message": f"Column could not be resolved against the schema: {col_name}"})
        except Exception as e:
            # Never turn schema-resolution uncertainty into an executable query.
            # A generic qualifier failure can represent an unresolved column,
            # alias/scope problem, or an unsupported construct. Treating it as a
            # warning would let a nonexistent field reach the execution layer.
            issues.append({
                "type": "schema_resolution_error",
                "message": f"SQL could not be resolved against the current dataset schema: {e}",
            })

    # Compile with SQLite's own parser (against an empty in-memory twin of the schema) so anything SQLite itself
    # would reject at execution time -- reserved words used as identifiers, ambiguous columns -- fails HERE.
    if not any(i["type"] in {"dangerous_sql", "multiple_statements"} for i in issues) and schema.get("tables"):
        from .sqlite_compile_check import compile_check
        c_ok, c_err = compile_check(normalized, schema)
        if not c_ok:
            issues.append({"type": "sqlite_compile_error", "message": f"SQLite cannot compile this query: {c_err}. "
                           "Quote identifiers that are SQL keywords with double quotes."})

    # Joins & fan-out risk, evaluated on the qualified tree when we have one
    # so aliases are unambiguous.
    joins = _all_joins(qualified_statement)
    relationship_pairs = _relationship_pairs(schema)
    invalid_relationships: List[str] = []
    join_equalities: List[Tuple[str, str, str, str]] = []
    for j in joins:
        join_equalities.extend(_join_equalities(j))

    unresolved_join_keys: List[str] = []
    if not issues_block_scope(issues):
        try:
            scoped, cartesian = _scoped_join_equalities(qualified_statement)
        except Exception:
            scoped, cartesian = [], []
        for left, right, text, grouped_keys in scoped:
            if left is None or right is None:
                unresolved_join_keys.append(text)  # computed key: cannot be traced; surfaced as a warning
                continue
            k1 = tuple(str(x).lower() for x in (*left, *right))
            same_unique_col = (k1[0] == k1[2] and k1[1] == k1[3] and _is_unique_col(schema, left[0], left[1], _canon))
            ok = (k1 in relationship_pairs or (k1[2], k1[3], k1[0], k1[1]) in relationship_pairs or same_unique_col
                  or grouped_keys or _long_format_key_join(schema, left, right, _canon))
            for r in schema.get("relationships", []):
                rk = (str(r["from_table"]).lower(), str(r["from_col"]).lower(), str(r["to_table"]).lower(), str(r["to_col"]).lower())
                if r.get("unmatched_rows") and (rk == k1 or rk == (k1[2], k1[3], k1[0], k1[1])):
                    warnings.append(f"{r['unmatched_rows']} row(s) of {r['from_table']} have a {r['from_col']} with no matching "
                                    f"{r['to_table']}.{r['to_col']}; an inner join leaves them out (use LEFT JOIN to keep them).")
            if not ok:
                invalid_relationships.append(f"{left[0]}.{left[1]} -> {right[0]}.{right[1]}")
                issues.append({"type": "invalid_join_relationship", "message": f"Join relationship not found in schema: {left[0]}.{left[1]} = {right[0]}.{right[1]}"})
        for alias in cartesian:
            issues.append({"type": "cartesian_join", "message": f"Join of {alias} has no join condition (Cartesian product)."})
        for text in unresolved_join_keys:
            warnings.append(f"Join on a computed expression could not be checked against known relationships: {text}")

    fanout_risk = "Low"
    if len(join_equalities) >= 2:
        many_sides = 0
        for a1, c1, a2, c2 in join_equalities:
            t1 = alias_map.get(a1, a1)
            t2 = alias_map.get(a2, a2)
            t1_pk = any(col.get("pk") and col["name"] == c1 for col in schema.get("tables", {}).get(t1, {}).get("columns", []))
            t2_pk = any(col.get("pk") and col["name"] == c2 for col in schema.get("tables", {}).get(t2, {}).get("columns", []))
            if not t1_pk and not t2_pk:
                many_sides += 1
        if many_sides:
            fanout_risk = "High" if many_sides > 1 else "Medium"
            warnings.append("Potential fan-out risk due to joins on non-primary key fields.")

    # Aggregation / GROUP BY correctness on the (possibly qualified) select.
    agg_valid = True
    select = _root_select(qualified_statement)
    if select is not None:
        projections = select.expressions
        aggregation_present = any(_is_aggregated_expr(e) for e in projections)
        group_by_cols = set(_group_by_column_names(select))
        non_agg = [e for e in projections if not _is_aggregated_expr(e) and not isinstance(e, exp.Star)]
        if aggregation_present and non_agg:
            for e in non_agg:
                name = _projection_alias_or_name(e)
                if name not in group_by_cols:
                    agg_valid = False
                    warnings.append(f"Non-aggregated select item may require GROUP BY: {e.sql(dialect=_READ_DIALECT)}")
            if not group_by_cols:
                agg_valid = False

    warnings.extend(_metric_validation(normalized, semantic))
    if semantic and semantic.get("metric_definitions"):
        metric_names = list(semantic["metric_definitions"].keys())
        if any(m in normalized.lower() for m in metric_names):
            warnings.append("Query references known business metrics.")

    risk_score = 0
    risk_score += 60 if any(i["type"] == "dangerous_sql" for i in issues) else 0
    risk_score += 50 if any(i["type"] == "multiple_statements" for i in issues) else 0
    risk_score += 25 if any(i["type"] in {"unknown_table", "unknown_column", "unknown_alias"} for i in issues) else 0
    risk_score += 20 if any(i["type"] == "invalid_join_relationship" for i in issues) else 0
    risk_score += 15 if not agg_valid else 0
    risk_score += 10 if has_select_star else 0
    risk_score += 5 if warnings else 0
    risk_score = min(100, risk_score)

    total_refs = len(table_names) + len(list(statement.find_all(exp.Column))) + len(join_equalities)
    hallucination_hits = sum(1 for i in issues if i["type"] in {"unknown_table", "unknown_column", "unknown_alias", "schema_resolution_error", "invalid_join_relationship"})
    hallucination_score = min(1.0, hallucination_hits / max(1, total_refs))
    validation_score = max(0, 100 - risk_score)

    is_valid = not any(
        i["type"] in {
            "dangerous_sql", "multiple_statements", "unknown_table",
            "unknown_column", "unknown_alias", "schema_resolution_error", "invalid_join_relationship",
            "cartesian_join", "sqlite_compile_error",
        }
        for i in issues
    )

    return {
        "is_valid": is_valid,
        "validation_score": validation_score,
        "risk_score": risk_score,
        "hallucination_score": round(hallucination_score, 4),
        "issues": issues,
        "warnings": warnings,
        "validated_sql": normalized,
        "join_validation": {"valid": len(invalid_relationships) == 0, "invalid_relationships": invalid_relationships},
        "aggregation_validation": {"valid": agg_valid},
        "fanout_risk": fanout_risk,
    }
