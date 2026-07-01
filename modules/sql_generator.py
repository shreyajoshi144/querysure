"""
SQL Generator
Converts a business question + retrieved semantic context into SQL using Groq.
"""

from __future__ import annotations

import re
from groq import Groq

try:
    from .retrieval_engine import retrieval_to_prompt_text
except ImportError:
    from retrieval_engine import retrieval_to_prompt_text

SYSTEM_PROMPT = (
    "You are a senior analytics engineer. Generate only valid SQLite SQL. "
    "Never invent tables or columns. Use retrieved context as the primary source. "
    "Follow metric definitions exactly. Return only the requested format."
)


def _extract_sql(text: str) -> str:
    if "```sql" in text:
        chunk = text.split("```sql", 1)[1]
        chunk = chunk.split("```", 1)[0]
        return chunk.strip()

    if "SQL:" in text:
        chunk = text.split("SQL:", 1)[1]

        if "EXPLANATION:" in chunk:
            chunk = chunk.split("EXPLANATION:", 1)[0]

        return chunk.strip()

    m = re.search(
        r"SELECT\s+.*",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    return m.group(0).strip() if m else ""


def _extract_section(text: str, start: str, end: str) -> str:
    if start not in text:
        return ""
    chunk = text.split(start, 1)[1]
    if end in chunk:
        chunk = chunk.split(end, 1)[0]
    return chunk.strip()


def _schema_subset_text(schema: dict, relevant_tables: list | None = None) -> str:
    tables = relevant_tables or list(schema.get("tables", {}).keys())
    lines = ["SCHEMA SUBSET:\n"]
    for table_name in tables:
        if table_name not in schema.get("tables", {}):
            continue
        info = schema["tables"][table_name]
        lines.append(f"Table: {table_name} ({info['row_count']} rows)")
        for col in info.get("columns", []):
            pk_tag = " [PK]" if col.get("pk") else ""
            nn_tag = " NOT NULL" if col.get("notnull") else ""
            lines.append(f" - {col['name']} ({col['type']}){pk_tag}{nn_tag}")
        if info.get("foreign_keys"):
            for fk in info["foreign_keys"]:
                lines.append(f" FK: {fk['from']} -> {fk['to_table']}.{fk['to_col']}")
        lines.append("")
    if schema.get("relationships"):
        lines.append("RELEVANT RELATIONSHIPS:")
        for r in schema["relationships"]:
            if r["from_table"] in tables or r["to_table"] in tables:
                lines.append(f" {r['from_table']}.{r['from_col']} -> {r['to_table']}.{r['to_col']}")
    return "\n".join(lines)


def _semantic_subset_text(semantic: dict | None, relevant_tables: list | None = None, retrieval: dict | None = None) -> str:
    if not semantic:
        return ""
    tables = relevant_tables or (retrieval.get("relevant_tables") if retrieval else None) or list(semantic.get("tables", {}).keys())
    lines = ["SEMANTIC SUBSET:\n"]
    for table_name in tables:
        if table_name not in semantic.get("tables", {}):
            continue
        info = semantic["tables"][table_name]
        lines.append(f"Table: {table_name} -> {info.get('business_name', table_name)}")
        lines.append(f" Type: {info.get('table_type', 'unknown')}")
        lines.append(f" Summary: {info.get('table_summary', '')}")
        lines.append(f" Metrics: {', '.join(info.get('available_metrics', [])) or 'general'}")
        for col in info.get("columns", []):
            lines.append(
                f" - {col['column']} | role={col.get('semantic_role', 'attribute')} | priority={col.get('business_priority', 1)} | meaning={col.get('meaning', '')}"
            )
        lines.append("")
    return "\n".join(lines)


def _metric_context_text(semantic: dict, retrieval: dict | None = None) -> str:
    lines = ["METRIC DEFINITIONS:\n"]
    keys = []
    if retrieval:
        keys.extend(retrieval.get("retrieved_metrics", []))
        for t in retrieval.get("top_tables", []):
            keys.extend(t.get("available_metrics", []))
    keys = [k for k in dict.fromkeys(keys) if k in semantic.get("metric_definitions", {})]
    keys = keys or list(semantic.get("metric_definitions", {}).keys())[:8]
    for k in keys:
        lines.append(f"- {k}: {semantic['metric_definitions'][k]}")
    return "\n".join(lines)


def _retrieval_confidence(retrieval: dict | None) -> float:
    if not retrieval:
        return 0.0
    table_scores = [float(t.get("score", 0)) for t in retrieval.get("top_tables", [])[:3]]
    if not table_scores:
        return 0.0
    return sum(table_scores) / len(table_scores)


def generate_sql(
    question: str,
    schema: dict,
    api_key: str,
    semantic: dict | None = None,
    retrieval: dict | None = None,
) -> dict:
    """Generate SQL from a business question using schema, semantic metadata, and retrieval context."""
    relevant_tables = retrieval.get("relevant_tables") if retrieval else None
    schema_text = _schema_subset_text(schema, relevant_tables=relevant_tables)
    semantic_text = _semantic_subset_text(semantic, relevant_tables=relevant_tables, retrieval=retrieval)
    retrieval_text = retrieval_to_prompt_text(retrieval) if retrieval else ""
    metric_text = _metric_context_text(semantic or {}, retrieval)
    retrieval_conf = _retrieval_confidence(retrieval)

    prompt = f"""SCHEMA SUBSET:
{schema_text}

SEMANTIC SUBSET:
{semantic_text}

{metric_text}

RETRIEVED CONTEXT:
{retrieval_text}

BUSINESS QUESTION:
{question}

RULES:
- Never use SELECT *.
- Always qualify columns with aliases.
- Always use explicit JOIN conditions.
- Prefer GROUP BY over subqueries when possible.
- Never invent tables or columns.
- If uncertain, use only retrieved tables.
- Use only SQLite syntax.
- If a metric definition is provided, follow it exactly.
- Use retrieved join hints when available.

Return in this exact format:

SQL:
```sql
[your SQL]
```

EXPLANATION:
[2-3 sentences]

CONFIDENCE: [High/Medium/Low]
COMPLEXITY: [Simple/Moderate/Complex]
"""

    try:
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=1500,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        text = response.choices[0].message.content or ""
        sql = _extract_sql(text)
        explanation = _extract_section(text, "EXPLANATION:", "CONFIDENCE:")

        confidence = "Medium"
        if "CONFIDENCE: High" in text or "CONFIDENCE:High" in text:
            confidence = "High"
        elif "CONFIDENCE: Low" in text or "CONFIDENCE:Low" in text:
            confidence = "Low"

        complexity = "Moderate"
        if "COMPLEXITY: Simple" in text or "COMPLEXITY:Simple" in text:
            complexity = "Simple"
        elif "COMPLEXITY: Complex" in text or "COMPLEXITY:Complex" in text:
            complexity = "Complex"

        if retrieval and retrieval_conf < 0.15:
            confidence = "Low"

        return {
            "success": True,
            "sql": sql,
            "explanation": explanation,
            "confidence": confidence,
            "complexity": complexity,
            "raw_response": text,
            "retrieval_confidence": retrieval_conf,
            "prompt_context": {
                "has_semantic": bool(semantic),
                "has_retrieval": bool(retrieval),
                "retrieved_tables": retrieval.get("relevant_tables", []) if retrieval else [],
                "schema_subset_tables": relevant_tables or list(schema.get("tables", {}).keys()),
            },
        }
    except Exception as e:
        return {
            "success": False,
            "sql": "",
            "explanation": "",
            "confidence": "Low",
            "complexity": "Unknown",
            "error": str(e),
            "retrieval_confidence": retrieval_conf,
        }