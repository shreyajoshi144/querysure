"""Human- and machine-readable rendering of a report dict."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _ms(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.1f} ms"


def _headline_rows(m: Dict[str, Any]) -> List[tuple]:
    a, h, rj, ov = m["answerable"], m["hallucination"], m["rejection"], m["overall"]
    return [
        ("Behavior accuracy (answer right OR decline right)", _pct(ov["behavior_accuracy"]), f"{ov['cases']} cases"),
        ("SQL validity rate", _pct(a["sql_validity_rate"]), f"of {a['n']} answerable"),
        ("Execution success rate", _pct(a["execution_success_rate"]), f"of {a['n']} answerable"),
        ("Execution accuracy (result-set match)", _pct(a["execution_accuracy"]), f"of {a['n']} answerable"),
        ("  ...tolerating extra columns", _pct(a["execution_accuracy_superset_columns"]), ""),
        ("  ...given the query executed", _pct(a["execution_accuracy_given_executed"]), "wrong-answer rate isolated"),
        ("Expected-table grounding rate", _pct(a["table_grounding_rate"]), f"of {a['n']} answerable"),
        ("Hallucinated table/column - case level", _pct(h["case_level_rate"]), f"of {h['cases_with_sql']} cases with SQL"),
        ("Hallucinated table/column - identifier level", _pct(h["identifier_level_rate"]), "of identifier references"),
        ("Hallucinations the validator let through", _pct(h["validator_escape_rate"]), f"of {h['cases_with_hallucination']} hallucinating"),
        ("Rejection/clarification accuracy (strict)", _pct(rj["strict_accuracy"]), f"of {rj['n']} should-decline"),
        ("Rejection/clarification accuracy (lenient)", _pct(rj["lenient_accuracy"]), "+ run-time database errors"),
        ("Answered instead of declining", _pct(rj["answered_instead_rate"]), "the dangerous failure"),
        ("'Low confidence' on correct answers", _pct(m["confidence_signal"]["low_on_correct_answers"]), "high = flag is noisy"),
        ("'Low confidence' on wrongly-answered declines", _pct(m["confidence_signal"]["low_on_wrongly_answered_decline_cases"]), "high = flag is useful"),
        ("Latency mean / p95", f"{_ms(m['latency_ms']['mean'])} / {_ms(m['latency_ms']['p95'])}",
         "incl. LLM" if m["latency_ms"]["includes_real_llm_time"] else "excludes LLM time"),
    ]


def _cost_line(m: Dict[str, Any]) -> str:
    c = m["cost"]
    if not c["applicable"]:
        return "Estimated LLM cost: n/a (offline source, no LLM calls)"
    return (f"Estimated LLM cost: ${c['estimated_total_usd']:.4f} total, "
            f"${c['estimated_mean_usd_per_query']:.6f}/query  [{c['basis']}]")


def _failures(report: Dict[str, Any], limit: int = 25) -> List[Dict[str, Any]]:
    return [c for c in report["cases"] if c["verdict"] != "correct"][:limit]


def render_console(report: Dict[str, Any]) -> str:
    meta, m = report["meta"], report["metrics"]
    lines: List[str] = []
    bar = "=" * 92
    lines += [bar, f"QuerySure evaluation  |  mode={meta['mode']}  source={meta['source']['name']}"
                   f"  |  {meta['dataset']['cases_run']}/{meta['dataset']['cases_total']} cases", bar]
    for w in meta["warnings"]:
        lines.append(f"  ! {w}")
    lines.append("")
    label_w = max(len(r[0]) for r in _headline_rows(m))
    for label, value, note in _headline_rows(m):
        lines.append(f"  {label:<{label_w}}  {value:>16}   {note}")
    lines.append("")
    lines.append(f"  {_cost_line(m)}")
    lines.append("")
    lines.append("  By category                 cases   pass    exec-success")
    for cat, e in m["by_category"].items():
        lines.append(f"    {cat:<24} {e['n']:>5}   {_pct(e['pass_rate']):>6}   {_pct(e.get('execution_success_rate')):>8}")
    lines.append("")
    lines.append("  By difficulty               cases   pass")
    for d, e in m["by_difficulty"].items():
        lines.append(f"    {d:<24} {e['n']:>5}   {_pct(e['pass_rate']):>6}")
    lines.append("")
    lines.append("  Outcome distribution: " + ", ".join(f"{k}={v}" for k, v in m["failure_modes"].items()))
    fails = _failures(report, 15)
    if fails:
        lines.append("")
        lines.append(f"  First {len(fails)} non-passing cases:")
        for c in fails:
            detail = c.get("result_mismatch_reason") or (c.get("error") or "")
            lines.append(f"    {c['id']:<12} {c['failure_mode']:<22} {detail[:70]}")
    lines.append(bar)
    return "\n".join(lines)


def render_markdown(report: Dict[str, Any]) -> str:
    meta, m = report["meta"], report["metrics"]
    L: List[str] = []
    L.append(f"# QuerySure evaluation report\n")
    L.append(f"- **Mode:** `{meta['mode']}` (source `{meta['source']['name']}`, LLM calls: {meta['source']['calls_llm']})")
    L.append(f"- **Dataset:** `{meta['dataset']['version']}`, {meta['dataset']['cases_run']}/{meta['dataset']['cases_total']} cases, sha256 `{meta['dataset']['sha256'][:12]}`")
    L.append(f"- **Database:** {meta['database']['source']}, fingerprint `{meta['database']['fingerprint'][:12]}`")
    L.append(f"- **Generated:** {meta['generated_at']}  |  Python {meta['environment']['python']}, sqlglot {meta['environment']['sqlglot']}, SQLite {meta['environment']['sqlite']}\n")
    if meta["warnings"]:
        L.append("## Read this first\n")
        L += [f"> {w}\n" for w in meta["warnings"]]
    L.append("## Headline metrics\n")
    L.append("| Metric | Value | Denominator / note |\n|---|---:|---|")
    for label, value, note in _headline_rows(m):
        L.append(f"| {label.strip()} | {value} | {note} |")
    L.append(f"\n{_cost_line(m)}\n")
    L.append("## By category\n")
    L.append("| Category | Cases | Pass rate | Execution success |\n|---|---:|---:|---:|")
    for cat, e in m["by_category"].items():
        L.append(f"| {cat} | {e['n']} | {_pct(e['pass_rate'])} | {_pct(e.get('execution_success_rate'))} |")
    L.append("\n## By difficulty\n")
    L.append("| Difficulty | Cases | Pass rate |\n|---|---:|---:|")
    for d, e in m["by_difficulty"].items():
        L.append(f"| {d} | {e['n']} | {_pct(e['pass_rate'])} |")
    L.append("\n## Rejection / clarification by reason\n")
    L.append("| Reason | Cases | Strict | Lenient |\n|---|---:|---:|---:|")
    for reason, e in m["rejection"]["by_reason"].items():
        L.append(f"| {reason} | {e['n']} | {_pct(e['strict_accuracy'])} | {_pct(e['lenient_accuracy'])} |")
    L.append("\n## Outcome distribution\n")
    L.append("| Outcome | Cases |\n|---|---:|")
    for k, v in m["failure_modes"].items():
        L.append(f"| {k} | {v} |")
    fails = _failures(report, 100)
    if fails:
        L.append("\n## Non-passing cases\n")
        L.append("| Case | Category | Outcome | Detail |\n|---|---|---|---|")
        for c in fails:
            detail = (c.get("result_mismatch_reason") or c.get("error") or "").replace("|", "\\|").replace("\n", " ")[:120]
            L.append(f"| {c['id']} | {c['category']} | {c['failure_mode']} | {detail} |")
    L.append("")
    return "\n".join(L)


def write_report(report: Dict[str, Any], out_dir: str, stem: str) -> Dict[str, str]:
    """Write <stem>.json and <stem>.md into out_dir; return their paths."""
    os.makedirs(out_dir, exist_ok=True)
    jpath = os.path.join(out_dir, f"{stem}.json")
    mpath = os.path.join(out_dir, f"{stem}.md")
    with open(jpath, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=False)
    with open(mpath, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report))
    return {"json": jpath, "markdown": mpath}
