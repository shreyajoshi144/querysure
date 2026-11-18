"""Data-quality profiling and anomaly checks for ingested datasets (Sprint 6).

After every successful run, ``record`` profiles the new curated version (row counts, null shares, distinct counts, numeric and date
ranges), compares it with the previous version and with the recent history of row counts, and stores deterministic findings.
No model is involved, so every finding is reproducible from the numbers it carries.

Privacy: the profile holds names, counts and numeric/date summaries only -- no text values. A failure here is logged and never
fails or rolls back the import it describes.
"""
from __future__ import annotations

import logging
import math
import sqlite3
from statistics import mean, pstdev
from typing import List, Optional

import config
import storage
from db import Dataset, DatasetQualityReport, IngestionRun

logger = logging.getLogger("querysure.quality")
MAX_TABLES, MAX_COLUMNS = 50, 200
_NUMERIC = ("INT", "REAL", "FLOA", "DOUB", "NUMERIC", "DECIMAL")
_RANK = {"error": 2, "warn": 1, "info": 0}


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _kind(declared: str) -> str:
    d = (declared or "").upper()
    if any(t in d for t in ("DATE", "TIME")):
        return "temporal"
    if any(t in d for t in _NUMERIC):
        return "numeric"
    return "text"


def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else None


def profile_file(path: str) -> dict:
    """{table: {rows, columns: {name: {type, kind, non_null, nulls, null_pct, distinct?, min?, max?, mean?}}}} of a curated file."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND substr(name, 1, 4) <> '_qs_' ORDER BY name LIMIT ?",
            (MAX_TABLES,))]
        out = {}
        for t in tables:
            rows = con.execute(f"SELECT COUNT(*) FROM {_q(t)}").fetchone()[0]
            cols = [(c[1], c[2] or "") for c in con.execute(f"PRAGMA table_info({_q(t)})")][:MAX_COLUMNS]
            exprs, plan = [], []
            for name, declared in cols:
                kind, c = _kind(declared), _q(name)
                parts = [f"COUNT({c})"]
                if rows <= config.QUALITY_DISTINCT_MAX_ROWS:
                    parts.append(f"COUNT(DISTINCT {c})")
                if kind in ("numeric", "temporal"):
                    parts += [f"MIN({c})", f"MAX({c})"]
                if kind == "numeric":
                    parts.append(f"AVG({c})")
                plan.append((name, declared, kind, len(parts)))
                exprs += parts
            vals = con.execute(f"SELECT {', '.join(exprs)} FROM {_q(t)}").fetchone() if exprs else ()
            i, columns = 0, {}
            for name, declared, kind, n in plan:
                chunk = vals[i:i + n]
                i += n
                non_null = int(chunk[0] or 0)
                info = {"type": declared.upper() or "TEXT", "kind": kind, "non_null": non_null, "nulls": rows - non_null,
                        "null_pct": round(100.0 * (rows - non_null) / rows, 2) if rows else 0.0}
                k = 1
                if rows <= config.QUALITY_DISTINCT_MAX_ROWS:
                    info["distinct"] = int(chunk[k] or 0)
                    k += 1
                if kind == "numeric":
                    for key, v in zip(("min", "max", "mean"), chunk[k:k + 3]):
                        if _num(v) is not None:
                            info[key] = round(float(v), 6)
                elif kind == "temporal":
                    for key, v in zip(("min", "max"), chunk[k:k + 2]):
                        if v is not None:
                            info[key] = str(v)[:32]
                columns[name] = info
            out[t] = {"rows": int(rows), "columns": columns}
        return out
    finally:
        con.close()


def _f(rule, severity, message, table=None, column=None, value=None, baseline=None) -> dict:
    return {"rule": rule, "severity": severity, "table": table, "column": column, "message": message, "value": value, "baseline": baseline}


def evaluate(profile: dict, previous: Optional[dict], history_rows: List[dict], run: Optional[IngestionRun]) -> List[dict]:
    """Findings, most severe first. ``history_rows`` are earlier per-table row totals ({table: rows}) used for the trend check."""
    out: List[dict] = []
    for t, cur in profile.items():
        rows, prev = cur["rows"], (previous or {}).get(t)
        if rows == 0:
            out.append(_f("empty_table", "error" if prev and prev["rows"] else "warn", f"Table '{t}' has no rows.", t, value=0,
                          baseline=prev["rows"] if prev else None))
        if prev and prev["rows"] > 0 and rows > 0:
            drop = 100.0 * (prev["rows"] - rows) / prev["rows"]
            if drop >= config.QUALITY_ROW_DROP_PCT:
                out.append(_f("row_count_drop", "warn", f"Table '{t}' lost {drop:.0f}% of its rows since the previous version.", t,
                              value=rows, baseline=prev["rows"]))
            elif rows >= 4 * prev["rows"]:
                out.append(_f("row_count_surge", "info", f"Table '{t}' grew to {rows / prev['rows']:.1f}x its previous size.", t,
                              value=rows, baseline=prev["rows"]))
        series = [h[t] for h in history_rows if t in h]
        if len(series) >= 4:
            m, sd = mean(series), pstdev(series)
            if sd > 0 and abs(rows - m) > 3 * sd and not any(f["table"] == t and f["rule"] == "row_count_drop" for f in out):
                out.append(_f("row_count_anomaly", "warn", f"Table '{t}' has {rows} rows; recent versions averaged {m:.0f} (more than 3 standard deviations away).",
                              t, value=rows, baseline=round(m, 1)))
        pcols = (prev or {}).get("columns", {})
        for name, c in cur["columns"].items():
            before = pcols.get(name)
            if rows > 0 and c["non_null"] == 0:
                out.append(_f("all_null_column", "warn", f"Column '{t}.{name}' is empty in every row.", t, name, value=100.0,
                              baseline=before["null_pct"] if before else None))
            elif before and rows > 0 and prev["rows"] > 0 and c["null_pct"] - before["null_pct"] >= config.QUALITY_NULL_SPIKE_POINTS:
                out.append(_f("null_spike", "warn", f"Column '{t}.{name}' is now {c['null_pct']:.0f}% empty (was {before['null_pct']:.0f}%).", t, name,
                              value=c["null_pct"], baseline=before["null_pct"]))
            if before and before["kind"] != c["kind"]:
                out.append(_f("type_changed", "warn", f"Column '{t}.{name}' changed type from {before['type']} to {c['type']}.", t, name,
                              value=c["type"], baseline=before["type"]))
            if (before and c["kind"] == "numeric" and before["kind"] == "numeric" and "mean" in c and "mean" in before
                    and rows >= 30 and prev["rows"] >= 30 and before["mean"] != 0):
                shift = 100.0 * abs(c["mean"] - before["mean"]) / abs(before["mean"])
                if shift >= config.QUALITY_NUMERIC_SHIFT_PCT:
                    out.append(_f("numeric_shift", "warn", f"The average of '{t}.{name}' moved {shift:.0f}% ({before['mean']:g} to {c['mean']:g}).", t, name,
                                  value=c["mean"], baseline=before["mean"]))
        if prev:
            for name in pcols:
                if name not in cur["columns"]:
                    out.append(_f("column_removed", "warn", f"Column '{t}.{name}' is no longer present.", t, name))
            for name in cur["columns"]:
                if name not in pcols:
                    out.append(_f("column_added", "info", f"Column '{t}.{name}' is new.", t, name))
    if previous:
        for t in previous:
            if t not in profile:
                out.append(_f("table_removed", "warn", f"Table '{t}' is no longer present.", t))
    if run is not None and run.records_read:
        rej = 100.0 * (run.records_rejected or 0) / run.records_read
        if config.QUALITY_REJECT_ERROR_PCT and rej >= config.QUALITY_REJECT_ERROR_PCT:
            out.append(_f("high_reject_rate", "error", f"{rej:.1f}% of the rows read were rejected.", value=round(rej, 2), baseline=config.QUALITY_REJECT_ERROR_PCT))
        elif config.QUALITY_REJECT_WARN_PCT and rej >= config.QUALITY_REJECT_WARN_PCT:
            out.append(_f("high_reject_rate", "warn", f"{rej:.1f}% of the rows read were rejected.", value=round(rej, 2), baseline=config.QUALITY_REJECT_WARN_PCT))
        dup = 100.0 * (run.duplicate_rows or 0) / run.records_read
        if config.QUALITY_DUPLICATE_WARN_PCT and dup >= config.QUALITY_DUPLICATE_WARN_PCT:
            out.append(_f("duplicate_rows", "warn", f"{dup:.1f}% of the rows are exact duplicates (kept).", value=round(dup, 2), baseline=config.QUALITY_DUPLICATE_WARN_PCT))
    return sorted(out, key=lambda f: -_RANK[f["severity"]])


def overall(findings: List[dict]) -> str:
    worst = max((_RANK[f["severity"]] for f in findings), default=0)
    return "error" if worst == 2 else "warn" if worst == 1 else "ok"


def record(db, ds: Dataset, run: IngestionRun) -> Optional[DatasetQualityReport]:
    """Profile the version ``run`` just published and store the report. Never raises."""
    if not config.QUALITY_ENABLED or run.status != "succeeded":
        return None
    try:
        path = storage.ensure_local_workspace(ds.storage_ref)
        profile = profile_file(path)
        recent = (db.query(DatasetQualityReport).filter(DatasetQualityReport.dataset_id == ds.id, DatasetQualityReport.run_id != run.id)
                  .order_by(DatasetQualityReport.created_at.desc()).limit(10).all())
        previous = recent[0].profile if recent else None
        history = [{t: v["rows"] for t, v in (r.profile or {}).items()} for r in recent]
        findings = evaluate(profile, previous, history, run)
        report = DatasetQualityReport(dataset_id=ds.id, workspace_id=ds.workspace_id, run_id=run.id, dataset_version=run.dataset_version,
                                      status=overall(findings), profile=profile, findings=findings)
        db.add(report)
        db.commit()
        old = (db.query(DatasetQualityReport.id).filter(DatasetQualityReport.dataset_id == ds.id)
               .order_by(DatasetQualityReport.created_at.desc()).offset(config.QUALITY_REPORTS_KEEP).all())
        if old:
            db.query(DatasetQualityReport).filter(DatasetQualityReport.id.in_([o[0] for o in old])).delete(synchronize_session=False)
            db.commit()
        return report
    except Exception as exc:  # noqa: BLE001 - quality must never undo an import
        db.rollback()
        logger.warning("quality report failed: %s", type(exc).__name__)
        return None


def latest_status(db, dataset_ids: List[str]) -> dict:
    """{dataset_id: {status, findings, run_id}} of the newest report per dataset, for the dataset list."""
    if not dataset_ids:
        return {}
    out = {}
    for r in (db.query(DatasetQualityReport).filter(DatasetQualityReport.dataset_id.in_(dataset_ids))
              .order_by(DatasetQualityReport.created_at.desc()).all()):
        if r.dataset_id not in out:
            out[r.dataset_id] = {"status": r.status, "findings": len(r.findings or []), "run_id": r.run_id}
    return out
