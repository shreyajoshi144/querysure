"""Drive QuerySure's real pipeline over the multi-format cases and score the results."""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import math
import os
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from typing import Any, Dict, List, Optional

from ..compare import compare_results
from .cases import ALL_CASES, Case
from .sources import DATASETS, Sources, resolve_data_dir


@contextlib.contextmanager
def _live_provider(sql_holder: Dict[str, Any]):
    """Real provider chain (costs money). Wraps it only to count calls and remember the model's first SQL."""
    import modules.sql_generator as sg
    original = sg._run_provider_chain

    def counting(system_prompt, user_prompt, key):
        sql_holder["calls"] = sql_holder.get("calls", 0) + 1
        try:
            return original(system_prompt, user_prompt, key)
        except sg.ProviderError as exc:
            # Kept so the report can tell "the provider refused the call" (quota, rate limit, auth) from wrong SQL.
            sql_holder["provider_failure"] = str(exc)
            raise

    sg._run_provider_chain = counting
    try:
        yield
    finally:
        sg._run_provider_chain = original


@contextlib.contextmanager
def _stub_provider(sql_holder: Dict[str, Any]):
    """Replace ONLY the network call. Prompting, parsing, validation, repair and verification all run for real.
    Repairs return the same SQL, so any repair request is counted (a correct query should never need one)."""
    import modules.sql_generator as sg
    original = sg._run_provider_chain

    def fake(system_prompt, user_prompt, key):
        sql_holder["calls"] = sql_holder.get("calls", 0) + 1
        reply = {"sql": sql_holder["sql"], "explanation": "eval", "confidence": "High", "complexity": "Moderate"}
        return json.dumps(reply), "groq:eval-stub", []

    sg._run_provider_chain = fake
    try:
        yield
    finally:
        sg._run_provider_chain = original


def _ingest(src: Sources, key: str, workdir: str) -> str:
    """The worker's upload path: SQLite files are validated and used as-is, other formats are converted."""
    from modules.ingestion import ingest_to_sqlite, validate_sqlite
    name, ext = DATASETS[key]
    raw = src.path(key)
    dest = os.path.join(workdir, f"{key}.db")
    if ext in (".db", ".sqlite", ".sqlite3"):
        validate_sqlite(raw)
        shutil.copyfile(raw, dest)
    else:
        ingest_to_sqlite(raw, ext, dest, os.path.splitext(name)[0])
    return dest


def _canon(v: Any) -> Any:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.strftime("%Y-%m-%d")
    if hasattr(v, "item"):
        return _canon(v.item())
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, float):
        return round(v, 9)
    return v


def _same_number(x: Any, y: Any) -> bool:
    if x == y:
        return True
    try:
        return x is not None and y is not None and abs(float(x) - float(y)) < 1e-9
    except (TypeError, ValueError):
        return False


def ingestion_fidelity(src: Sources, key: str, db_path: str) -> Dict[str, Any]:
    """Compare the ingested table(s) with the raw source, column by column (positional, header-normalised)."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if key.startswith(("sqlite_", "db_")):
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            ok = all(con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] == len(src.table(key, t)) for t in tables)
            return {"tables": len(tables), "columns_checked": None, "columns_exact": None, "row_counts_match": ok, "mismatches": []}
        raw = {"csv_financial": src.csv, "xlsx_projects": src.xlsx, "json_countries": src.json_countries}[key]()
        (table,) = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        cur = con.execute(f'SELECT * FROM "{table}"')
        stored = cur.fetchall()
        mism, type_changed = [], []
        exact = 0
        for i, col in enumerate(raw.columns):
            a = [_canon(v) for v in raw[col].tolist()]
            b = [_canon(r[i]) for r in stored]
            if a == b:
                exact += 1
            elif all(_same_number(x, y) for x, y in zip(a, b)):
                exact += 1
                type_changed.append(str(col))       # same values, stored as numbers instead of text
            else:
                diffs = [(x, y) for x, y in zip(a, b) if x != y][:3]
                mism.append({"column": str(col), "examples": [list(map(str, d)) for d in diffs]})
        return {"tables": 1, "columns_checked": len(raw.columns), "columns_exact": exact,
                "row_counts_match": len(raw) == len(stored), "mismatches": mism, "text_stored_as_number": type_changed}
    finally:
        con.close()


def _round2(rows):
    return [tuple(round(v, 2) if isinstance(v, float) else v for v in r) for r in rows]


def _tables_in(sql: str, schema: Dict[str, Any]) -> List[str]:
    import sqlglot
    from sqlglot import exp
    canon = {t.lower(): t for t in (schema.get("tables") or {})}
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:  # noqa: BLE001
        return []
    return sorted({canon[t.name.lower()] for t in tree.find_all(exp.Table) if t.name.lower() in canon})


def _run_one(case: Case, question: str, ctx: Dict[str, Any], expected) -> Dict[str, Any]:
    import modules.sql_generator as sg
    from modules.query_result_engine import execute_query
    from modules.retrieval_engine import retrieve_context
    from modules.sql_validator import validate_sql

    holder = ctx["holder"]
    holder.clear()
    holder["sql"] = ctx.get("sql_override") or case.sql
    schema, semantic, db = ctx["schema"], ctx["semantic"], ctx["db"]
    retrieval = retrieve_context(question, semantic, cache_key=f"eval:{case.dataset}")
    # The model only sees the retrieved schema subset: every table the correct SQL needs must be in it.
    needed = _tables_in(case.sql, schema)
    got_tables = {t.lower() for t in (retrieval or {}).get("relevant_tables", [])}
    retrieval_ok = all(t.lower() in got_tables for t in needed)
    if ctx.get("live"):
        import config
        gen = sg.generate_sql(question=question, schema=schema, api_key=config.GROQ_API_KEY, semantic=semantic,
                              retrieval=retrieval, db_path=db)
        if not gen.get("success", True):
            return {"provider_unavailable": bool(holder.get("provider_failure")) and not gen.get("sql"),
                    "provider_failure": holder.get("provider_failure"),
                    "accepted": False, "rejection": ["generation failed: " + str(gen.get("error_type"))], "executed": False,
                    "exec_error": None, "correct": False, "compare_reason": "generation failed", "semantic_issues": [],
                    "verification": None, "repairs_requested": 0, "clean": False, "false_alarm": False, "declined": False,
                    "rows_preview": [], "sql": "", "provider": None, "cost_cents": 0.0}
    else:
        gen = sg.generate_sql(question=question, schema=schema, api_key="eval", semantic=semantic, retrieval=retrieval, db_path=db)
    final_sql = gen.get("sql") or ""
    val = validate_sql(final_sql, schema, semantic)   # what /api/execute-sql re-checks before running
    ex = execute_query(final_sql, db, parameterize=True) if val.get("is_valid") else {"success": False, "rows": [], "columns": []}
    cand = [tuple(r.get(c) for c in ex.get("columns", [])) for r in ex.get("rows", [])]
    cmp = compare_results(expected, cand, ordered=case.ordered) if ex.get("success") else None
    # Semantic correctness: the same answer presented differently -- extra columns allowed, numbers compared at the
    # 2 decimals a person reads (a model's ROUND(x, 2) is not a wrong answer). Exact correctness stays the headline.
    sem_cmp = compare_results(_round2(expected), _round2(cand), ordered=case.ordered, allow_extra_columns=True) \
        if ex.get("success") else None
    sem_issues = [i.get("message") for i in (gen.get("semantic_validation") or {}).get("issues", [])]
    verification = (gen.get("verification") or {}).get("status")
    repairs = max(0, holder.get("calls", 1) - 1) if not ctx.get("live") else int(gen.get("repair_attempts") or 0)
    correct = bool(cmp and cmp.match)
    # With candidate SQL supplied, any repair request is a false alarm. Live, a repair that fixes the model's first SQL is
    # the pipeline working, so only warnings left on the final answer count against it (repairs are reported separately).
    clean = not sem_issues and verification not in ("mismatch", "suspicious_empty") and (repairs == 0 or bool(ctx.get("live")))
    return {
        "accepted": bool(val.get("is_valid")),
        "rejection": [i.get("message") for i in val.get("issues", [])][:3] if not val.get("is_valid") else [],
        "executed": bool(ex.get("success")),
        "exec_error": ex.get("error"),
        "correct": correct,
        "semantic_correct": bool(correct or (sem_cmp and sem_cmp.match)),
        "compare_reason": (cmp.reason if cmp else "not executed"),
        "semantic_issues": sem_issues,
        "verification": verification,
        "repairs_requested": repairs,
        "clean": clean,
        "false_alarm": correct and not clean,
        "rows_preview": [list(map(str, r)) for r in cand[:3]],
        "declined": not final_sql.strip(),
        "retrieval_ok": retrieval_ok,
        "deterministic_fixes": list(gen.get("deterministic_fixes") or []),
        "retrieval_missing": sorted(t for t in needed if t.lower() not in got_tables),
        "sql": final_sql if ctx.get("live") else None,
        "provider": gen.get("provider_used") if ctx.get("live") else None,
        "cost_cents": float(((gen.get("usage") or {}).get("cost_cents")) or 0.0) if ctx.get("live") else 0.0,
    }


def run(data_dir: Optional[str] = None, only: Optional[str] = None, holdout: bool = False, live: bool = False,
        delay_seconds: float = 0.0, replay: Optional[Dict[tuple, str]] = None) -> Dict[str, Any]:
    """``replay`` maps (case id, variant) to SQL a model wrote in an earlier live run: that SQL goes through today's pipeline
    with no LLM call (repairs get the same SQL back). Runs the provider never answered are left out."""
    src = Sources(resolve_data_dir(data_dir))
    results: List[Dict[str, Any]] = []
    fidelity: Dict[str, Any] = {}
    skipped: List[str] = []
    workdir = tempfile.mkdtemp(prefix="qs-mf-eval-")
    holder: Dict[str, Any] = {}
    try:
        from modules.schema_engine import load_database
        from modules.semantic_layer import build_semantic_layer
        with (_live_provider(holder) if live else _stub_provider(holder)):
            for key in DATASETS:
                if not src.available(key):
                    skipped.append(key)
                    continue
                pool = ALL_CASES
                if holdout:
                    from .holdout import HOLDOUT_CASES
                    pool = HOLDOUT_CASES
                cases = [c for c in pool if c.dataset == key and (not only or c.id.startswith(only))]
                if not cases:
                    continue
                db = _ingest(src, key, workdir)
                fidelity[key] = ingestion_fidelity(src, key, db)
                schema = load_database(db)
                ctx = {"holder": holder, "db": db, "schema": schema, "semantic": build_semantic_layer(schema), "live": live}
                for case in cases:
                    try:
                        expected = case.expected(src)
                    except Exception as exc:  # noqa: BLE001 - a broken expectation is reported, never hidden
                        results.append({"id": case.id, "dataset": key, "lang": "en", "question": case.question,
                                        "category": case.category, "correct": False, "error": f"expected() failed: {exc}"})
                        continue
                    for lang, q in [("en", case.question)] + list(case.variants.items()):
                        ctx["sql_override"] = None
                        if replay is not None:
                            ctx["sql_override"] = replay.get((case.id, lang))
                            if not ctx["sql_override"]:
                                continue
                        if live and delay_seconds:
                            import time
                            time.sleep(delay_seconds)      # stay under provider rate limits
                        try:
                            r = _run_one(case, q, ctx, expected)
                        except Exception as exc:  # noqa: BLE001
                            r = {"correct": False, "clean": False, "error": f"{type(exc).__name__}: {exc}"}
                        results.append({"id": case.id, "dataset": key, "lang": lang, "question": q, "category": case.category, **r})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return {"generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), "data_dir": src.data_dir,
            "mode": "live" if live else ("replay" if replay is not None else "candidate_sql"), "holdout": holdout,
            "skipped_datasets": skipped, "ingestion_fidelity": fidelity, "metrics": metrics(results), "results": results}


def run_negatives(data_dir: Optional[str] = None) -> Dict[str, Any]:
    """Score deliberately wrong SQL: a case is detected when the pipeline rejects or flags it in any way."""
    from modules.schema_engine import load_database
    from modules.semantic_layer import build_semantic_layer
    from .negatives import NEGATIVES
    src = Sources(resolve_data_dir(data_dir))
    workdir = tempfile.mkdtemp(prefix="qs-mf-neg-")
    holder: Dict[str, Any] = {}
    out: List[Dict[str, Any]] = []
    try:
        with _stub_provider(holder):
            ctxs: Dict[str, Dict[str, Any]] = {}
            for n in NEGATIVES:
                if not src.available(n.dataset):
                    continue
                if n.dataset not in ctxs:
                    db = _ingest(src, n.dataset, workdir)
                    schema = load_database(db)
                    ctxs[n.dataset] = {"holder": holder, "db": db, "schema": schema, "semantic": build_semantic_layer(schema)}
                # a tuned case asking the same question supplies the independent answer a correction must reproduce
                same = next((c for c in ALL_CASES if c.dataset == n.dataset and c.question == n.question), None)
                expected = same.expected(src) if same else []
                case = Case(n.id, n.dataset, "negative", n.question, n.sql, lambda s: expected,
                            ordered=same.ordered if same else False)
                r = _run_one(case, n.question, ctxs[n.dataset], expected)
                corrected = bool(same and r["deterministic_fixes"] and r["correct"])
                detected = (not r["accepted"]) or bool(r["semantic_issues"]) or r["verification"] in ("mismatch", "suspicious_empty") \
                    or r["repairs_requested"] > 0 or corrected
                out.append({"id": n.id, "dataset": n.dataset, "mistake": n.mistake, "detectable": n.detectable, "detected": detected,
                            "corrected": corrected,
                            "signals": r["rejection"] + r["semantic_issues"] + ([f"verification={r['verification']}"]
                                                                                if r["verification"] in ("mismatch", "suspicious_empty") else [])
                            + ([f"corrected deterministically ({'; '.join(r['deterministic_fixes'])}); result matches the independent answer"]
                               if corrected else [])})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    det = [r for r in out if r["detectable"]]
    return {"cases": len(out), "detectable": len(det), "detected_of_detectable": sum(r["detected"] for r in det),
            "corrected": sum(r["corrected"] for r in out),
            "detected_of_hard": sum(r["detected"] for r in out if not r["detectable"]), "hard": len(out) - len(det), "results": out}


def _rate(n: int, d: int) -> Optional[float]:
    return round(100.0 * n / d, 2) if d else None


def metrics(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    def summary(rs):
        n = len(rs)
        answered = [r for r in rs if not r.get("provider_unavailable")]
        return {"cases": n,
                "exact_result_correct_pct": _rate(sum(1 for r in rs if r.get("correct")), n),
                "semantic_correct_pct": _rate(sum(1 for r in rs if r.get("semantic_correct")), n),
                "accepted_pct": _rate(sum(1 for r in rs if r.get("accepted")), n),
                "clean_pct": _rate(sum(1 for r in rs if r.get("clean")), n),
                "false_alarm_pct": _rate(sum(1 for r in rs if r.get("false_alarm")), n),
                "rejected": sum(1 for r in rs if r.get("accepted") is False),
                "retrieval_recall_pct": _rate(sum(1 for r in rs if r.get("retrieval_ok")), n),
                "wrong_or_failed": sum(1 for r in rs if not r.get("correct")),
                "verified_match": sum(1 for r in rs if r.get("verification") == "verified_match"),
                "verification_mismatch": sum(1 for r in rs if r.get("verification") == "mismatch"),
                "declined": sum(1 for r in rs if r.get("declined")),
                "repaired_and_correct": sum(1 for r in rs if r.get("repairs_requested") and r.get("correct")),
                "estimated_cost_usd": round(sum(r.get("cost_cents") or 0.0 for r in rs) / 100.0, 4),
                # Live runs only: calls the provider refused (quota, rate limit, auth) are not model answers, so the
                # accuracy over the runs the model actually answered is reported next to the headline figure.
                "provider_unavailable": sum(1 for r in rs if r.get("provider_unavailable")),
                "answered_runs": len(answered),
                "exact_correct_of_answered_pct": _rate(sum(1 for r in answered if r.get("correct")), len(answered)),
                "semantic_correct_of_answered_pct": _rate(sum(1 for r in answered if r.get("semantic_correct")), len(answered))}
    by_ds, by_lang = defaultdict(list), defaultdict(list)
    for r in results:
        by_ds[r["dataset"]].append(r)
        by_lang[r["lang"]].append(r)
    return {"overall": summary(results), "by_dataset": {k: summary(v) for k, v in by_ds.items()},
            "by_language": {k: summary(v) for k, v in sorted(by_lang.items())}}


def render(report: Dict[str, Any]) -> str:
    m = report["metrics"]
    o = m["overall"]
    title = ("QuerySure multi-format evaluation -- LIVE: SQL written by the configured LLM provider chain"
             if report.get("mode") == "live" else
             "QuerySure multi-format evaluation -- REPLAY: SQL recorded from an earlier live run, re-checked by today's pipeline"
             if report.get("mode") == "replay" else
             "QuerySure multi-format evaluation (deterministic pipeline; candidate SQL supplied, no LLM called)")
    lines = [title, f"set: {'held-out' if report.get('holdout') else 'tuned'}",
             f"data: {report['data_dir']}  skipped datasets: {report['skipped_datasets'] or 'none'}", "",
             f"cases (incl. variants): {o['cases']}",
             f"exact-result correct:   {o['exact_result_correct_pct']}%",
             f"semantically correct:   {o['semantic_correct_pct']}%  (extra columns / 2-decimal rounding tolerated)",
             f"accepted by validator:  {o['accepted_pct']}%",
             f"clean (no false alarm): {o['clean_pct']}%   false alarms on correct results: {o['false_alarm_pct']}%",
             f"verified_match: {o['verified_match']}   verification mismatch: {o['verification_mismatch']}",
             f"retrieval recall (all needed tables shown to the model): {o['retrieval_recall_pct']}%",
             f"declined (no SQL): {o['declined']}   estimated LLM cost: ${o['estimated_cost_usd']}"
             + (f"   correct after a repair round: {o['repaired_and_correct']}" if report.get("mode") == "live" else ""),
             *([f"provider unavailable (quota / rate limit / auth): {o['provider_unavailable']} of {o['cases']}; "
                f"of the {o['answered_runs']} answered: exact {o['exact_correct_of_answered_pct']}%, "
                f"semantic {o['semantic_correct_of_answered_pct']}%"] if report.get("mode") == "live" else []), "",
             "by dataset:"]
    for k, v in m["by_dataset"].items():
        lines.append(f"  {k:18s} n={v['cases']:3d} correct={v['exact_result_correct_pct']}% semantic={v['semantic_correct_pct']}% clean={v['clean_pct']}% rejected={v['rejected']}"
                     f" retrieval_recall={v['retrieval_recall_pct']}%")
    lines.append("by language/variant:")
    for k, v in m["by_language"].items():
        lines.append(f"  {k:12s} n={v['cases']:3d} correct={v['exact_result_correct_pct']}% semantic={v['semantic_correct_pct']}% clean={v['clean_pct']}%")
    lines.append("ingestion fidelity:")
    for k, v in report["ingestion_fidelity"].items():
        lines.append(f"  {k:18s} rows_match={v['row_counts_match']} columns_exact={v['columns_exact']}/{v['columns_checked']}"
                     + (f" mismatches={[x['column'] for x in v['mismatches']]}" if v["mismatches"] else "")
                     + (f" text_stored_as_number={v['text_stored_as_number']}" if v.get("text_stored_as_number") else ""))
    bad = [r for r in report["results"] if not r.get("correct") or not r.get("clean")]
    if bad:
        lines.append("")
        lines.append(f"not correct or not clean ({len(bad)}):")
        for r in bad:
            why = r.get("error") or "; ".join(filter(None, [
                "REJECTED: " + "; ".join(r.get("rejection") or []) if r.get("accepted") is False else "",
                "" if r.get("correct") else f"WRONG: {r.get('compare_reason')}",
                ("semantic: " + " | ".join(r.get("semantic_issues") or [])) if r.get("semantic_issues") else "",
                f"verification={r.get('verification')}" if r.get("verification") in ("mismatch", "suspicious_empty") else "",
                f"repairs={r.get('repairs_requested')}" if r.get("repairs_requested") else ""]))
            lines.append(f"  [{r['id']}/{r['lang']}] {r['question'][:70]} -> {why[:300]}")
    return "\n".join(lines)
