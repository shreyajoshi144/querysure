"""Configurable SLOs (Sprint 4), evaluated from the aggregate metrics store. Targets come from config (SLO_* env vars).

Objectives
  availability     share of non-5xx responses (static assets excluded). Also error-budget remaining and multi-window burn rate.
  api_latency_p95  interactive API p95 (routes that run AI / uploads / reports are excluded -- they have their own SLOs)
  api_latency_p99  same, p99
  ai_success       share of generate-sql requests that produced SQL (budget / kill-switch refusals are not provider failures)
  ai_latency_p95   generate-sql provider latency p95
  queue_wait_p95   background job wait (enqueue -> start) p95
  job_success      share of background jobs that succeeded

An objective with fewer than SLO_MIN_SAMPLES observations reports ``no_data`` rather than a misleading pass/fail.
Percentiles are histogram-bucket estimates (see store.py).
"""
from __future__ import annotations

from typing import Optional

import config
from observability import metrics as m
from observability.store import MetricsStore, Snapshot, get_store, quantile_from_buckets

LONG_WINDOW_MINUTES = 60
SHORT_WINDOW_MINUTES = 5
# Routes whose latency is dominated by LLM / file / report work; excluded from the interactive-API latency objectives.
SLOW_ROUTES = frozenset({"/api/generate-sql", "/api/connect/upload", "/api/generate-report", "/api/generate-insights",
                         "/api/analyze-reliability", "/api/execute-sql"})
NON_API_ROUTES = frozenset({"static", "assets", "unmatched"})


def _http_totals(snap: Snapshot) -> tuple:
    total = errors = 0.0
    for (name, labels), v in snap.counters.items():
        if name != m.HTTP_REQUESTS:
            continue
        d = dict(labels)
        if d.get("route") in NON_API_ROUTES:
            continue
        total += v
        if d.get("status_class") == "5xx":
            errors += v
    return total, errors


def _latency_excluding(snap: Snapshot, q: float) -> tuple:
    merged = [0.0] * (len(next(iter(snap.hists.values()))["b"]) if snap.hists else 0)
    n = 0.0
    for (name, labels), h in snap.hists.items():
        if name != m.HTTP_LATENCY or dict(labels).get("route") in SLOW_ROUTES | NON_API_ROUTES:
            continue
        merged = [a + b for a, b in zip(merged, h["b"])]
        n += h["n"]
    return (quantile_from_buckets(merged, q) if n else None), n


def _status(ok: Optional[bool], samples: float) -> str:
    if samples < config.SLO_MIN_SAMPLES or ok is None:
        return "no_data"
    return "ok" if ok else "breach"


def burn_rate(snap: Snapshot, target_pct: float) -> Optional[float]:
    total, errors = _http_totals(snap)
    if total < config.SLO_MIN_SAMPLES:
        return None
    allowed = max(1e-9, 1.0 - target_pct / 100.0)
    return round((errors / total) / allowed, 2)


def definitions() -> list:
    return [
        {"name": "availability", "target": config.SLO_AVAILABILITY_TARGET_PCT, "unit": "%"},
        {"name": "api_latency_p95", "target": config.SLO_API_P95_MS, "unit": "ms"},
        {"name": "api_latency_p99", "target": config.SLO_API_P99_MS, "unit": "ms"},
        {"name": "ai_success", "target": config.SLO_AI_SUCCESS_TARGET_PCT, "unit": "%"},
        {"name": "ai_latency_p95", "target": config.SLO_AI_P95_MS, "unit": "ms"},
        {"name": "queue_wait_p95", "target": config.SLO_QUEUE_WAIT_P95_MS, "unit": "ms"},
        {"name": "job_success", "target": config.SLO_JOB_SUCCESS_TARGET_PCT, "unit": "%"},
    ]


def evaluate(store: Optional[MetricsStore] = None, window_minutes: Optional[int] = None) -> dict:
    store = store or get_store()
    window = window_minutes or config.SLO_WINDOW_MINUTES
    snap = store.snapshot(window)
    long_snap = store.snapshot(LONG_WINDOW_MINUTES)
    short_snap = store.snapshot(SHORT_WINDOW_MINUTES)
    out = []

    total, errors = _http_totals(snap)
    avail = None if total == 0 else 100.0 * (1 - errors / total)
    allowed_err = max(1e-9, 1 - config.SLO_AVAILABILITY_TARGET_PCT / 100.0)
    budget_left = None if total == 0 else round(max(0.0, 100.0 * (1 - (errors / total) / allowed_err)), 1)
    out.append({"name": "availability", "target": config.SLO_AVAILABILITY_TARGET_PCT, "unit": "%",
                "sli": None if avail is None else round(avail, 3), "samples": int(total),
                "status": _status(None if avail is None else avail >= config.SLO_AVAILABILITY_TARGET_PCT, total),
                "error_budget_remaining_pct": budget_left,
                "burn_rate_short": burn_rate(short_snap, config.SLO_AVAILABILITY_TARGET_PCT),
                "burn_rate_long": burn_rate(long_snap, config.SLO_AVAILABILITY_TARGET_PCT)})

    for name, q, target in (("api_latency_p95", 0.95, config.SLO_API_P95_MS), ("api_latency_p99", 0.99, config.SLO_API_P99_MS)):
        value, n = _latency_excluding(snap, q)
        out.append({"name": name, "target": target, "unit": "ms", "sli": None if value is None else round(value, 1),
                    "samples": int(n), "status": _status(None if value is None else value <= target, n)})

    ok = snap.counter(m.AI_REQUESTS, outcome="success")
    bad = snap.counter(m.AI_REQUESTS, outcome="failure")
    ai_total = ok + bad
    ai_rate = None if ai_total == 0 else 100.0 * ok / ai_total
    out.append({"name": "ai_success", "target": config.SLO_AI_SUCCESS_TARGET_PCT, "unit": "%",
                "sli": None if ai_rate is None else round(ai_rate, 2), "samples": int(ai_total),
                "status": _status(None if ai_rate is None else ai_rate >= config.SLO_AI_SUCCESS_TARGET_PCT, ai_total)})

    ai_p95 = snap.quantile(m.AI_LATENCY, 0.95)
    ai_n = snap.hist_count(m.AI_LATENCY)
    out.append({"name": "ai_latency_p95", "target": config.SLO_AI_P95_MS, "unit": "ms",
                "sli": None if ai_p95 is None else round(ai_p95, 1), "samples": int(ai_n),
                "status": _status(None if ai_p95 is None else ai_p95 <= config.SLO_AI_P95_MS, ai_n)})

    qw = snap.quantile(m.JOB_QUEUE_WAIT, 0.95)
    qn = snap.hist_count(m.JOB_QUEUE_WAIT)
    out.append({"name": "queue_wait_p95", "target": config.SLO_QUEUE_WAIT_P95_MS, "unit": "ms",
                "sli": None if qw is None else round(qw, 1), "samples": int(qn),
                "status": _status(None if qw is None else qw <= config.SLO_QUEUE_WAIT_P95_MS, qn)})

    jok = snap.counter(m.JOBS, outcome="succeeded")
    jbad = snap.counter(m.JOBS, outcome="failed")
    jt = jok + jbad
    jrate = None if jt == 0 else 100.0 * jok / jt
    out.append({"name": "job_success", "target": config.SLO_JOB_SUCCESS_TARGET_PCT, "unit": "%",
                "sli": None if jrate is None else round(jrate, 2), "samples": int(jt),
                "status": _status(None if jrate is None else jrate >= config.SLO_JOB_SUCCESS_TARGET_PCT, jt)})

    return {"window_minutes": window, "objectives": out,
            "breaches": [o["name"] for o in out if o["status"] == "breach"]}
