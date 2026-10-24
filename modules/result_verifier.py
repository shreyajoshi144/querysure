"""Pre-execution semantic validation and post-execution result verification.

Separates four things the product used to blur together:
  syntax/security  -> sql_validator            (is it safe and does SQLite accept it?)
  semantic         -> semantic_validate        (does it implement what was asked? are filter values real?)
  execution        -> execute_query            (did it run?)
  correctness      -> verify_result            (does an INDEPENDENT calculation agree? is an empty result plausible?)
`verify_result` never says an answer is "correct" unless an independent reference calculation agreed.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional

import sqlglot
from sqlglot import exp

from .query_planner import build_plan, check_sql_against_plan, reference_sql
from .query_result_engine import execute_query
from .sql_intent_checker import check_intent
from .value_grounding import ground_question, nearest_values, norm, value_exists


def _string_filters(sql: str, schema: dict) -> List[Dict[str, str]]:
    """[(table, column, literal)] for column = 'lit' / column IN ('a','b') on schema text columns."""
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return []
    tables = [t.name for t in tree.find_all(exp.Table)]
    canon = {t.lower(): t for t in (schema.get("tables") or {})}
    tables = [canon.get(t.lower(), t) for t in tables]
    alias_to_table = {}
    for t in tree.find_all(exp.Table):
        alias_to_table[(t.alias or t.name).lower()] = canon.get(t.name.lower(), t.name)
    out = []

    def resolve(col: exp.Column):
        if col.table and col.table.lower() in alias_to_table:
            cands = [alias_to_table[col.table.lower()]]
        else:
            cands = tables
        for tn in cands:
            tinfo = (schema.get("tables") or {}).get(tn) or {}
            for c in tinfo.get("columns", []):
                if c["name"].lower() == col.name.lower():
                    return tn, c["name"]
        return None

    for node in tree.find_all(exp.EQ):
        l, r = node.this, node.expression
        for col, lit in ((l, r), (r, l)):
            if isinstance(col, exp.Column) and isinstance(lit, exp.Literal) and lit.is_string:
                res = resolve(col)
                if res:
                    out.append({"table": res[0], "column": res[1], "literal": lit.this})
    for node in tree.find_all(exp.In):
        col = node.this
        if isinstance(col, exp.Column):
            res = resolve(col)
            for lit in node.expressions:
                if res and isinstance(lit, exp.Literal) and lit.is_string:
                    out.append({"table": res[0], "column": res[1], "literal": lit.this})
    return out


def semantic_validate(question: str, sql: str, schema: dict, db_path: Optional[str] = None,
                      plan: Optional[dict] = None, grounded: Optional[list] = None) -> Dict[str, Any]:
    """Pre-execution: structure vs plan, intent, and EXACT filter values against the real data."""
    if grounded is None and db_path:
        grounded = ground_question(question, schema, db_path)
    plan = plan if plan is not None else build_plan(question, schema, grounded)
    issues: List[Dict[str, str]] = []
    issues += check_sql_against_plan(sql, plan)
    from .long_format import check_sql as _long_format_issues
    seen = {i["message"] for i in issues}
    for i in check_intent(question, sql, schema) + _long_format_issues(question, sql, schema, grounded, db_path):
        if i["message"] not in seen:
            issues.append(i)
    if db_path:
        for f in _string_filters(sql, schema):
            if not value_exists(db_path, f["table"], f["column"], f["literal"]):
                near = nearest_values(db_path, f["table"], f["column"], f["literal"])
                msg = f"The value {f['literal']!r} does not exist in {f['table']}.{f['column']}."
                if near:
                    msg += " Stored value(s) closest to it: " + "; ".join(repr(n) for n in near) + " (use the stored spelling exactly)."
                issues.append({"type": "value_not_found", "message": msg, "table": f["table"], "column": f["column"],
                               "literal": f["literal"], "suggestions": near})
    return {"status": "ok" if not issues else "needs_attention", "issues": issues, "plan": plan}


# ---------------------------------------------------------------------------------------------------------------
def _num(x) -> Optional[float]:
    if isinstance(x, bool) or x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    return None


def _canon_rows(rows: List[tuple]) -> List[tuple]:
    def cell(v):
        n = _num(v)
        return ("n", round(n, 6)) if n is not None else ("s", "" if v is None else str(v))
    return sorted(tuple(cell(v) for v in r) for r in rows)


def _diagnose_empty(sql: str, schema: dict, db_path: str) -> List[str]:
    out = []
    for f in _string_filters(sql, schema):
        if not value_exists(db_path, f["table"], f["column"], f["literal"]):
            near = nearest_values(db_path, f["table"], f["column"], f["literal"])
            out.append(f"{f['table']}.{f['column']} has no value {f['literal']!r}" + (f"; closest stored: {near}" if near else ""))
    return out


def verify_result(question: str, sql: str, schema: dict, exec_result: dict, db_path: str,
                  plan: Optional[dict] = None, grounded: Optional[list] = None) -> Dict[str, Any]:
    """Post-execution: honest, evidence-based statement about the result.

    status values: verified_match | mismatch | suspicious_empty | empty_plausible | not_independently_verified | error
    `verified` is True ONLY for verified_match."""
    if grounded is None and db_path:
        grounded = ground_question(question, schema, db_path)
    plan = plan if plan is not None else build_plan(question, schema, grounded)
    checks: List[Dict[str, str]] = []
    out: Dict[str, Any] = {"status": "not_independently_verified", "verified": False, "checks": checks, "plan": plan,
                           "reference_sql": None}
    if not exec_result.get("success"):
        out["status"] = "error"
        return out
    rows = exec_result.get("rows", [])
    cols = exec_result.get("columns", [])

    # 1) empty results
    if not rows:
        diag = _diagnose_empty(sql, schema, db_path)
        if diag:
            out["status"] = "suspicious_empty"
            out["diagnosis"] = diag
            checks.append({"check": "empty_result", "result": "suspicious", "detail": "; ".join(diag)})
        else:
            out["status"] = "empty_plausible"
            checks.append({"check": "empty_result", "result": "unconfirmed",
                           "detail": "No rows. Every filter value exists on its own, so the combination of filters/ranges may simply match nothing. Not independently confirmed."})
        return out

    # 2) cheap sanity checks
    tuples = [tuple(r.get(c) for c in cols) for r in rows]
    if len(rows) == 1 and all(v is None for v in tuples[0]):
        checks.append({"check": "null_aggregate", "result": "suspicious", "detail": "The aggregate returned NULL (no rows matched, or the column is entirely NULL)."})
        out["status"] = "suspicious_empty"
        out["diagnosis"] = _diagnose_empty(sql, schema, db_path) or ["Aggregate over zero matching rows returned NULL."]
        return out
    if exec_result.get("execution_metadata", {}).get("truncated"):
        checks.append({"check": "row_cap", "result": "warning", "detail": "Result hit the row cap; totals computed from displayed rows would be incomplete."})

    # 3) independent reference calculation (separate code path from the generated SQL)
    ref = reference_sql(plan)
    if not ref:
        checks.append({"check": "reference", "result": "skipped", "detail": "; ".join(plan.get("notes") or ["question shape not supported for independent recomputation"])})
        return out
    ref_sql, params = ref
    out["reference_sql"] = ref_sql
    ref_res = execute_query(ref_sql, db_path, params=params)
    if not ref_res.get("success"):
        checks.append({"check": "reference", "result": "skipped", "detail": "reference query could not run"})
        return out
    ref_rows = [tuple(r.get(c) for c in ref_res["columns"]) for r in ref_res["rows"]]
    # compare on the label+value columns: find in generated rows a projection equal to the reference
    n_ref = len(ref_res["columns"])
    matched = False
    if len(cols) >= n_ref:
        from itertools import permutations
        ref_canon = _canon_rows(ref_rows)
        for idxs in permutations(range(len(cols)), n_ref):
            if _canon_rows([tuple(t[i] for i in idxs) for t in tuples]) == ref_canon:
                matched = True
                break
        if not matched and plan.get("limit"):
            # ties at the LIMIT boundary may legitimately pick different rows: compare the value column only
            vref = sorted(round(_num(r[-1]) or 0.0, 6) for r in ref_rows)
            for i in range(len(cols)):
                v = sorted(round(_num(t[i]) or 0.0, 6) for t in tuples if _num(t[i]) is not None)
                if v == vref:
                    matched = True
                    break
    if matched:
        out["status"], out["verified"] = "verified_match", True
        checks.append({"check": "reference", "result": "match", "detail": "An independent recomputation from the question's plan produced the same rows."})
    else:
        out["status"] = "mismatch"
        out["reference_preview"] = [list(r) for r in ref_rows[:5]]
        checks.append({"check": "reference", "result": "mismatch",
                       "detail": "An independent recomputation from the question's plan gave different rows; the SQL may not answer the question."})
    return out


# ---------------------------------------------------------------------------------------------------------------
def correctness_dimensions(validation: dict, semantic: Optional[dict], exec_result: Optional[dict], verification: Optional[dict]) -> Dict[str, Dict[str, str]]:
    """Five separately reported dimensions. A passing earlier dimension never implies a later one."""
    v = validation or {}
    issues = v.get("issues", [])
    sec_types = {"dangerous_sql", "multiple_statements", "select_star"}
    syn_types = {"parse_error", "sqlite_compile_error", "unknown_table", "unknown_column", "unknown_alias", "schema_resolution_error",
                 "invalid_join_relationship", "cartesian_join", "empty_sql"}
    dims = {
        "syntax": {"status": "pass" if v and not any(i["type"] in syn_types for i in issues) else ("unknown" if not v else "fail"),
                   "detail": "SQL parses, compiles in SQLite and references only known tables/columns/relationships."},
        "security": {"status": "pass" if v and not any(i["type"] in sec_types for i in issues) else ("unknown" if not v else "fail"),
                     "detail": "Read-only single statement; no denied functions."},
    }
    if semantic is None:
        dims["semantic_validation"] = {"status": "not_run", "detail": "No question-to-SQL semantic check was recorded."}
    else:
        dims["semantic_validation"] = {"status": "pass" if not semantic.get("issues") else "needs_attention",
                                       "detail": "; ".join(i["message"] for i in semantic.get("issues", [])[:3]) or "Filters, aggregation, grouping, dates and ordering match the question plan."}
    if exec_result is None:
        dims["execution"] = {"status": "not_run", "detail": "Query has not been executed."}
    else:
        dims["execution"] = {"status": "pass" if exec_result.get("success") else "fail",
                             "detail": f"{exec_result.get('row_count', 0)} row(s) returned." if exec_result.get("success") else str(exec_result.get("error", ""))[:200]}
    st = (verification or {}).get("status", "not_independently_verified")
    label = {"verified_match": "verified", "mismatch": "mismatch", "suspicious_empty": "suspicious",
             "empty_plausible": "unconfirmed", "not_independently_verified": "not_verified", "error": "fail"}.get(st, "not_verified")
    dims["result_correctness"] = {"status": label, "detail": "; ".join(c["detail"] for c in (verification or {}).get("checks", [])[:2])
                                  or "Result correctness was not independently checked; successful execution does not prove the answer is right."}
    return dims
