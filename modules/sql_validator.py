"""
SQL Validation Agent
Validates generated SQL for safety, schema consistency, and analytics correctness.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

DANGEROUS_SQL_PATTERNS = [
    r"\bDROP\b",
    r"\bDELETE\b",
    r"\bTRUNCATE\b",
    r"\bUPDATE\b",
    r"\bINSERT\b",
    r"\bALTER\b",
    r"\bREPLACE\b",
    r"\bATTACH\b",
    r"\bDETACH\b",
    r"\bVACUUM\b",
    r"\bPRAGMA\b",
]

AGG_FUNCS = {"sum", "avg", "count", "min", "max"}


def _parse_from_joins(sql: str) -> Tuple[Dict[str, str], List[Tuple[str, str, str, str]]]:
    alias_map: Dict[str, str] = {}
    joins: List[Tuple[str, str, str, str]] = []

    from_matches = list(re.finditer(r"\bfrom\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s+as)?(?:\s+([A-Za-z_][A-Za-z0-9_]*))?", sql, flags=re.IGNORECASE))
    for m in from_matches:
        table = m.group(1)
        alias = m.group(2) or table
        alias_map[alias] = table
        alias_map[table] = table

    for m in re.finditer(r"\bjoin\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s+as)?(?:\s+([A-Za-z_][A-Za-z0-9_]*))?\s+on\s+([^\n;]+?)(?=\bjoin\b|\bwhere\b|\bgroup\b|\border\b|\blimit\b|$)", sql, flags=re.IGNORECASE | re.DOTALL):
        table = m.group(1)
        alias = m.group(2) or table
        alias_map[alias] = table
        alias_map[table] = table
        on_clause = m.group(3)
        conds = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)", on_clause)
        for a1, c1, a2, c2 in conds:
            joins.append((a1, c1, a2, c2))
    return alias_map, joins


def _find_table_names(sql: str) -> List[str]:
    tables = []
    patterns = [r"\bfrom\s+([A-Za-z_][A-Za-z0-9_]*)", r"\bjoin\s+([A-Za-z_][A-Za-z0-9_]*)"]
    for pat in patterns:
        for m in re.finditer(pat, sql, flags=re.IGNORECASE):
            tables.append(m.group(1))
    return list(dict.fromkeys(tables))


def _has_select_star(sql: str) -> bool:
    return bool(re.search(r"\bselect\s+\*\b", sql, flags=re.IGNORECASE))


def _split_select_clause(sql: str) -> List[str]:
    m = re.search(r"\bselect\s+(.*?)\s+\bfrom\b", sql, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return []
    clause = m.group(1)
    parts = []
    depth = 0
    current = []
    for ch in clause:
        if ch == "," and depth == 0:
            item = "".join(current).strip()
            if item:
                parts.append(item)
            current = []
        else:
            if ch == "(":
                depth += 1
            elif ch == ")" and depth > 0:
                depth -= 1
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _is_aggregated(expr: str) -> bool:
    return bool(re.search(r"\b(sum|avg|count|min|max)\s*\(", expr, flags=re.IGNORECASE))


def _group_by_columns(sql: str) -> List[str]:
    m = re.search(r"\bgroup\s+by\s+(.*?)(?:\border\b|\bhaving\b|\blimit\b|$)", sql, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return []
    return [c.strip().split()[0] for c in m.group(1).split(",") if c.strip()]


def _find_column_refs(sql: str) -> List[Tuple[str, str]]:
    return re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b", sql)


def _schema_column_lookup(schema: dict) -> Dict[str, set]:
    lookup = {}
    for tname, tinfo in schema.get("tables", {}).items():
        lookup[tname] = {c["name"] for c in tinfo.get("columns", [])}
    return lookup


def _relationship_pairs(schema: dict) -> set:
    pairs = set()
    for r in schema.get("relationships", []):
        pairs.add((r["from_table"], r["from_col"], r["to_table"], r["to_col"]))
        pairs.add((r["to_table"], r["to_col"], r["from_table"], r["from_col"]))
    return pairs


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


def validate_sql(sql: str, schema: dict, semantic: dict | None = None) -> dict:
    """Validate SQL against schema and analytics guardrails."""
    issues: List[Dict[str, Any]] = []
    warnings: List[str] = []
    normalized = sql.strip()

    if not normalized:
        return {
            "is_valid": False,
            "validation_score": 0,
            "risk_score": 100,
            "hallucination_score": 1.0,
            "issues": [{"type": "empty_sql", "message": "SQL is empty."}],
            "warnings": [],
            "validated_sql": "",
            "join_validation": {"valid": False},
            "aggregation_validation": {"valid": False},
            "fanout_risk": "High",
        }

    for pat in DANGEROUS_SQL_PATTERNS:
        if re.search(pat, normalized, flags=re.IGNORECASE):
            issues.append({"type": "dangerous_sql", "message": f"Dangerous SQL keyword detected: {pat}"})

    if _has_select_star(normalized):
        issues.append({"type": "select_star", "message": "SELECT * is not allowed."})

    alias_map, join_edges = _parse_from_joins(normalized)
    table_names = _find_table_names(normalized)
    available_tables = set(schema.get("tables", {}).keys())
    for t in table_names:
        if t not in available_tables:
            issues.append({"type": "unknown_table", "message": f"Table not found in schema: {t}"})

    column_lookup = _schema_column_lookup(schema)
    refs = _find_column_refs(normalized)
    for alias_or_table, col in refs:
        resolved_table = alias_map.get(alias_or_table)
        if not resolved_table:
            issues.append({"type": "unknown_alias", "message": f"Unknown table alias: {alias_or_table}"})
            continue
        if col not in column_lookup.get(resolved_table, set()):
            issues.append({"type": "unknown_column", "message": f"Column not found on table {resolved_table}: {col}"})

    relationship_pairs = _relationship_pairs(schema)
    invalid_relationships = []
    for a1, c1, a2, c2 in join_edges:
        t1 = alias_map.get(a1)
        t2 = alias_map.get(a2)
        if not t1 or not t2:
            continue
        ok = (t1, c1, t2, c2) in relationship_pairs or (t2, c2, t1, c1) in relationship_pairs
        if not ok:
            invalid_relationships.append(f"{t1}.{c1} -> {t2}.{c2}")
            issues.append({"type": "invalid_join_relationship", "message": f"Join relationship not found in schema: {t1}.{c1} = {t2}.{c2}"})

    select_items = _split_select_clause(normalized)
    group_by_cols = set(_group_by_columns(normalized))
    aggregation_present = any(_is_aggregated(item) for item in select_items)
    non_agg_exprs = [item for item in select_items if not _is_aggregated(item)]
    agg_valid = True
    if aggregation_present and non_agg_exprs:
        for expr in non_agg_exprs:
            expr_col = expr.split()[0]
            if expr_col not in group_by_cols and expr_col != "*":
                agg_valid = False
                warnings.append(f"Non-aggregated select item may require GROUP BY: {expr}")
    if aggregation_present and not group_by_cols and non_agg_exprs:
        agg_valid = False

    fanout_risk = "Low"
    if len(join_edges) >= 2:
        many_sides = 0
        for a1, c1, a2, c2 in join_edges:
            t1 = alias_map.get(a1)
            t2 = alias_map.get(a2)
            if not t1 or not t2:
                continue
            t1_pk = any(col.get("pk") and col["name"] == c1 for col in schema.get("tables", {}).get(t1, {}).get("columns", []))
            t2_pk = any(col.get("pk") and col["name"] == c2 for col in schema.get("tables", {}).get(t2, {}).get("columns", []))
            if not t1_pk and not t2_pk:
                many_sides += 1
        if many_sides:
            fanout_risk = "High" if many_sides > 1 else "Medium"
            warnings.append("Potential fan-out risk due to joins on non-primary key fields.")

    metric_warnings = _metric_validation(normalized, semantic)
    warnings.extend(metric_warnings)

    risk_score = 0
    risk_score += 60 if any(i["type"] == "dangerous_sql" for i in issues) else 0
    risk_score += 25 if any(i["type"] in {"unknown_table", "unknown_column", "unknown_alias"} for i in issues) else 0
    risk_score += 20 if any(i["type"] == "invalid_join_relationship" for i in issues) else 0
    risk_score += 15 if not agg_valid else 0
    risk_score += 10 if any(i["type"] == "select_star" for i in issues) else 0
    risk_score += 5 if warnings else 0
    risk_score = min(100, risk_score)

    hallucination_hits = sum(1 for i in issues if i["type"] in {"unknown_table", "unknown_column", "unknown_alias", "invalid_join_relationship"})
    hallucination_score = min(1.0, hallucination_hits / max(1, len(table_names) + len(refs) + len(join_edges)))
    validation_score = max(0, 100 - risk_score)
    is_valid = not any(i["type"] in {"dangerous_sql", "unknown_table", "unknown_column", "unknown_alias", "select_star", "invalid_join_relationship"} for i in issues)

    if semantic and semantic.get("metric_definitions"):
        metric_names = list(semantic["metric_definitions"].keys())
        if any(m in normalized.lower() for m in metric_names):
            warnings.append("Query references known business metrics.")

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