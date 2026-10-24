"""
Data Reliability Agent
Evaluates query outputs for completeness, freshness, integrity, drift, and uniqueness.
"""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import mean, median
from typing import Any, Dict, List

def _status_from_score(score: float, warn_threshold: int = 75, pass_threshold: int = 90) -> str:
    if score >= pass_threshold:
        return "PASS"
    if score >= warn_threshold:
        return "WARNING"
    return "FAIL"

def _level_from_score(score: float) -> str:
    if score >= 90:
        return "High"
    if score >= 75:
        return "Medium"
    return "Low"

def _safe_numeric_values(rows: List[dict], columns: List[str]) -> Dict[str, List[float]]:
    out = {c: [] for c in columns}
    for row in rows:
        for c in columns:
            v = row.get(c)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[c].append(float(v))
    return out

def _schema_timestamp_metadata(schema: dict) -> List[Dict[str, Any]]:
    cols = []
    for tname, tinfo in schema.get("tables", {}).items():
        for col in tinfo.get("columns", []):
            name = col["name"].lower()
            ctype = col["type"].upper()
            if any(k in name for k in ["date", "time", "created_at", "updated_at", "ship_date", "order_date"]) or ctype in {"DATE", "DATETIME", "TIMESTAMP"}:
                cols.append({"table": tname, "column": col["name"], "type": ctype})
    return cols

def _parse_dt(value: Any):
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    return None

def _freshness_score(schema: dict, query_result: dict) -> Dict[str, Any]:
    ts_meta = _schema_timestamp_metadata(schema)
    if not ts_meta or not query_result.get("rows"):
        return {"score": 100, "status": "PASS", "details": "No timestamp-based freshness check needed."}

    cols = query_result.get("columns", [])
    matching = [m for m in ts_meta if m["column"] in cols or any(k in m["column"].lower() for k in ["date", "time", "created_at", "updated_at", "ship_date", "order_date"])]
    if not matching:
        return {"score": 100, "status": "PASS", "details": "Not applicable: the result contains no date column (aggregated or non-temporal answer)."}

    latest_seen = None
    parsed = 0
    for row in query_result.get("rows", []):
        for m in matching:
            dt = _parse_dt(row.get(m["column"]))
            if dt is not None:
                parsed += 1
                latest_seen = dt if latest_seen is None else max(latest_seen, dt)

    if latest_seen is None:
        return {"score": 100, "status": "PASS", "details": "Not applicable: date-like values in the result are periods (e.g. year/month), not record timestamps."}

    # Uploaded CSV/Excel/SQLite files are point-in-time snapshots, often deliberately
    # historical. Penalising a 2-year-old but complete dataset as "stale" produced a
    # misleading warning on every historical question, so age is reported for
    # information only and does not reduce the score.
    now = datetime.now(timezone.utc)
    if latest_seen.tzinfo is None:
        latest_seen = latest_seen.replace(tzinfo=timezone.utc)
    age_days = max(0, (now - latest_seen.astimezone(timezone.utc)).days)
    return {"score": 100, "status": "PASS",
            "details": f"Informational: latest record in the result is dated {latest_seen.date().isoformat()} ({age_days} days ago); uploaded files are point-in-time snapshots."}

def _completeness_score(query_result: dict) -> Dict[str, Any]:
    profile = query_result.get("result_profile", {})
    row_count = profile.get("row_count", query_result.get("row_count", 0))
    columns = query_result.get("columns", [])
    if row_count == 0:
        if query_result.get("status") == "empty_result":
            return {"score": 60, "status": "WARNING", "details": "Empty result set returned by execution."}
        return {"score": 70, "status": "WARNING", "details": "Empty result set; may be valid but should be reviewed."}

    null_counts = profile.get("null_counts", {})
    null_total = sum(null_counts.get(c, 0) for c in columns)
    denom = max(1, row_count * max(1, len(columns)))
    null_rate = null_total / denom
    score = max(0, int(round(100 - null_rate * 100)))
    return {"score": score, "status": _status_from_score(score), "details": f"Null rate: {null_rate:.2%}."}


def _integrity_score(schema: dict, validation_result: dict, query_result: dict) -> Dict[str, Any]:
    issues = validation_result.get("issues", []) if validation_result else []
    joins_ok = validation_result.get("join_validation", {}).get("valid", True) if validation_result else True
    agg_ok = validation_result.get("aggregation_validation", {}).get("valid", True) if validation_result else True
    risk = validation_result.get("risk_score", 0) if validation_result else 0
    profile = query_result.get("result_profile", {})
    row_count = profile.get("row_count", query_result.get("row_count", 0))

    score = 100
    score -= min(risk, 40)
    score -= 10 if not joins_ok else 0
    score -= 10 if not agg_ok else 0
    score -= min(len([i for i in issues if i.get("type") in {"unknown_table", "unknown_column", "unknown_alias", "invalid_join_relationship"}]) * 8, 24)
    if row_count == 0:
        score -= 10
    score = max(0, score)
    return {"score": score, "status": _status_from_score(score), "details": f"Validation risk: {risk}."}


_TIME_NAME_HINTS = ("date", "month", "year", "week", "quarter", "day", "period", "time")


def _is_period_column(name: str) -> bool:
    low = str(name).lower()
    return any(h in low for h in _TIME_NAME_HINTS)


def _has_time_axis(rows: List[dict], cols: List[str]) -> bool:
    for c in cols:
        if _is_period_column(c):
            return True
        vals = [r.get(c) for r in rows[:20] if isinstance(r.get(c), str)]
        if vals and all(_parse_dt(v) is not None for v in vals):
            return True
    return False


def _drift_score(schema: dict, query_result: dict) -> Dict[str, Any]:
    rows = query_result.get("rows", [])
    cols = query_result.get("columns", [])
    if not rows:
        return {"score": 100, "status": "PASS", "details": "No data to compare for drift."}

    # Drift only means something across time. For ranked / grouped / categorical results the
    # mean-vs-median gap and spread are just the shape of the answer (a top-10 list is
    # SUPPOSED to be skewed), so the check is not applicable there.
    if not _has_time_axis(rows, cols):
        return {"score": 100, "status": "PASS", "details": "Not applicable: result is not a time series (ranked/grouped/categorical answer)."}
    numeric = {c: v for c, v in _safe_numeric_values(rows, cols).items() if v and not _is_period_column(c)}
    if not numeric:
        return {"score": 100, "status": "PASS", "details": "Not applicable: no measure columns to compare over time."}

    drifts = []
    for c, vals in numeric.items():
        if len(vals) >= 3:
            mu = mean(vals)
            md = median(vals)
            if mu != 0:
                drifts.append(min(100.0, abs(mu - md) / abs(mu) * 100))
            if len(set(vals)) >= 2:
                spread = (max(vals) - min(vals)) / (abs(mu) + 1e-9)
                drifts.append(min(100.0, spread * 25))
    if not drifts:
        return {"score": 85, "status": "WARNING", "details": "Insufficient numeric variability for drift assessment."}

    avg_drift = mean(drifts)
    variability_penalty = min(20, sum(1 for v in drifts if v > 25) * 4)
    score = max(0, 100 - int(avg_drift) - variability_penalty)
    status = _status_from_score(score)
    if score < 90 and status == "PASS":
        status = "WARNING"
    return {"score": score, "status": status, "details": "Heuristic numeric drift check."}


def _uniqueness_score(query_result: dict) -> Dict[str, Any]:
    profile = query_result.get("result_profile", {})
    row_count = profile.get("row_count", query_result.get("row_count", 0))
    distinct_counts = profile.get("distinct_counts", {}) or {}
    if row_count == 0 or not distinct_counts:
        return {"score": 85, "status": "WARNING", "details": "No uniqueness signal available."}

    # Only label (non-numeric) columns should be unique in a grouped/ranked result.
    # Measure columns legitimately repeat (many groups can share a count), and counting
    # them dragged every aggregate answer toward a spurious WARNING.
    numeric_cols = {c for c in query_result.get("columns", [])
                       if query_result.get("rows") and all(
                           isinstance(r.get(c), (int, float)) and not isinstance(r.get(c), bool) or r.get(c) is None
                           for r in query_result["rows"])}
    label_cols = {c: d for c, d in distinct_counts.items() if c not in numeric_cols and not _is_period_column(c)}
    if row_count == 1 or not label_cols:
        return {"score": 100, "status": "PASS", "details": "Not applicable: no label columns to check for duplicates."}
    ratios = []
    for col, distinct in label_cols.items():
        try:
            ratios.append(min(1.0, float(distinct) / max(1.0, float(row_count))))
        except Exception:
            continue
    if not ratios:
        return {"score": 85, "status": "WARNING", "details": "No usable uniqueness ratios."}

    avg_ratio = mean(ratios)
    score = int(round(avg_ratio * 100))
    status = _status_from_score(score)
    return {"score": score, "status": status, "details": f"Average distinct-to-row ratio: {avg_ratio:.2%}."}

def assess_reliability(schema: dict, validation_result: dict, query_result: dict) -> dict:
    """Assess data reliability from validation and query results."""
    completeness = _completeness_score(query_result)
    freshness = _freshness_score(schema, query_result)
    integrity = _integrity_score(schema, validation_result, query_result)
    drift = _drift_score(schema, query_result)
    uniqueness = _uniqueness_score(query_result)

    reliability_score = int(round(
        completeness["score"] * 0.22 +
        freshness["score"] * 0.22 +
        integrity["score"] * 0.30 +
        drift["score"] * 0.13 +
        uniqueness["score"] * 0.13
    ))

    # HONESTY: the score above measures process quality (nulls, validation, shape). It is NOT the probability that the
    # answer is right. Answer correctness is reported separately and a failed independent check caps the score.
    verification = (query_result or {}).get("verification") or None
    v_status = (verification or {}).get("status")
    capped_reason = None
    sem = (query_result or {}).get("semantic_validation") or {}
    if v_status in ("mismatch", "suspicious_empty") and reliability_score > 60:
        reliability_score, capped_reason = 60, f"capped at 60 because the independent result check reported '{v_status}'"
    elif sem.get("issues") and reliability_score > 74:
        reliability_score, capped_reason = 74, "capped at 74 because the SQL does not fully match the question (semantic check)"
    elif v_status not in ("verified_match", "empty_plausible") and reliability_score > 89:
        # "High"/"PASS" is only earned by an answer that was independently re-computed and agreed; a clean process alone is not enough.
        reliability_score, capped_reason = 89, "capped at 89 because the result was not independently verified"
    try:
        from .result_verifier import correctness_dimensions
        dimensions = correctness_dimensions(validation_result or {}, (query_result or {}).get("semantic_validation"),
                                            query_result, verification)
    except Exception:  # noqa: BLE001
        dimensions = None

    return {
        "reliability_score": reliability_score,
        "reliability_level": _level_from_score(reliability_score),
        "score_meaning": "Process-quality score (completeness, validation, result shape). It is not a measure of answer accuracy; see 'dimensions.result_correctness'.",
        "answer_verified": bool((verification or {}).get("verified")),
        "capped_reason": capped_reason,
        "dimensions": dimensions,
        "completeness": completeness,
        "freshness": freshness,
        "integrity": integrity,
        "drift": drift,
        "uniqueness": uniqueness,
        "status": _status_from_score(reliability_score),
        "summary": {
            "row_count": query_result.get("row_count", 0),
            "query_id": query_result.get("query_id"),
            "validation_score": validation_result.get("validation_score") if validation_result else None,
            "query_status": query_result.get("status"),
        },
    }