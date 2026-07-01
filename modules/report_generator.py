"""
Analytics Report Generator
Combines question, SQL, validation, results, reliability, and insights into JSON, HTML, PDF, and CSV artifacts.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def _safe_slug(text: str) -> str:
    s = "".join(ch.lower() if ch.isalnum() else "_" for ch in text).strip("_")
    while "__" in s:
        s = s.replace("__", "_")
    return s[:80] or "report"


def _ensure_output_dir(output_dir: str = "output") -> Path:
    p = Path(output_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _json_default(obj):
    if isinstance(obj, (datetime,)):
        return obj.isoformat()
    return str(obj)


def _build_report_payload(
    question: str,
    sql_result: dict,
    validation_result: dict,
    query_result: dict,
    reliability_result: dict,
    insight_result: dict,
    semantic: dict | None = None,
) -> dict:
    return {
        "question": question,
        "generated_sql": sql_result.get("sql", ""),
        "sql_explanation": sql_result.get("explanation", ""),
        "sql_confidence": sql_result.get("confidence", "Unknown"),
        "sql_complexity": sql_result.get("complexity", "Unknown"),
        "retrieval_confidence": sql_result.get("retrieval_confidence"),
        "validation": validation_result,
        "query": {
            "status": query_result.get("status"),
            "success": query_result.get("success"),
            "query_id": query_result.get("query_id"),
            "row_count": query_result.get("row_count", 0),
            "columns": query_result.get("columns", []),
            "execution_metadata": query_result.get("execution_metadata", {}),
            "result_profile": query_result.get("result_profile", {}),
        },
        "reliability": reliability_result,
        "insights": insight_result,
        "semantic_context": semantic or {},
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }


def _render_html_report(payload: dict) -> str:
    validation = payload.get("validation", {})
    reliability = payload.get("reliability", {})
    insights = payload.get("insights", {})
    query = payload.get("query", {})
    rows = query.get("row_count", 0)
    cols = len(query.get("columns", []))
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>AI-RAD Analytics Report</title>
<style>
body {{ font-family: Arial, sans-serif; color: #1f2937; margin: 32px; line-height: 1.5; }}
.card {{ border: 1px solid #e5e7eb; border-radius: 12px; padding: 16px 18px; margin-bottom: 16px; }}
h1,h2,h3 {{ margin: 0 0 12px 0; }}
.meta {{ color: #6b7280; font-size: 12px; }}
.badge {{ display: inline-block; padding: 4px 10px; border-radius: 999px; background: #eef2ff; margin-right: 6px; font-size: 12px; }}
pre {{ white-space: pre-wrap; word-wrap: break-word; background: #f9fafb; padding: 12px; border-radius: 8px; border: 1px solid #e5e7eb; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #e5e7eb; padding: 8px; text-align: left; vertical-align: top; }}
th {{ background: #f3f4f6; }}
.small {{ font-size: 13px; }}
</style>
</head>
<body>
<h1>AI-RAD Analytics Report</h1>
<p class="meta">Generated at {payload.get('generated_at')}</p>

<div class="card">
<h2>Question</h2>
<p>{payload.get('question', '')}</p>
</div>

<div class="card">
<h2>SQL</h2>
<p><span class="badge">Confidence: {payload.get('sql_confidence')}</span><span class="badge">Complexity: {payload.get('sql_complexity')}</span></p>
<pre>{payload.get('generated_sql', '')}</pre>
</div>

<div class="card">
<h2>Validation</h2>
<p class="small">Valid: {validation.get('is_valid')} | Validation Score: {validation.get('validation_score')} | Risk Score: {validation.get('risk_score')}</p>
<p class="small">Join Valid: {validation.get('join_validation', {}).get('valid')} | Aggregation Valid: {validation.get('aggregation_validation', {}).get('valid')} | Fan-out Risk: {validation.get('fanout_risk')}</p>
<pre>{json.dumps(validation, indent=2, default=_json_default)}</pre>
</div>

<div class="card">
<h2>Query Results</h2>
<p class="small">Status: {query.get('status')} | Rows: {rows} | Columns: {cols} | Query ID: {query.get('query_id')}</p>
<p class="small">Execution Time: {query.get('execution_metadata', {}).get('execution_time_ms')} ms | Truncated: {query.get('execution_metadata', {}).get('truncated')}</p>
<pre>{json.dumps(query.get('result_profile', {}), indent=2, default=_json_default)}</pre>
</div>

<div class="card">
<h2>Reliability</h2>
<p class="small">Score: {reliability.get('reliability_score')} | Level: {reliability.get('reliability_level')} | Status: {reliability.get('status')}</p>
<pre>{json.dumps(reliability, indent=2, default=_json_default)}</pre>
</div>

<div class="card">
<h2>Insights</h2>
<p><strong>Summary:</strong> {insights.get('summary', '')}</p>
<p><strong>Insight Confidence:</strong> {insights.get('insight_confidence', 'Unknown')}</p>
<p><strong>Executive Note:</strong> {insights.get('executive_note', '')}</p>
<h3>Key Findings</h3>
<ul>
{''.join(f'<li>{x}</li>' for x in insights.get('key_findings', []))}
</ul>
<h3>Anomalies</h3>
<ul>
{''.join(f'<li>{x}</li>' for x in insights.get('anomalies', []))}
</ul>
<h3>Recommendations</h3>
<ul>
{''.join(f'<li>{x}</li>' for x in insights.get('recommendations', []))}
</ul>
</div>
</body>
</html>"""
    return html


def _write_csv_files(output_dir: Path, payload: dict, query_result: dict) -> Dict[str, str]:
    paths = {}
    query_id = query_result.get("query_id") or _safe_slug(payload.get("question", "report"))
    rows = query_result.get("rows", []) or []
    columns = query_result.get("columns", []) or []

    rows_path = output_dir / f"{query_id}_rows.csv"
    with rows_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c) for c in columns})
    paths["rows_csv"] = str(rows_path)

    summary_path = output_dir / f"{query_id}_summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "value"])
        writer.writerow(["question", payload.get("question", "")])
        writer.writerow(["query_id", query_id])
        writer.writerow(["row_count", query_result.get("row_count", 0)])
        writer.writerow(["validation_score", payload.get("validation", {}).get("validation_score")])
        writer.writerow(["risk_score", payload.get("validation", {}).get("risk_score")])
        writer.writerow(["reliability_score", payload.get("reliability", {}).get("reliability_score")])
        writer.writerow(["reliability_level", payload.get("reliability", {}).get("reliability_level")])
        writer.writerow(["insight_confidence", payload.get("insights", {}).get("insight_confidence")])
    paths["summary_csv"] = str(summary_path)
    return paths


def _write_pdf_if_possible(output_dir: Path, html: str, query_id: str) -> Optional[str]:
    pdf_path = output_dir / f"{query_id}_report.pdf"
    try:
        from weasyprint import HTML
        HTML(string=html).write_pdf(str(pdf_path))
        return str(pdf_path)
    except Exception:
        try:
            from reportlab.lib.pagesizes import letter
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
            from reportlab.lib.styles import getSampleStyleSheet
            doc = SimpleDocTemplate(str(pdf_path), pagesize=letter)
            styles = getSampleStyleSheet()
            story = []
            for line in html.splitlines()[:400]:
                clean = line.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
                if clean.strip():
                    story.append(Paragraph(clean[:250], styles["BodyText"]))
                    story.append(Spacer(1, 6))
            doc.build(story)
            return str(pdf_path)
        except Exception:
            return None


def generate_report(
    question: str,
    sql_result: dict,
    validation_result: dict,
    query_result: dict,
    reliability_result: dict,
    insight_result: dict,
    semantic: dict | None = None,
    output_dir: str = "output",
) -> dict:
    """Generate JSON, HTML, PDF, and CSV report artifacts."""
    out = _ensure_output_dir(output_dir)
    payload = _build_report_payload(question, sql_result, validation_result, query_result, reliability_result, insight_result, semantic)
    query_id = query_result.get("query_id") or _safe_slug(question)
    html = _render_html_report(payload)

    json_path = out / f"{query_id}_report.json"
    html_path = out / f"{query_id}_report.html"
    json_path.write_text(json.dumps(payload, indent=2, default=_json_default), encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")

    csv_paths = _write_csv_files(out, payload, query_result)
    pdf_path = _write_pdf_if_possible(out, html, query_id)

    return {
        "success": True,
        "query_id": query_id,
        "json_path": str(json_path),
        "html_path": str(html_path),
        "pdf_path": pdf_path,
        **csv_paths,
        "payload": payload,
    }