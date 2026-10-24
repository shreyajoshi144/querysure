"""
Benchmark integrity checks -- "is the yardstick itself sound?"

Run by `python -m evaluation validate-dataset` and by the test suite. For every
answerable case the gold SQL must:

  * pass QuerySure's own validator and execute (no truncation, non-empty
    unless the case allows it),
  * reference only real tables/columns,
  * touch exactly the tables the case declares as expected,
  * contain the expected columns and satisfy its declared characteristics,
  * for ranking ("ordered") cases, have an unambiguous order: the ordering
    column must have no ties within the rows that matter, otherwise two
    correct answers could legitimately differ and the case would be unfair.
"""
from __future__ import annotations

from typing import List, Sequence

import sqlglot

from .analysis import analyze_sql
from .models import BenchmarkCase, DatasetError
from .runner import EvalEnvironment, compute_gold


def _strip_limit(sql: str):
    tree = sqlglot.parse_one(sql, read="sqlite")
    limit = tree.args.get("limit")
    n = None
    if limit is not None:
        try:
            n = int(limit.expression.name)
        except (ValueError, AttributeError):
            n = None
        tree.set("limit", None)
    return tree.sql(dialect="sqlite"), n


def check_case(case: BenchmarkCase, env: EvalEnvironment) -> List[str]:
    if case.should_decline:
        return []
    from modules import execute_query

    problems: List[str] = []
    try:
        gold = compute_gold(case, env)
    except DatasetError as e:
        return [str(e)]
    if gold.truncated:
        problems.append(f"{case.id}: gold result hit the row cap ({env.max_rows}); comparison would be unreliable")

    a = analyze_sql(case.gold_sql or "", env.catalog)
    if not a.parse_ok:
        return problems + [f"{case.id}: gold SQL does not parse: {a.parse_error}"]
    if a.hallucinated_tables or a.hallucinated_columns:
        problems.append(f"{case.id}: gold SQL references non-existent identifiers: {a.hallucinated_identifiers}")
    if set(a.tables) != set(case.expected_tables):
        problems.append(f"{case.id}: expected_tables {sorted(case.expected_tables)} != tables in gold SQL {sorted(a.tables)}")
    missing_cols = set(case.expected_columns) - set(a.columns)
    if missing_cols:
        problems.append(f"{case.id}: expected_columns not referenced by gold SQL: {sorted(missing_cols)}")
    for key, want in case.expected_characteristics.items():
        if a.features.get(key) != want:
            problems.append(f"{case.id}: declared characteristic {key}={want} but gold SQL has {a.features.get(key)}")

    if case.ordered:
        if not a.features.get("order_by"):
            problems.append(f"{case.id}: ordered case but gold SQL has no ORDER BY")
        else:
            problems += _check_order_is_unambiguous(case, env, execute_query)
    return problems


def _check_order_is_unambiguous(case: BenchmarkCase, env: EvalEnvironment, execute_query) -> List[str]:
    unlimited_sql, n = _strip_limit(case.gold_sql or "")
    res = execute_query(unlimited_sql, env.db_path, max_rows=env.max_rows)
    if not res.get("success"):
        return [f"{case.id}: could not run gold SQL without LIMIT for the tie check: {res.get('error')}"]
    cols = {c.lower(): c for c in res["columns"]}
    key = (case.order_by_column or "").lower()
    if key not in cols:
        return [f"{case.id}: order_by_column '{case.order_by_column}' is not an output column {res['columns']}"]
    values = [row[cols[key]] for row in res["rows"]]
    window = values[: (n + 1) if n else len(values)]
    if len(set(window)) != len(window):
        return [f"{case.id}: ties in '{case.order_by_column}' within the ranked rows -- the expected order is ambiguous"]
    return []


def check_dataset(cases: Sequence[BenchmarkCase], env: EvalEnvironment) -> List[str]:
    problems: List[str] = []
    for c in cases:
        problems += check_case(c, env)
    return problems
