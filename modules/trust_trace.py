"""Sanitized, presentation-ready answer provenance assembled from persisted session state."""

from __future__ import annotations

from typing import Any


def build_trust_trace(row: Any) -> dict:
    """Build a compact provenance trace without recomputing pipeline stages.

    Only values already persisted on PipelineSession are exposed. Secrets,
    prompts, provider credentials, and raw internal retrieval vectors are not
    included.
    """
    trace: dict[str, Any] = {"available": True, "session_id": getattr(row, "id", None)}
    schema = getattr(row, "schema", None) or {}
    if schema.get("dataset_id"):
        trace["dataset"] = {
            "dataset_id": schema.get("dataset_id"),
            "table_count": schema.get("table_count"),
            "total_rows": schema.get("total_rows"),
        }

    if row.question:
        trace["question"] = row.question

    retrieval = row.retrieval or {}
    semantic = row.semantic or {}
    validation = row.validation or {}
    query_result = row.query_result or {}
    reliability = row.reliability or {}
    insights = row.insights or {}

    if retrieval:
        tables = []
        for item in retrieval.get("top_tables", []) or []:
            if not isinstance(item, dict) or not item.get("table"):
                continue
            tables.append({
                "table": item.get("table"),
                "business_name": item.get("business_name"),
                "table_type": item.get("table_type"),
                "score": item.get("score"),
            })
        columns = []
        for item in retrieval.get("top_columns", []) or []:
            if not isinstance(item, dict) or not item.get("table") or not item.get("column"):
                continue
            columns.append({
                "table": item.get("table"),
                "column": item.get("column"),
                "role": item.get("role"),
                "score": item.get("score"),
            })
        if tables or columns or retrieval.get("join_hints"):
            trace["grounding"] = {
                "tables": tables,
                "columns": columns,
                "join_hints": [str(x) for x in (retrieval.get("join_hints") or [])[:8]],
            }

    # Show semantic definitions only for the tables/columns actually surfaced
    # by retrieval. This keeps the trace useful without dumping the full KB.
    selected_tables = {x.get("table") for x in trace.get("grounding", {}).get("tables", [])}
    selected_columns = {
        (x.get("table"), x.get("column"))
        for x in trace.get("grounding", {}).get("columns", [])
    }
    if semantic and selected_tables:
        definitions = []
        for table_name in selected_tables:
            info = (semantic.get("tables", {}) or {}).get(table_name)
            if not info:
                continue
            cols = []
            for col in info.get("columns", []) or []:
                key = (table_name, col.get("column"))
                if key in selected_columns:
                    cols.append({
                        "column": col.get("column"),
                        "meaning": col.get("meaning"),
                        "role": col.get("semantic_role"),
                        "domain": col.get("domain"),
                    })
            definitions.append({
                "table": table_name,
                "business_name": info.get("business_name"),
                "summary": info.get("table_summary"),
                "columns": cols,
            })
        if definitions:
            trace["semantic_context"] = definitions

    if row.sql:
        trace["sql"] = row.sql

    if validation:
        trace["validation"] = {
            "is_valid": validation.get("is_valid"),
            "validation_score": validation.get("validation_score"),
            "risk_score": validation.get("risk_score"),
            "fanout_risk": validation.get("fanout_risk"),
            "join_valid": (validation.get("join_validation") or {}).get("valid"),
            "aggregation_valid": (validation.get("aggregation_validation") or {}).get("valid"),
            "warning_count": len(validation.get("warnings") or []),
            "issue_count": len(validation.get("issues") or []),
        }

    if query_result:
        metadata = query_result.get("execution_metadata") or {}
        trace["execution"] = {
            "status": query_result.get("status"),
            "success": query_result.get("success"),
            "execution_time_ms": metadata.get("execution_time_ms"),
            "rows_returned": query_result.get("row_count", len(query_result.get("rows") or [])),
        }

    if reliability:
        signals = {}
        for key in ("reliability_score", "reliability_level", "status"):
            if key in reliability:
                signals[key] = reliability[key]
        for key in ("completeness", "freshness", "integrity", "drift", "uniqueness"):
            value = reliability.get(key)
            if value is not None:
                signals[key] = value
        if signals:
            trace["reliability"] = signals

    if insights:
        answer = insights.get("executive_note") or insights.get("summary")
        if answer:
            trace["answer"] = answer

    return trace
