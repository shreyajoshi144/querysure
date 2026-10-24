"""
IMP-4: several render paths inserted user/LLM-influenced text into HTML with
no escaping -- a malicious cell value, insight text, validation message, or
(for raw .db/.sqlite uploads, which skip the table-name sanitization
CSV/JSON/XLSX uploads get) a crafted table name could execute script in the
viewer's own browser. Same bug in the standalone HTML report file.

app.js has no test runner in this repo, so the frontend fix is verified by
running the actual file through Node and calling the real functions against
a DOM (jsdom) -- not by re-implementing escapeHtml() and asserting on that.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from modules.report_generator import _render_html_report

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(REPO_ROOT, "static", "js", "app.js")

PAYLOAD = '<img src=x onerror=alert(document.cookie)>'


# ===================================================================
# Frontend: static/js/app.js render functions
# ===================================================================

def _node_available() -> bool:
    """True only if both node and jsdom (see package.json, `npm install`)
    are available -- not just node itself."""
    if shutil.which("node") is None:
        return False
    r = subprocess.run(["node", "-e", "require('jsdom')"], capture_output=True, cwd=REPO_ROOT)
    return r.returncode == 0


def _extract_function(src: str, name: str) -> str:
    """Pulls one top-level `function name(...) {...}` declaration out of
    app.js by brace-counting from the source (app.js has no build step /
    module system to import from, and the file's top-level statements
    assume a full app shell we don't want to construct here -- so the
    functions under test are extracted directly rather than re-typed)."""
    start = src.index(f"function {name}(")
    open_brace = src.index("{", start)
    depth = 0
    i = open_brace
    while True:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1


def _run_render_functions(js_calls: str) -> dict:
    """Loads the ACTUAL escapeHtml/renderTable/renderBulletList/
    renderValidationTab/renderRetrievalTab function bodies straight out of
    the shipped static/js/app.js (not reimplemented), evaluates them against
    a real jsdom document, calls the given render function(s), and returns
    each element's innerHTML."""
    src = open(APP_JS, encoding="utf-8").read()
    functions = "\n".join(
        _extract_function(src, name)
        for name in ("escapeHtml", "renderTable", "renderBulletList", "renderValidationTab", "renderRetrievalTab")
    )

    script = f"""
    const {{ JSDOM }} = require('jsdom');
    const ids = ['resultsTable', 'findingsList', 'valContent', 'retrievalContent', 'emptyState', 'resultsState'];
    const bodyHtml = ids.map(id =>
      id === 'resultsTable'
        ? `<table id="${{id}}"><thead></thead><tbody></tbody></table>`
        : `<div id="${{id}}"></div>`
    ).join('');
    const dom = new JSDOM(`<!doctype html><html><body>${{bodyHtml}}</body></html>`, {{ url: 'http://localhost/' }});
    global.document = dom.window.document;
    const $ = id => document.getElementById(id);

    (new Function('$', {json.dumps(functions)} + `
      {js_calls}
    `))($);

    const out = {{}};
    for (const id of ['resultsTable', 'findingsList', 'valContent', 'retrievalContent']) {{
      out[id] = document.getElementById(id).innerHTML;
    }}
    console.log(JSON.stringify(out));
    """
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=REPO_ROOT)
    assert r.returncode == 0, f"node script failed:\nSTDOUT: {r.stdout}\nSTDERR: {r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


needs_node = pytest.mark.skipif(not _node_available(), reason="node is required to exercise the real app.js")


@needs_node
def test_render_table_escapes_column_names_and_cell_values():
    """Column names come straight from the user's schema (unsanitized for
    raw .db/.sqlite uploads); cell values come from the user's own data."""
    out = _run_render_functions(f"""
      renderTable([{{ {json.dumps(PAYLOAD)}: {json.dumps(PAYLOAD)} }}]);
    """)
    html = out["resultsTable"]
    assert PAYLOAD not in html, "raw payload reached the DOM unescaped"
    assert "&lt;img" in html and "onerror" in html  # content preserved, just neutralized


@needs_node
def test_render_table_still_formats_numbers_normally():
    """The fix must not turn numeric cells into escaped strings."""
    out = _run_render_functions("renderTable([{ amount: 1234567 }]);")
    assert "1,234,567" in out["resultsTable"]


@needs_node
def test_render_bullet_list_escapes_insight_text():
    out = _run_render_functions(f"""
      renderBulletList('findingsList', [{json.dumps(PAYLOAD)}], 'finding', '\\u25c6');
    """)
    html = out["findingsList"]
    assert PAYLOAD not in html
    assert "&lt;img" in html


@needs_node
def test_render_validation_tab_escapes_issue_and_warning_messages():
    """Mirrors modules/sql_validator.py, which embeds a raw table/column
    name straight into "unknown_table"/"unknown_column" messages."""
    out = _run_render_functions(f"""
      renderValidationTab({{
        is_valid: false,
        validation_score: 10,
        issues: [{{ message: {json.dumps(PAYLOAD)} }}],
        warnings: [{json.dumps(PAYLOAD)}],
      }});
    """)
    html = out["valContent"]
    assert PAYLOAD not in html
    assert html.count("&lt;img") == 2  # one from issues, one from warnings


@needs_node
def test_render_retrieval_tab_escapes_table_name():
    """The table name here comes straight from the user's schema -- for a
    raw .db/.sqlite upload, nothing sanitizes it before this point."""
    out = _run_render_functions(f"""
      renderRetrievalTab({{ top_tables: [{{ table: {json.dumps(PAYLOAD)}, score: 0.9 }}] }});
    """)
    html = out["retrievalContent"]
    assert PAYLOAD not in html
    assert "&lt;img" in html


# ===================================================================
# Backend: the standalone HTML report (modules/report_generator.py)
# ===================================================================

def _minimal_report_payload(**overrides) -> dict:
    payload = {
        "question": "how much revenue",
        "sql_confidence": "High",
        "sql_complexity": "Simple",
        "generated_sql": "SELECT 1",
        "validation": {"is_valid": True, "validation_score": 100, "risk_score": 0,
                       "join_validation": {}, "aggregation_validation": {}, "fanout_risk": "Low"},
        "query": {"row_count": 1, "columns": ["x"], "status": "success", "query_id": "q1",
                  "execution_metadata": {}, "result_profile": {}},
        "reliability": {"reliability_score": 90, "reliability_level": "High", "status": "ok"},
        "insights": {"summary": "", "insight_confidence": "High", "executive_note": "",
                     "key_findings": [], "anomalies": [], "recommendations": []},
        "generated_at": "2026-01-01T00:00:00Z",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("field,value", [
    ("question", PAYLOAD),
    ("generated_sql", PAYLOAD),
    ("sql_confidence", PAYLOAD),
])
def test_html_report_escapes_top_level_payload_fields(field, value):
    html = _render_html_report(_minimal_report_payload(**{field: value}))
    assert PAYLOAD not in html
    assert "&lt;img" in html


def test_html_report_escapes_validation_fanout_risk_and_dumped_json():
    payload = _minimal_report_payload()
    payload["validation"] = {**payload["validation"], "fanout_risk": PAYLOAD,
                              "unexpected_field": PAYLOAD}  # flows into json.dumps(validation)
    html = _render_html_report(payload)
    assert PAYLOAD not in html
    assert html.count("&lt;img") >= 2


def test_html_report_escapes_insight_lists():
    payload = _minimal_report_payload(insights={
        "summary": PAYLOAD, "insight_confidence": "High", "executive_note": PAYLOAD,
        "key_findings": [PAYLOAD], "anomalies": [PAYLOAD], "recommendations": [PAYLOAD],
    })
    html = _render_html_report(payload)
    assert PAYLOAD not in html
    # summary + executive_note + one item each in key_findings/anomalies/recommendations
    assert html.count("&lt;img") == 5


def test_html_report_result_profile_json_is_escaped():
    """result_profile is built from the user's own query results (column
    names / sample values), then json.dumps'd straight into a <pre> block."""
    payload = _minimal_report_payload()
    payload["query"] = {**payload["query"], "result_profile": {"columns": [PAYLOAD]}}
    html = _render_html_report(payload)
    assert PAYLOAD not in html
    assert "&lt;img" in html


def test_html_report_stays_valid_and_readable_for_ordinary_content():
    """The fix must not mangle normal, non-malicious content."""
    html = _render_html_report(_minimal_report_payload(
        question="What was revenue by region in Q3?",
        generated_sql="SELECT region, SUM(amount) FROM sales GROUP BY region",
    ))
    assert "What was revenue by region in Q3?" in html
    assert "SELECT region, SUM(amount) FROM sales GROUP BY region" in html
    assert html.startswith("<!doctype html>")
