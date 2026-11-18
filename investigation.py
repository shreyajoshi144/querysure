"""Investigations (Sprint 10): deterministic multi-step analyses of one dataset that keep FACTS, HYPOTHESES, LIMITATIONS and
RECOMMENDATIONS apart.

* Facts are computed by read-only SQL through the existing ``modules.query_result_engine.execute_query`` (authorizer, timeout, row cap).
  Identifiers come only from the dataset's own schema and are quoted; values are bound as parameters. Each fact keeps the SQL that
  produced it, so every number can be re-run.
* Hypotheses are rule-based statements that cite the fact ids they rest on and are labelled unverified. No model writes any number.
* Document excerpts (``knowledge.search``) are shown as untrusted context and never feed a fact or a hypothesis.
* The same dataset version and parameters always give the same facts (tests/test_s10_investigations.py).
"""
from __future__ import annotations

import html as _html
import json
import re
from statistics import mean, median, pstdev
from typing import Any, Dict, List, Optional

import config
import knowledge
from db import DatasetQualityReport, Investigation, Report
from modules.query_result_engine import execute_query

KINDS = ("trend", "comparison", "breakdown")
AGGS = ("sum", "avg", "count")
_DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?([T ].*)?$")
_NUM_DECL = re.compile(r"INT|REAL|FLOA|DOUB|NUM|DEC", re.I)
_PERIOD = re.compile(r"^\d{4}-\d{2}$")


class InvestigationError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _id(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _run(path: str, sql: str, params: Optional[list] = None) -> List[dict]:
    res = execute_query(sql, path, max_rows=config.INVESTIGATION_MAX_ROWS, timeout_seconds=config.INVESTIGATION_QUERY_TIMEOUT_SECONDS,
                        params=params or [])
    if not res.get("success"):
        raise InvestigationError("QUERY_FAILED", "The analysis query could not be run on this dataset.", 422)
    if (res.get("execution_metadata") or {}).get("truncated"):
        raise InvestigationError("TOO_MANY_ROWS", f"The analysis would produce more than {config.INVESTIGATION_MAX_ROWS} groups, so its totals and shares "
                                 "would be wrong. Pick a column with fewer distinct values.", 422)
    return res.get("rows") or []


# ------------------------------------------------------------------ schema
def introspect(path: str) -> Dict[str, dict]:
    """{table: {rows, columns: [{name, kind}]}} with kind date | number | category | text, guessed from declared types and a sample."""
    import sqlite3
    import time
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    deadline = time.monotonic() + config.INVESTIGATION_QUERY_TIMEOUT_SECONDS
    con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)   # a huge dataset must not hold the request open
    out: Dict[str, dict] = {}
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                                           "AND substr(name,1,4) <> '_qs_' ORDER BY name LIMIT 50")]
        for t in names:
            rows = con.execute(f"SELECT COUNT(*) FROM {_id(t)}").fetchone()[0]
            cols = []
            for _, name, declared, *_rest in list(con.execute(f"PRAGMA table_info({_id(t)})"))[:100]:
                sample = con.execute(f"SELECT typeof({_id(name)}), {_id(name)} FROM {_id(t)} WHERE {_id(name)} IS NOT NULL LIMIT 50").fetchall()
                kind = "text"
                if sample:
                    if all(ty in ("integer", "real") for ty, _ in sample) or (_NUM_DECL.search(declared or "") and all(ty != "text" or re.fullmatch(r"-?\d+(\.\d+)?", str(v)) for ty, v in sample)):
                        kind = "number"
                    elif sum(1 for ty, v in sample if ty == "text" and _DATE.match(v)) >= 0.9 * len(sample):
                        kind = "date"
                    else:
                        distinct = con.execute(f"SELECT COUNT(DISTINCT c) FROM (SELECT {_id(name)} AS c FROM {_id(t)} LIMIT 100000)").fetchone()[0]
                        kind = "category" if 1 < distinct <= 50 and distinct <= max(2, rows // 2) else "text"
                cols.append({"name": name, "kind": kind})
            out[t] = {"rows": rows, "columns": cols}
    except sqlite3.OperationalError as exc:
        raise InvestigationError("DATASET_TOO_SLOW", "Reading this dataset's structure took too long.", 422) from exc
    finally:
        con.close()
    return out


# ------------------------------------------------------------------ helpers
def _pct(a: float, b: float) -> Optional[float]:
    return None if not a else round((b - a) / abs(a) * 100, 1)


def _num(x: Any) -> float:
    return float(x) if x is not None else 0.0


class _Report:
    def __init__(self):
        self.facts: List[dict] = []
        self.hypotheses: List[dict] = []
        self.limitations: List[str] = []
        self.recommendations: List[dict] = []
        self.tables: List[dict] = []

    def fact(self, text: str, sql: Optional[str] = None, value: Any = None) -> str:
        fid = f"F{len(self.facts) + 1}"
        self.facts.append({"id": fid, "text": text, "value": value, "sql": sql})
        return fid

    def hypothesis(self, text: str, based_on: List[str]) -> None:
        self.hypotheses.append({"id": f"H{len(self.hypotheses) + 1}", "text": text, "based_on": based_on, "status": "unverified"})

    def recommend(self, text: str, because: List[str]) -> None:
        self.recommendations.append({"text": text, "because": because})


def _agg_sql(agg: str, measure: Optional[str]) -> str:
    return "COUNT(*)" if agg == "count" else f"{agg.upper()}(CAST({_id(measure)} AS REAL))"


def _validate(schema: Dict[str, dict], p: dict) -> dict:
    kind, t = p.get("kind"), p.get("table")
    if kind not in KINDS:
        raise InvestigationError("INVALID_KIND", f"kind must be one of {list(KINDS)}.")
    if t not in schema:
        raise InvestigationError("INVALID_TABLE", "Unknown table.")
    cols = {c["name"]: c["kind"] for c in schema[t]["columns"]}
    agg = p.get("agg") or "sum"
    if agg not in AGGS:
        raise InvestigationError("INVALID_AGG", f"agg must be one of {list(AGGS)}.")
    out = {"kind": kind, "table": t, "agg": agg}
    for key, want in (("measure", ("number",)), ("date_column", ("date",)), ("dimension", ("category", "text"))):
        v = p.get(key)
        if v in (None, ""):
            continue
        if v not in cols or cols[v] not in want:
            raise InvestigationError("INVALID_COLUMN", f"{key} {v!r} is not a usable {'/'.join(want)} column of {t}.")
        out[key] = v
    if agg != "count" and "measure" not in out:
        raise InvestigationError("MEASURE_REQUIRED", "A numeric measure is required unless agg is count.")
    if kind in ("trend", "comparison") and "date_column" not in out:
        raise InvestigationError("DATE_REQUIRED", f"A date column is required for a {kind}.")
    if kind == "breakdown" and ("dimension" not in out or agg == "avg"):
        raise InvestigationError("DIMENSION_REQUIRED", "A breakdown needs a dimension and agg sum or count.")
    if p.get("split_at"):
        if not _PERIOD.match(str(p["split_at"])):
            raise InvestigationError("INVALID_SPLIT", "split_at must look like YYYY-MM.")
        out["split_at"] = p["split_at"]
    return out


def _period_series(path: str, rep: _Report, q: dict) -> List[dict]:
    t, d, a = _id(q["table"]), _id(q["date_column"]), _agg_sql(q["agg"], q.get("measure"))
    where = f"{d} IS NOT NULL AND {d} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]*'" + ("" if q["agg"] == "count" else f" AND {_id(q['measure'])} IS NOT NULL")
    sql = f"SELECT substr({d},1,7) AS period, {a} AS value, COUNT(*) AS n FROM {t} WHERE {where} GROUP BY 1 ORDER BY 1"
    rows = _run(path, sql)
    if len(rows) < 2:
        raise InvestigationError("INSUFFICIENT_DATA", "At least two months of dated rows are needed.")
    total = _run(path, f"SELECT COUNT(*) AS c FROM {t}")[0]["c"]
    used = sum(int(r["n"]) for r in rows)
    rep.series_sql = sql
    if total - used:
        rep.limitations.append(f"{total - used} of {total} rows have no usable date{'' if q['agg'] == 'count' else ' or measure'} and are excluded.")
    rep.tables.append({"name": "By month", "columns": ["period", "value", "rows"],
                       "rows": [[r["period"], round(_num(r["value"]), 2), int(r["n"])] for r in rows][:120]})
    if len(rows) > 120:
        rep.limitations.append("Only the first 120 months are listed in the table; statistics use all months.")
    return rows


def _by_segment(path: str, q: dict, period_sql: str, params: list) -> Dict[str, float]:
    t, d, s = _id(q["table"]), _id(q["date_column"]), _id(q["dimension"])
    where = f"{d} IS NOT NULL AND {d} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]*' AND {period_sql}"
    if q["agg"] != "count":
        where += f" AND {_id(q['measure'])} IS NOT NULL"
    rows = _run(path, f"SELECT COALESCE(CAST({s} AS TEXT), '(blank)') AS seg, {_agg_sql(q['agg'], q.get('measure'))} AS v FROM {t} WHERE {where} GROUP BY 1", params)
    return {r["seg"]: _num(r["v"]) for r in rows}


def _driver(path: str, rep: _Report, q: dict, a_sql: str, a_params: list, b_sql: str, b_params: list, label_a: str, label_b: str, fact_ids: List[str]) -> None:
    A, B = _by_segment(path, q, a_sql, a_params), _by_segment(path, q, b_sql, b_params)
    deltas = sorted(((s, B.get(s, 0.0) - A.get(s, 0.0), A.get(s, 0.0), B.get(s, 0.0)) for s in set(A) | set(B)), key=lambda x: -abs(x[1]))
    total = sum(d for _, d, _, _ in deltas)
    rep.tables.append({"name": f"Change by {q['dimension']} ({label_a} to {label_b})", "columns": [q["dimension"], label_a, label_b, "change"],
                       "rows": [[s, round(a, 2), round(b, 2), round(d, 2)] for s, d, a, b in deltas[:10]]})
    if not deltas or not total:
        return
    top, d0, a0, b0 = deltas[0]
    share = d0 / total * 100
    fid = rep.fact(f"{q['dimension']} = {top!r} changed by {d0:+,.2f} ({a0:,.2f} to {b0:,.2f}), {share:.0f}% of the total change of {total:+,.2f}.", value={"segment": top, "share_pct": round(share, 1)})
    if share >= 50 and len(deltas) > 1:
        rep.hypothesis(f"The change is concentrated in {q['dimension']} = {top!r}; something specific to it (an event, a data change, a policy) may explain much of it.", fact_ids + [fid])
        rep.recommend(f"Look at {q['dimension']} = {top!r} first, e.g. ask the owner of that segment what changed between {label_a} and {label_b}.", [fid])
    elif share < 50 and len(deltas) > 2:
        rep.hypothesis(f"The change is spread across several values of {q['dimension']} (the largest explains {share:.0f}%), which points to a broad cause rather than one segment.", fact_ids + [fid])


def _trend(path: str, rep: _Report, q: dict) -> None:
    rows = _period_series(path, rep, q)
    vals, per, ns = [_num(r["value"]) for r in rows], [r["period"] for r in rows], [int(r["n"]) for r in rows]
    f1 = rep.fact(f"{len(rows)} months from {per[0]} to {per[-1]}; the {q['agg']} went from {vals[0]:,.2f} to {vals[-1]:,.2f}"
                  f"{'' if _pct(vals[0], vals[-1]) is None else f' ({_pct(vals[0], vals[-1]):+.1f}%)'}.", rep.series_sql, {"first": vals[0], "last": vals[-1]})
    hi, lo = max(range(len(vals)), key=vals.__getitem__), min(range(len(vals)), key=vals.__getitem__)
    f2 = rep.fact(f"Peak {vals[hi]:,.2f} in {per[hi]}; low {vals[lo]:,.2f} in {per[lo]}.", rep.series_sql)
    xs = list(range(len(vals)))
    mx, my = mean(xs), mean(vals)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, vals)) / sum((x - mx) ** 2 for x in xs)
    rel = slope / abs(my) * 100 if my else 0.0
    direction = "rising" if rel > 1 else "falling" if rel < -1 else "roughly flat"
    f3 = rep.fact(f"Overall the series is {direction} (fitted slope {rel:+.1f}% of the average per month).", rep.series_sql, {"direction": direction})
    steps = [(abs(vals[i] - vals[i - 1]), i) for i in range(1, len(vals))]
    big, bi = max(steps)
    f4 = rep.fact(f"The largest month-to-month move was {per[bi - 1]} to {per[bi]}: {vals[bi] - vals[bi - 1]:+,.2f}.", rep.series_sql, {"from": per[bi - 1], "to": per[bi]})
    if len(ns) >= 4 and ns[-1] < 0.5 * median(ns[:-1]):
        rep.hypothesis(f"The latest month ({per[-1]}) has only {ns[-1]} rows against a typical {median(ns[:-1]):.0f}, so it may be incomplete and understate the trend.", [f1])
        rep.recommend("Confirm the latest month is complete before quoting its change.", [f1])
    sd = pstdev(vals)
    if sd and len(vals) >= 5 and vals[hi] > mean(vals) + 2 * sd:
        rep.hypothesis(f"The peak in {per[hi]} is more than two standard deviations above the average, so it may be a one-off rather than a pattern.", [f2])
    if q.get("dimension"):
        d = _id(q["date_column"])
        _driver(path, rep, q, f"substr({d},1,7) = ?", [per[bi - 1]], f"substr({d},1,7) = ?", [per[bi]], per[bi - 1], per[bi], [f4])
    rep.trend_facts = (f1, f3)


def _comparison(path: str, rep: _Report, q: dict) -> None:
    rows = _period_series(path, rep, q)
    per = [r["period"] for r in rows]
    boundary = q.get("split_at") or per[len(per) // 2]
    A, B = [r for r in rows if r["period"] < boundary], [r for r in rows if r["period"] >= boundary]
    if not A or not B:
        raise InvestigationError("INVALID_SPLIT", f"split_at {boundary} leaves one side empty (data covers {per[0]} to {per[-1]}).")

    def total(part):
        if q["agg"] == "avg":
            n = sum(int(r["n"]) for r in part)
            return sum(_num(r["value"]) * int(r["n"]) for r in part) / n
        return sum(_num(r["value"]) for r in part)
    ta, tb = total(A), total(B)
    la, lb = f"{A[0]['period']}..{A[-1]['period']}", f"{B[0]['period']}..{B[-1]['period']}"
    pct = _pct(ta, tb)
    f1 = rep.fact(f"{q['agg']} in {la} ({len(A)} months) is {ta:,.2f}; in {lb} ({len(B)} months) it is {tb:,.2f}: {tb - ta:+,.2f}{'' if pct is None else f' ({pct:+.1f}%)'}.",
                  rep.series_sql, {"a": ta, "b": tb})
    if q["agg"] != "avg" and len(A) != len(B):
        rep.limitations.append(f"The two periods have different lengths ({len(A)} vs {len(B)} months); totals are not directly comparable, compare per-month averages instead.")
        f2 = rep.fact(f"Per month: {ta / len(A):,.2f} before vs {tb / len(B):,.2f} after.", rep.series_sql)
        f1 = f2
    if q.get("dimension"):
        d = _id(q["date_column"])
        _driver(path, rep, q, f"substr({d},1,7) < ?", [boundary], f"substr({d},1,7) >= ?", [boundary], la, lb, [f1])
    if pct is not None and abs(pct) < 2:
        rep.hypothesis("The two periods are within 2% of each other, so any difference may be noise.", [f1])


def _breakdown(path: str, rep: _Report, q: dict) -> None:
    t, s = _id(q["table"]), _id(q["dimension"])
    where = "1=1" if q["agg"] == "count" else f"{_id(q['measure'])} IS NOT NULL"
    sql = f"SELECT COALESCE(CAST({s} AS TEXT), '(blank)') AS seg, {_agg_sql(q['agg'], q.get('measure'))} AS value, COUNT(*) AS n FROM {t} WHERE {where} GROUP BY 1 ORDER BY value DESC"
    rows = _run(path, sql)
    if not rows:
        raise InvestigationError("INSUFFICIENT_DATA", "No rows to break down.")
    vals = [_num(r["value"]) for r in rows]
    total = sum(vals)
    top = [(r["seg"], v, v / total * 100 if total else 0.0) for r, v in zip(rows, vals)]
    rep.tables.append({"name": f"{q['agg']} by {q['dimension']}", "columns": [q["dimension"], "value", "share %", "rows"],
                       "rows": [[seg, round(v, 2), round(sh, 1), int(r["n"])] for (seg, v, sh), r in zip(top[:15], rows)]})
    if len(rows) > 15:
        rep.limitations.append(f"Only the top 15 of {len(rows)} values are listed; shares use all of them.")
    rep.fact(f"{len(rows)} values of {q['dimension']}; total {q['agg']} {total:,.2f}.", sql, {"segments": len(rows), "total": total})
    f2 = rep.fact(f"The largest, {top[0][0]!r}, holds {top[0][2]:.1f}% of the total; the top 3 hold {sum(x[2] for x in top[:3]):.1f}%.", sql, {"top_share": round(top[0][2], 1)})
    if any(v < 0 for v in vals):
        rep.limitations.append("Some values are negative, so shares can exceed 100% or mislead.")
    if top[0][2] >= 50 and len(rows) > 1:
        rep.hypothesis(f"Results depend heavily on {top[0][0]!r}; a change there would move the total, and the other values matter little.", [f2])
        rep.recommend(f"Treat {top[0][0]!r} separately when reporting this measure, and check how reliable its source is.", [f2])
    blank = next((r for r in rows if r["seg"] == "(blank)"), None)
    if blank is not None and total and _num(blank["value"]) / total > 0.05:
        rep.limitations.append(f"{_num(blank['value']) / total * 100:.0f}% of the total sits under a blank {q['dimension']}.")
    rep.top_segments = [x[0] for x in top[:3]]


def _context(db, ws_id: str, q: dict, rep: _Report) -> List[dict]:
    words = [q["table"], q.get("measure") or "", q.get("dimension") or ""] + [str(s)[:40] for s in getattr(rep, "top_segments", [])]
    found = knowledge.search(db, ws_id, " ".join(w for w in words if w), top_k=3)
    return [{"document": p["document_name"], "page": p["page"], "text": p["text"][:400], "score": p["score"]} for p in found]


def run(db, ds, path: str, params: dict) -> dict:
    """Run one investigation on the dataset file ``path`` and return the structured report (not stored)."""
    schema = introspect(path)
    q = _validate(schema, params)
    if schema[q["table"]]["rows"] > config.INVESTIGATION_MAX_TABLE_ROWS:
        raise InvestigationError("TABLE_TOO_LARGE", f"{q['table']} has {schema[q['table']]['rows']:,} rows; investigations are limited to "
                                 f"{config.INVESTIGATION_MAX_TABLE_ROWS:,} rows per table.", 422)
    rep = _Report()
    {"trend": _trend, "comparison": _comparison, "breakdown": _breakdown}[q["kind"]](path, rep, q)
    qr = db.query(DatasetQualityReport).filter(DatasetQualityReport.dataset_id == ds.id).order_by(DatasetQualityReport.created_at.desc()).first()
    if qr is not None and qr.status in ("warn", "error"):
        codes = sorted({f.get("code") for f in (qr.findings or []) if f.get("code")})[:5]
        rep.limitations.append(f"The data-quality report of version {qr.dataset_version} is '{qr.status}' ({', '.join(codes) or 'see report'}); numbers may be affected.")
    rep.limitations.append("Column roles (date, number, category) are guessed from declared types and a sample of the data.")
    rep.limitations.append("Findings describe correlation within this dataset only; they do not establish causes.")
    title = {"trend": "Trend", "comparison": "Period comparison", "breakdown": "Segment breakdown"}[q["kind"]] + f": {q['agg']}" + \
        (f" of {q['measure']}" if q.get("measure") else "") + f" in {q['table']}"
    return {"title": title, "kind": q["kind"], "params": q, "dataset": {"id": ds.id, "name": ds.name, "version": ds.version},
            "facts": rep.facts, "hypotheses": rep.hypotheses, "recommendations": rep.recommendations, "limitations": rep.limitations,
            "tables": rep.tables, "context_documents": _context(db, ds.workspace_id, q, rep)}


# ------------------------------------------------------------------ rendering
def render_html(report: dict) -> str:
    e = lambda v: _html.escape(str(v), quote=True)  # noqa: E731
    out = [f"<!doctype html><html><head><meta charset='utf-8'><title>{e(report['title'])}</title>"
           "<style>body{font-family:system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;color:#222}h2{margin-top:2rem}"
           "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px;text-align:left}.tag{font-size:.75rem;text-transform:uppercase;"
           "letter-spacing:.05em;color:#555}code{background:#f3f3f3;padding:1px 4px;display:block;overflow-x:auto;margin:4px 0}</style></head><body>",
           f"<h1>{e(report['title'])}</h1><p>Dataset: {e(report['dataset']['name'])} (version {e(report['dataset']['version'])})</p>",
           "<h2>Facts <span class='tag'>computed from the data</span></h2><ul>"]
    for f in report["facts"]:
        out.append(f"<li><b>{e(f['id'])}</b> {e(f['text'])}" + (f"<code>{e(f['sql'])}</code>" if f.get("sql") else "") + "</li>")
    out.append("</ul><h2>Hypotheses <span class='tag'>unverified, not facts</span></h2><ul>")
    out += [f"<li><b>{e(h['id'])}</b> {e(h['text'])} <span class='tag'>based on {e(', '.join(h['based_on']))}</span></li>" for h in report["hypotheses"]] or ["<li>None.</li>"]
    out.append("</ul><h2>Recommendations <span class='tag'>suggestions</span></h2><ul>")
    out += [f"<li>{e(r['text'])} <span class='tag'>because {e(', '.join(r['because']))}</span></li>" for r in report["recommendations"]] or ["<li>None.</li>"]
    out.append("</ul><h2>Limitations</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in report["limitations"]) + "</ul>")
    for t in report["tables"]:
        out.append(f"<h2>{e(t['name'])}</h2><table><tr>" + "".join(f"<th>{e(c)}</th>" for c in t["columns"]) + "</tr>" +
                   "".join("<tr>" + "".join(f"<td>{e(c)}</td>" for c in row) + "</tr>" for row in t["rows"]) + "</table>")
    if report["context_documents"]:
        out.append("<h2>Related document excerpts <span class='tag'>untrusted context, not used in any figure</span></h2><ul>" +
                   "".join(f"<li><b>{e(c['document'])}</b>{e(' p.%s' % c['page']) if c['page'] else ''}: {e(c['text'])}</li>" for c in report["context_documents"]) + "</ul>")
    return "".join(out) + "</body></html>"


def dumps(report: dict) -> str:
    return json.dumps(report, indent=2, default=str)


def delete_investigations(db, query_filter) -> int:
    """Delete investigations matching ``query_filter`` together with their exported report files and Report rows (no commit)."""
    import report_storage
    rows = db.query(Investigation).filter(query_filter).all()
    for inv in rows:
        ids = list((inv.exports or {}).values())
        for rep in (db.query(Report).filter(Report.id.in_(ids)).all() if ids else []):
            report_storage.delete_report_object(rep.key)
            db.delete(rep)
        db.delete(inv)
    return len(rows)
