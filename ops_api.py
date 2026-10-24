"""Admin / ops API (Sprint 4): AGGREGATE SYSTEM METRICS ONLY.

Privacy contract (enforced by construction and by tests/test_sprint4_ops_privacy.py):
  * This module never imports or queries PipelineSession, Report, Job payloads, UsageDaily rows of a user, AIInvocation rows,
    or storage. It reads only (a) the aggregate metrics store, (b) process gauges, (c) queue/Redis/DB-pool counters,
    (d) ops_events / ops_incidents, whose ``detail`` is allow-list sanitised (observability/events.py), and (e) error
    fingerprints (type + code location, never a message).
  * Therefore an administrator cannot reach prompts, datasets, files, SQL, results, reports, raw LLM context, user ids or
    e-mail addresses through this surface. Admin status grants NO access to the user-facing data routes either: those remain
    owner-scoped (``PipelineSession.user_id == current_user.id``) exactly as before.
  * Access: a signed-in, verified account whose e-mail is in ADMIN_EMAILS (default: empty => nobody). The Prometheus endpoint
    additionally accepts OPS_METRICS_TOKEN (constant-time compare) for scrapers.
"""
from __future__ import annotations

import hmac
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.orm import Session as OrmSession

import config
import storage
from auth import get_current_user
from db import User, get_db
from observability import alerts, errors, events, gauges, health, metrics as m, slo
from observability.store import get_store, iter_prom_lines

logger = logging.getLogger("querysure.ops")
router = APIRouter(prefix="/api/admin/ops", tags=["ops"])


def _deny(code: int = 403):
    return JSONResponse(status_code=code, content={"success": False, "error": "Not authorized.", "error_code": "FORBIDDEN"})


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    from fastapi import HTTPException
    email = (current_user.email or "").strip().lower()
    if not current_user.email_verified or email not in config.ADMIN_EMAILS:
        # Log the pseudonymous reference only -- never the e-mail.
        logger.warning("ops access denied", extra={"event": "ops_access_denied"})
        raise HTTPException(status_code=403, detail="Not authorized.")
    return current_user


def _window(minutes: Optional[int]) -> int:
    return max(1, min(int(minutes or 15), 360))


def _traffic(snap) -> dict:
    by_class = snap.counter_by(m.HTTP_REQUESTS, "status_class")
    total = sum(by_class.values())
    errors_5xx = by_class.get("5xx", 0.0)
    minutes = max(1, snap.window_minutes)
    routes = []
    for route in sorted(snap.hist_label_values(m.HTTP_LATENCY, "route")):
        summ = snap.latency_summary(m.HTTP_LATENCY, route=route)
        per_route = snap.counter_by(m.HTTP_REQUESTS, "status_class", route=route)
        routes.append({"route": route, **summ, "requests": int(sum(per_route.values())),
                       "errors_5xx": int(per_route.get("5xx", 0)), "errors_4xx": int(per_route.get("4xx", 0))})
    routes.sort(key=lambda r: -r["requests"])
    return {"requests": int(total), "rps": round(total / (minutes * 60.0), 3),
            "by_status_class": {k: int(v) for k, v in sorted(by_class.items())},
            "error_rate_5xx_pct": round(100.0 * errors_5xx / total, 3) if total else 0.0,
            "latency": snap.latency_summary(m.HTTP_LATENCY), "routes": routes[:25],
            "rate_limited": int(snap.counter(m.RATE_LIMITED)), "rate_limited_by_route": {k: int(v) for k, v in snap.counter_by(m.RATE_LIMITED, "route").items()},
            "db_busy": int(snap.counter(m.DB_BUSY))}


def _ai(snap) -> dict:
    providers = {}
    # A provider that only ever FAILED (the "provider is down" case) has no success/latency series: include it via its failure and
    # circuit-skip counters too, otherwise its failure reasons would be invisible exactly when they matter.
    names = (set(snap.counter_by(m.AI_REQUESTS, "provider")) | set(snap.hist_label_values(m.AI_LATENCY, "provider"))
             | set(snap.counter_by(m.AI_PROVIDER_FAILURES, "provider")) | set(snap.counter_by(m.AI_CIRCUIT_SKIPS, "provider")))
    for provider in sorted(names):
        ok = snap.counter(m.AI_REQUESTS, provider=provider, outcome="success")
        bad = snap.counter(m.AI_REQUESTS, provider=provider, outcome="failure")
        providers[provider] = {"success": int(ok), "failure": int(bad), "latency": snap.latency_summary(m.AI_LATENCY, provider=provider),
                               "input_tokens": int(snap.counter(m.AI_TOKENS, provider=provider, direction="input")),
                               "output_tokens": int(snap.counter(m.AI_TOKENS, provider=provider, direction="output")),
                               "cost_usd": round(snap.counter(m.AI_COST, provider=provider) / 100.0, 4),
                               "circuit_skips": int(snap.counter(m.AI_CIRCUIT_SKIPS, provider=provider)),
                               "failure_reasons": {k: int(v) for k, v in snap.counter_by(m.AI_PROVIDER_FAILURES, "reason", provider=provider).items()}}
    ok = snap.counter(m.AI_REQUESTS, outcome="success")
    bad = snap.counter(m.AI_REQUESTS, outcome="failure")
    return {"requests": int(ok + bad), "success": int(ok), "failure": int(bad),
            "success_rate_pct": round(100.0 * ok / (ok + bad), 2) if ok + bad else None,
            "latency": snap.latency_summary(m.AI_LATENCY), "fallbacks": int(snap.counter(m.AI_FALLBACKS)),
            "deadline_exceeded": int(snap.counter(m.AI_DEADLINE)),
            "tokens": {"input": int(snap.counter(m.AI_TOKENS, direction="input")), "output": int(snap.counter(m.AI_TOKENS, direction="output"))},
            "cost_usd": round(snap.counter(m.AI_COST) / 100.0, 4),
            "budget_denied": {k: int(v) for k, v in snap.counter_by(m.AI_BUDGET_DENIED, "scope").items()},
            "providers": providers, "circuit_breakers": health.breaker_summary(),
            "kill_switch": bool(config.AI_KILL_SWITCH), "global_budget": health.probe("ai_budget") or {}}


def _jobs(snap) -> dict:
    queue = health.queue_stats()
    kinds = {}
    for kind in sorted(set(snap.counter_by(m.JOBS, "kind"))):
        by_outcome = snap.counter_by(m.JOBS, "outcome", kind=kind)
        kinds[kind] = {"outcomes": {k: int(v) for k, v in by_outcome.items()},
                       "queue_wait": snap.latency_summary(m.JOB_QUEUE_WAIT, kind=kind),
                       "duration": snap.latency_summary(m.JOB_DURATION, kind=kind)}
    return {"queue": queue, "queue_wait": snap.latency_summary(m.JOB_QUEUE_WAIT), "duration": snap.latency_summary(m.JOB_DURATION),
            "by_kind": kinds}


def _infra(snap) -> dict:
    db_ok, db_ms = health.check_database()
    return {"database": {"ok": db_ok, "latency_ms": db_ms, "pool": health.pool_summary()},
            "redis": health.redis_info(),
            "redis_fallbacks": {k: int(v) for k, v in snap.counter_by(m.REDIS_FALLBACK, "component").items()},
            "storage_ok": storage.health_check(),
            "processes": [{"role": d.get("role"), "age_s": round(max(0.0, time.time() - d.get("ts", 0)), 1),
                           "db_pool_utilization_pct": (d.get("db_pool") or {}).get("utilization_pct")} for d in gauges.read_all()],
            "metrics_store": {"shared_via_redis": get_store().uses_redis, "healthy": get_store().healthy()},
            "query_slots": {k: int(v) for k, v in snap.counter_by(m.QUERY_SLOTS, "outcome").items()}}


@router.get("/summary")
def ops_summary(window_minutes: int = 15, admin: User = Depends(require_admin), db: OrmSession = Depends(get_db)):
    window = _window(window_minutes)
    snap = get_store().snapshot(window)
    degraded = health.degraded_components()
    return JSONResponse(content={
        "success": True, "window_minutes": window, "environment": config.ENVIRONMENT,
        "status": "degraded" if degraded else "ok", "degraded": degraded,
        "traffic": _traffic(snap), "ai": _ai(snap), "jobs": _jobs(snap), "infrastructure": _infra(snap),
        "slo": slo.evaluate(), "open_incidents": events.list_incidents(db, status="open", limit=20),
        "errors": errors.top_errors(10),
        "privacy": "Aggregate system metrics only. No prompts, datasets, files, SQL, results, reports or user identities."})


@router.get("/providers")
def ops_providers(window_minutes: int = 60, limit: int = 50, admin: User = Depends(require_admin),
                  db: OrmSession = Depends(get_db)):
    """LLM provider health: configured fallback chain (provider + model, never keys), retry/limit settings, breaker
    states, per-provider metrics from the real metrics store, and recent fallback / all-failed events. No user data."""
    from modules.sql_generator import describe_provider_chain
    window = _window(window_minutes)
    snap = get_store().snapshot(window)
    ai = _ai(snap)
    limit = max(1, min(int(limit or 50), 200))
    history = (events.list_events(db, limit=limit, kind="llm_fallback")
               + events.list_events(db, limit=limit, kind="llm_all_providers_failed"))
    history.sort(key=lambda e: str(e.get("created_at") or e.get("at") or ""), reverse=True)
    incidents = [i for i in events.list_incidents(db, status="open", limit=50)
                 if "provider" in str(i.get("alert", "")) or str(i.get("component", "")) == "ai"]
    return JSONResponse(content={
        "success": True, "window_minutes": window,
        "order": list(config.LLM_PROVIDER_ORDER),
        "chain": describe_provider_chain(config.GROQ_API_KEY),
        "settings": {"transient_retries": config.LLM_TRANSIENT_RETRIES,
                     "retry_backoff_seconds": config.LLM_RETRY_BACKOFF_SECONDS,
                     "max_attempts_per_request": config.LLM_MAX_ATTEMPTS_PER_REQUEST,
                     "provider_timeout_seconds": config.AI_PROVIDER_TIMEOUT_SECONDS,
                     "request_deadline_seconds": config.AI_REQUEST_DEADLINE_SECONDS},
        "circuit_breakers": ai["circuit_breakers"], "metrics": ai["providers"], "fallbacks": ai["fallbacks"],
        "fallback_history": history[:limit], "incidents": incidents})


@router.get("/slo")
def ops_slo(admin: User = Depends(require_admin)):
    return JSONResponse(content={"success": True, **slo.evaluate()})


@router.get("/incidents")
def ops_incidents(status: Optional[str] = None, limit: int = 50, admin: User = Depends(require_admin), db: OrmSession = Depends(get_db)):
    return JSONResponse(content={"success": True, "incidents": events.list_incidents(db, status=status, limit=limit)})


@router.get("/events")
def ops_events(kind: Optional[str] = None, limit: int = 100, admin: User = Depends(require_admin), db: OrmSession = Depends(get_db)):
    return JSONResponse(content={"success": True, "events": events.list_events(db, limit=limit, kind=kind)})


@router.get("/errors")
def ops_errors(limit: int = 20, admin: User = Depends(require_admin)):
    return JSONResponse(content={"success": True, "errors": errors.top_errors(max(1, min(limit, 100)))})


@router.post("/alerts/evaluate")
def ops_evaluate_alerts(admin: User = Depends(require_admin)):
    """Run one alert reconciliation now (operator action, e.g. after fixing something). Names only in the response."""
    return JSONResponse(content={"success": True, **alerts.evaluate()})


def _metrics_authorized(request: Request, x_ops_token: Optional[str]) -> bool:
    token = config.OPS_METRICS_TOKEN
    return bool(token and x_ops_token and hmac.compare_digest(token.encode(), x_ops_token.encode()))


@router.get("/metrics", response_class=PlainTextResponse)
def ops_prometheus(request: Request, window_minutes: int = 5, x_ops_token: Optional[str] = Header(default=None),
                   db: OrmSession = Depends(get_db)):
    """Prometheus text format. Auth: ``X-Ops-Token`` == OPS_METRICS_TOKEN, or an admin bearer token."""
    if not _metrics_authorized(request, x_ops_token):
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return PlainTextResponse("Not authorized.\n", status_code=403)
        user = get_current_user(token=auth[7:], db=db)
        require_admin(user)
    snap = get_store().snapshot(_window(window_minutes))
    lines = list(iter_prom_lines(snap))
    pool = health.pool_summary()
    q = health.queue_stats()
    lines += ["# TYPE qs_db_pool_utilization_pct gauge", f"qs_db_pool_utilization_pct {pool.get('max_utilization_pct', 0)}",
              "# TYPE qs_queue_depth gauge", f"qs_queue_depth {q.get('depth', 0) or 0}",
              "# TYPE qs_workers_alive gauge", f"qs_workers_alive {q.get('workers_alive', 0)}"]
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
