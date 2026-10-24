"""
Business Insight Agent
Turns reliable query results into KPI-aware business insights and anomaly narratives.
"""

from __future__ import annotations

from statistics import mean, median, pstdev
from typing import Any, Dict, List, Tuple

def _numeric_columns(rows: List[dict], columns: List[str]) -> List[str]:
    cols = []
    for c in columns:
        vals = [r.get(c) for r in rows if isinstance(r.get(c), (int, float)) and not isinstance(r.get(c), bool)]
        if len(vals) >= 2:
            cols.append(c)
    return cols


def _dimension_columns(rows: List[dict], columns: List[str]) -> List[str]:
    dims = []
    for c in columns:
        vals = [r.get(c) for r in rows if r.get(c) is not None]
        if vals and not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            dims.append(c)
    return dims


def _score_dimension(name: str) -> int:
    n = name.lower()
    score = 0
    for token, weight in [
        ("category", 6), ("segment", 6), ("region", 6), ("product", 6), ("customer", 6),
        ("store", 5), ("channel", 5), ("department", 5), ("team", 4), ("market", 4),
        ("country", 4), ("state", 4), ("city", 3), ("brand", 3)
    ]:
        if token in n:
            score += weight
    if any(k in n for k in ["date", "time", "created", "updated", "month", "year", "day"]):
        score -= 4
    return score


def _best_dimension(dims: List[str]) -> str | None:
    if not dims:
        return None
    ranked = sorted(dims, key=lambda d: (_score_dimension(d), -len(d), d.lower()), reverse=True)
    return ranked[0]


def _kpi_columns(query_result: dict, semantic: dict | None = None) -> List[str]:
    cols = query_result.get("columns", [])
    metric_names = set()
    if semantic:
        metric_names.update(semantic.get("metric_definitions", {}).keys())
        for t in semantic.get("tables", {}).values():
            metric_names.update(t.get("available_metrics", []))
    selected = [c for c in cols if c in metric_names or any(k in c.lower() for k in ["revenue", "sales", "profit", "margin", "count", "amount", "value", "quantity", "volume", "orders", "transactions", "cost"]) ]
    return list(dict.fromkeys(selected))


def _limited(sql: str | None) -> bool:
    """True when the outermost query has a LIMIT/OFFSET, so the returned rows are only part of the groups."""
    if not sql:
        return False
    try:
        import sqlglot
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:  # noqa: BLE001 - unparseable: assume partial, so no share is claimed from the rows
        return True
    return bool(tree.args.get("limit") or tree.args.get("offset"))


def _full_totals(sql: str, db_path: str | None, metrics: List[str]) -> Dict[str, float]:
    """Totals of ``metrics`` over every row the query returns WITHOUT its LIMIT/OFFSET (same filters and grouping),
    i.e. the denominator of a share of total. Empty when it cannot be computed; then no share is reported."""
    if not (sql and db_path and metrics):
        return {}
    try:
        import sqlglot
        from .query_result_engine import execute_query
        inner = sqlglot.parse_one(sql, read="sqlite")
        for k in ("limit", "offset", "order"):
            inner.set(k, None)
        cols = ", ".join('SUM("{}")'.format(m.replace('"', '""')) for m in metrics)
        res = execute_query(f"SELECT {cols} FROM ({inner.sql(dialect='sqlite')}) AS full_result", db_path)
        if not res.get("success") or not res.get("rows"):
            return {}
        row = res["rows"][0]
        vals = [row.get(c) for c in res.get("columns", [])]
        return {m: float(v) for m, v in zip(metrics, vals) if isinstance(v, (int, float)) and not isinstance(v, bool)}
    except Exception:  # noqa: BLE001 - a missing denominator only removes the share finding
        return {}


def _top_contributor_analysis(rows: List[dict], dim_col: str, metric_col: str, top_n: int = 3,
                              total: float | None = None) -> Dict[str, Any]:
    """``total`` is the full-result denominator when the rows are a LIMITed subset; otherwise the rows' own sum."""
    agg = {}
    for r in rows:
        d = r.get(dim_col)
        m = r.get(metric_col)
        if d is None or not isinstance(m, (int, float)) or isinstance(m, bool):
            continue
        agg[d] = agg.get(d, 0.0) + float(m)
    if not agg:
        return {"has_data": False}
    total = sum(agg.values()) if total is None else total
    ranked = sorted(agg.items(), key=lambda x: x[1], reverse=True)
    top = []
    for k, v in ranked[:top_n]:
        share = (v / total * 100) if total else 0
        top.append({"dimension": k, "value": v, "share_pct": round(share, 2)})
    concentration = sum(x["share_pct"] for x in top[:2]) if top else 0
    return {
        "has_data": True,
        "dimension": dim_col,
        "metric": metric_col,
        "total": total,
        "top_contributors": top,
        "concentration_pct": round(concentration, 2),
    }


def _trend_label(values: List[float]) -> Tuple[str, float]:
    if len(values) < 2:
        return "insufficient data", 0.0
    start, end = values[0], values[-1]
    if start == 0:
        return ("stable" if end == 0 else "changed", 0.0)
    pct = ((end - start) / abs(start)) * 100
    if abs(pct) < 5:
        return "stable", pct
    return ("upward" if pct > 0 else "downward"), pct


_NON_ADDITIVE_HINTS = ("pct", "percent", "rate", "ratio", "avg", "average", "mean", "margin", "growth",
                       "price", "score", "rating", "median", "share", "discount")
_TIME_HINTS = ("date", "month", "year", "week", "quarter", "day", "period", "time")


def _is_additive_metric(name: str) -> bool:
    """A 'share of total' is only meaningful for additive measures (sums/counts)."""
    low = str(name).lower()
    return not any(h in low for h in _NON_ADDITIVE_HINTS)


def _has_time_axis(rows: List[dict], columns: List[str]) -> bool:
    return any(any(h in str(c).lower() for h in _TIME_HINTS) for c in columns)


def _outlier_detection(rows: List[dict], metric_col: str) -> List[Dict[str, Any]]:
    vals = [float(r[metric_col]) for r in rows if isinstance(r.get(metric_col), (int, float)) and not isinstance(r.get(metric_col), bool)]
    if len(vals) < 4:
        return []
    med = median(vals)
    q1 = median(sorted(vals)[: len(vals)//2])
    q3 = median(sorted(vals)[(len(vals)+1)//2 :])
    iqr = max(0.0, q3 - q1)
    lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    outliers = []
    for idx, v in enumerate(vals):
        if v < lower or v > upper:
            z = (v - mean(vals)) / (pstdev(vals) + 1e-9)
            outliers.append({"index": idx, "value": v, "z_score": round(z, 2)})
    return outliers


def _fmt_num(v) -> str:
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int) or (isinstance(v, float) and v.is_integer() and abs(v) < 1e15):
        return f"{int(v):,}"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def _headline(rows: List[dict], columns: List[str], best_dim: str | None, metric_cols: List[str],
              totals: Dict[str, float] | None = None) -> str:
    """One factual sentence stating what the result says. Describes the returned rows only; it never claims the
    rows answer the question correctly (that is the verifier's job)."""
    if not rows:
        return "No rows were returned."
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    if len(rows) == 1:
        parts = [f"{c}: {_fmt_num(rows[0].get(c))}" for c in columns[:4] if rows[0].get(c) is not None]
        return ("Result: " + "; ".join(parts) + ".") if parts else "One row was returned."
    metric = next((m for m in metric_cols if m in columns), None)
    if best_dim and metric:
        vals = [r for r in rows if num(r.get(metric)) and r.get(best_dim) is not None]
        if len(vals) >= 2:
            top = max(vals, key=lambda r: r[metric])
            low = min(vals, key=lambda r: r[metric])
            total = sum(r[metric] for r in vals) if totals is None else totals.get(metric, 0)
            share = f" ({top[metric] / total * 100:.1f}% of the total)" if total > 0 and all(r[metric] >= 0 for r in vals) else ""
            return (f"{top[best_dim]} has the highest {metric} at {_fmt_num(top[metric])}{share}; "
                    f"{low[best_dim]} has the lowest at {_fmt_num(low[metric])} ({len(vals)} {best_dim} values compared).")
    return f"{len(rows)} rows returned."


def generate_insights(schema: dict, query_result: dict, reliability_result: dict | None = None, semantic: dict | None = None,
                      db_path: str | None = None) -> dict:
    """Generate KPI-aware business insights from query results. Shares of a total use the whole result: when the SQL
    has a LIMIT the denominator is recomputed without it (needs ``db_path``), and no share is stated if it cannot be."""
    rows = query_result.get("rows", [])
    columns = query_result.get("columns", [])
    if not rows:
        return {
            "summary": "No result rows were returned.",
            "key_findings": ["Query returned no rows."],
            "anomalies": [],
            "recommendations": ["Validate filters or source data for the queried period."],
            "insight_confidence": "Low",
            "executive_note": "Result set was empty.",
            "metadata": {"row_count": 0, "column_count": len(columns), "query_id": query_result.get("query_id")},
        }

    row_count = len(rows)
    kpis = _kpi_columns(query_result, semantic)
    dims = _dimension_columns(rows, columns)
    # a time column is an axis, not a category to apportion a 'share of total' across
    best_dim = _best_dimension([d for d in dims if not any(h in str(d).lower() for h in _TIME_HINTS)])
    num_cols = _numeric_columns(rows, columns)

    key_findings = []
    anomalies = []
    recommendations = []

    if reliability_result:
        score = reliability_result.get("reliability_score", 0)
        level = reliability_result.get("reliability_level", "Low")
        key_findings.append(f"Reliability assessment is {level} at {score}/100.")
        if score < 75:
            anomalies.append("Reliability score is below the preferred threshold for executive reporting.")
            recommendations.append("Review query logic and source data quality before sharing.")

    if query_result.get("status") == "empty_result":
        anomalies.append("Execution returned an empty result set.")
        recommendations.append("Check filters, date ranges, and join conditions.")

    if row_count < 10:
        key_findings.append(f"The result set is small, with only {row_count} rows.")
    else:
        key_findings.append(f"The result set contains {row_count} rows, which is sufficient for summary analysis.")

    profile = query_result.get("result_profile", {})
    null_counts = profile.get("null_counts", {}) or {}
    if any(v > 0 for v in null_counts.values()):
        total_nulls = sum(null_counts.values())
        key_findings.append(f"The output contains {total_nulls} null values across the returned columns.")
        if total_nulls > row_count:
            anomalies.append("Null density is relatively high compared with row count.")
            recommendations.append("Inspect missing-value handling for the affected fields.")

    time_axis = _has_time_axis(rows, columns)
    # None: the rows are the whole result, so their own sum is the total. A dict: totals recomputed without the LIMIT.
    totals = _full_totals(query_result.get("sql"), db_path, kpis[:2]) if _limited(query_result.get("sql")) else None
    for metric_col in kpis[:2]:
        if best_dim and not _is_additive_metric(metric_col):
            outliers = _outlier_detection(rows, metric_col)
            if outliers:
                anomalies.append(f"{metric_col} contains {len(outliers)} outlier row(s).")
        elif best_dim:
            if totals is not None and metric_col not in totals:
                contrib = {"has_data": False}      # partial rows and no full total: a share would be wrong
            else:
                contrib = _top_contributor_analysis(rows, best_dim, metric_col, top_n=3,
                                                    total=None if totals is None else totals[metric_col])
            if contrib.get("has_data"):
                top = contrib["top_contributors"]
                if top:
                    top1 = top[0]
                    key_findings.append(
                        f"{top1['dimension']} contributes {top1['share_pct']:.2f}% of total {metric_col}."
                    )
                    if len(top) >= 2 and contrib["concentration_pct"] >= 70:
                        key_findings.append(
                            f"{metric_col.capitalize()} is highly concentrated across {best_dim}; the top two contributors account for {contrib['concentration_pct']:.2f}% of the total."
                        )
                        recommendations.append(f"Investigate dependence on top {best_dim} contributors for {metric_col} performance.")
                    if top1["share_pct"] >= 50:
                        recommendations.append(f"Review why {top1['dimension']} dominates {metric_col}.")
                outliers = _outlier_detection(rows, metric_col)
                if outliers:
                    anomalies.append(f"{metric_col} contains {len(outliers)} outlier row(s).")
                    recommendations.append(f"Investigate outlier values in {metric_col} for potential data or business exceptions.")
        elif time_axis:
            vals = [float(r[metric_col]) for r in rows if isinstance(r.get(metric_col), (int, float)) and not isinstance(r.get(metric_col), bool)]
            if len(vals) >= 2:
                trend, pct = _trend_label(vals)
                if trend == "upward":
                    key_findings.append(f"{metric_col.capitalize()} increased across the result set by {abs(pct):.2f}% from first to last row.")
                    recommendations.append(f"Validate whether the growth in {metric_col} is consistent across segments.")
                elif trend == "downward":
                    key_findings.append(f"{metric_col.capitalize()} declined across the result set by {abs(pct):.2f}% from first to last row.")
                    recommendations.append(f"Investigate the drivers behind the decline in {metric_col}.")

    if not kpis and num_cols and time_axis:
        metric_col = num_cols[0]
        vals = [float(r[metric_col]) for r in rows if isinstance(r.get(metric_col), (int, float)) and not isinstance(r.get(metric_col), bool)]
        if len(vals) >= 2:
            trend, pct = _trend_label(vals)
            if trend == "upward":
                key_findings.append(f"{metric_col.capitalize()} increased across the result set by {abs(pct):.2f}% from first to last row.")
            elif trend == "downward":
                key_findings.append(f"{metric_col.capitalize()} declined across the result set by {abs(pct):.2f}% from first to last row.")

    if profile.get("distinct_counts"):
        for c, dcount in list(profile["distinct_counts"].items())[:3]:
            numeric_like = c in num_cols or any(h in str(c).lower() for h in _TIME_HINTS)
            if row_count >= 20 and not numeric_like and dcount <= max(1, int(row_count * 0.1)):
                anomalies.append(f"Column '{c}' has low distinctness relative to row count.")
                recommendations.append(f"Check whether '{c}' is being over-aggregated or affected by join duplication.")

    if not recommendations:
        recommendations.append("Continue monitoring the reported metrics for trend stability.")

    summary_bits = key_findings[:2]
    if reliability_result:
        summary_bits.append(f"Reliability: {reliability_result.get('reliability_level', 'Low')}.")
    summary = " ".join(summary_bits)

    confidence_score = reliability_result.get("reliability_score", 0) if reliability_result else 50
    insight_confidence = "High" if confidence_score >= 90 else "Medium" if confidence_score >= 75 else "Low"

    headline = _headline(rows, columns, best_dim, kpis + [c for c in num_cols if c not in kpis], totals)
    executive_note = headline
    if insight_confidence == "Low":
        executive_note += " Review before using this in a decision."
    if anomalies:
        executive_note += f" Key exception: {anomalies[0]}"

    return {
        "summary": summary,
        "key_findings": key_findings,
        "anomalies": anomalies,
        "recommendations": recommendations[:5],
        "insight_confidence": insight_confidence,
        "executive_note": executive_note,
        "headline": headline,
        "metadata": {
            "row_count": row_count,
            "column_count": len(columns),
            "query_id": query_result.get("query_id"),
            "kpis_detected": kpis,
        },
    }