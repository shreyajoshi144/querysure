"""Alert rules, incident lifecycle and delivery (Sprint 4).

``collect_signals()`` turns the current aggregate state (SLOs, queue/worker health, DB/Redis, circuit breakers, AI spend)
into a list of *signals*. ``evaluate()`` reconciles them with the ``ops_incidents`` table: a new signal opens an incident
and notifies; a signal that stays gone for ALERT_RESOLVE_AFTER_SECONDS resolves it (and notifies). One process is elected per
interval (Redis ``SET NX``) so N web workers do not N-times-notify.

Delivery is a generic JSON webhook (ALERT_WEBHOOK_URL: works with Slack-compatible/Alertmanager/PagerDuty-style relays) and is
ALWAYS accompanied by a log line + DB row. The payload carries alert name, severity, component, a numeric summary and an
incident id -- no user ids, e-mails, prompts, SQL or data. Delivery is best-effort (2 attempts, 3s timeout) and never raises.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

import config
from observability import events, health, metrics, slo
from observability.store import get_store

logger = logging.getLogger("querysure.alerts")


@dataclass
class Signal:
    key: str
    alert: str
    severity: str          # warning | critical
    component: str
    summary: str
    detail: dict = field(default_factory=dict)


def _fmt(v) -> str:
    return "n/a" if v is None else (f"{v:.1f}" if isinstance(v, float) else str(v))


def collect_signals() -> List[Signal]:
    sigs: List[Signal] = []

    db_ok, db_ms = health.check_database()
    if not db_ok:
        sigs.append(Signal("database_unavailable", "database_unavailable", "critical", "database",
                           "Database health check failed", {"component": "database", "value": db_ms}))
    if config.REDIS_URL:
        r = health.redis_info()
        if not r.get("reachable"):
            sigs.append(Signal("redis_unavailable", "redis_unavailable", "critical", "redis",
                               "Redis is unreachable (rate limiting, budgets and job queue degraded)", {"component": "redis"}))
        elif r.get("memory_utilization_pct") and r["memory_utilization_pct"] >= 90:
            sigs.append(Signal("redis_memory_high", "redis_memory_high", "warning", "redis",
                               f"Redis memory at {_fmt(r['memory_utilization_pct'])}% of maxmemory",
                               {"component": "redis", "pct": r["memory_utilization_pct"]}))

    degraded = health.degraded_components(force=True)
    if "worker" in degraded:
        q = health.queue_stats()
        sigs.append(Signal("worker_down", "worker_down", "critical", "worker",
                           f"No live background worker while {q.get('depth', 0)} job(s) wait",
                           {"component": "worker", "depth": q.get("depth", 0), "workers": q.get("workers_alive", 0)}))
    if "queue" in degraded:
        q = health.queue_stats()
        sigs.append(Signal("queue_backlog", "queue_backlog", "warning", "queue",
                           f"Queue backlog: depth {q.get('depth')}, oldest {_fmt(q.get('oldest_age_s'))}s",
                           {"component": "queue", "depth": q.get("depth") or 0, "oldest_age_s": q.get("oldest_age_s") or 0}))
    if "storage" in degraded:
        sigs.append(Signal("storage_unavailable", "storage_unavailable", "critical", "storage",
                           "Object storage health check failed", {"component": "storage"}))
    if "db_pool" in degraded:
        p = health.pool_summary()
        sigs.append(Signal("db_pool_saturated", "db_pool_saturated", "warning", "database",
                           f"DB pool utilisation {_fmt(p.get('max_utilization_pct'))}%",
                           {"component": "database", "utilization_pct": p.get("max_utilization_pct") or 0,
                            "threshold": config.ALERT_DB_POOL_UTILIZATION_PCT}))

    states = health.breaker_summary()
    open_providers = sorted(p for p, s in states.items() if s == "open")
    for provider in open_providers:
        sigs.append(Signal(f"ai_circuit_open:{provider}", "ai_circuit_open", "warning", "ai",
                           f"Circuit breaker open for AI provider {provider}", {"provider": provider, "state": "open"}))
    configured = health.configured_providers()
    if configured and all(states.get(p) == "open" for p in configured):
        sigs.append(Signal("ai_all_providers_down", "ai_all_providers_down", "critical", "ai",
                           "Every configured AI provider is failing", {"providers_open": len(configured), "providers_total": len(configured)}))
    if config.AI_KILL_SWITCH:
        sigs.append(Signal("ai_kill_switch_active", "ai_kill_switch_active", "warning", "ai",
                           "AI kill switch is ON: generate-sql is disabled", {"component": "ai"}))
    budget = health.probe("ai_budget")
    if budget:
        if budget.get("global_cap_reached"):
            sigs.append(Signal("ai_budget_exhausted", "ai_budget_exhausted", "critical", "ai",
                               "Global daily AI spend cap reached; AI requests are refused until 00:00 UTC",
                               {"component": "ai", "spent_pct": budget.get("spent_pct") or 100}))
        elif (budget.get("spent_pct") or 0) >= config.ALERT_GLOBAL_BUDGET_PCT:
            sigs.append(Signal("ai_budget_high", "ai_budget_high", "warning", "ai",
                               f"Global AI spend at {_fmt(budget.get('spent_pct'))}% of today's cap",
                               {"component": "ai", "spent_pct": budget.get("spent_pct"), "threshold": config.ALERT_GLOBAL_BUDGET_PCT}))

    result = slo.evaluate()
    by_name = {o["name"]: o for o in result["objectives"]}
    av = by_name["availability"]
    bs, bl = av.get("burn_rate_short"), av.get("burn_rate_long")
    thr = config.SLO_BURN_RATE_THRESHOLD
    if bs is not None and bl is not None and bs >= thr and bl >= thr:
        sigs.append(Signal("slo_availability_burn", "slo_availability_burn", "critical", "api",
                           f"Availability error budget burning fast (short {bs}x, long {bl}x)",
                           {"slo": "availability", "burn_short": bs, "burn_long": bl, "threshold": thr}))
    for o in result["objectives"]:
        if o["name"] == "availability" or o["status"] != "breach":
            continue
        sigs.append(Signal(f"slo_breach:{o['name']}", "slo_breach", "warning", "api" if o["name"].startswith("api") else o["name"].split("_")[0],
                           f"SLO {o['name']} breached: {o['sli']}{o['unit']} vs target {o['target']}{o['unit']}",
                           {"slo": o["name"], "sli": o["sli"], "target": o["target"], "samples": o["samples"]}))
    return sigs


# ---------------------------------------------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------------------------------------------
def build_payload(status: str, sig: Signal, incident_id: Optional[str]) -> dict:
    return {"source": "querysure", "environment": config.ENVIRONMENT, "status": status, "alert": sig.alert,
            "severity": sig.severity, "component": sig.component, "summary": sig.summary, "incident_id": incident_id,
            "detail": events.sanitize_detail(sig.detail), "at": datetime.now(timezone.utc).isoformat()}


def deliver(payload: dict) -> bool:
    url = config.ALERT_WEBHOOK_URL
    if not url:
        metrics.record_alert_delivery("skipped")
        return False
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    for _attempt in range(2):
        try:
            req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310 - operator-configured URL (https enforced in prod)
                if 200 <= resp.status < 300:
                    metrics.record_alert_delivery("ok")
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    metrics.record_alert_delivery("failed")
    logger.warning("alert webhook delivery failed", extra={"event": "alert_delivery_failed", "alert": payload.get("alert")})
    return False


_sent: dict = {}
_sent_lock = threading.Lock()


def _first_send(key: str, ttl: int = 900) -> bool:
    """Cross-process de-duplication for notifications made while the DB (and hence the incident table) is unavailable."""
    client = get_store().redis_client()
    if client is not None:
        try:
            return bool(client.set(f"qs:alert:sent:{key}", "1", nx=True, ex=ttl))
        except Exception:  # noqa: BLE001
            pass
    now = time.time()
    with _sent_lock:
        if _sent.get(key, 0) > now:
            return False
        _sent[key] = now + ttl
        return True


# ---------------------------------------------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------------------------------------------
def evaluate(*, notify: bool = True, signals: Optional[List[Signal]] = None) -> dict:
    """Reconcile current signals with the incident table. Returns what changed (names only)."""
    sigs = collect_signals() if signals is None else signals
    opened, resolved, active = [], [], sorted(s.key for s in sigs)
    from db import SessionLocal
    db = None
    try:
        db = SessionLocal()
        for s in sigs:
            row, created = events.open_or_touch_incident(db, s.key, alert=s.alert, severity=s.severity, component=s.component, detail=s.detail)
            if created:
                opened.append(s.key)
                events.record_event("alert_fired", severity=s.severity, component=s.component,
                                    detail={**s.detail, "kind": s.alert}, sync=True)
                if notify:
                    deliver(build_payload("firing", s, getattr(row, "id", None)))
        stale = events.resolve_stale_incidents(db, set(active), config.ALERT_RESOLVE_AFTER_SECONDS)
        for row in stale:
            resolved.append(row.incident_key)
            events.record_event("alert_resolved", severity="info", component=row.component, detail={"kind": row.alert}, sync=True)
            if notify:
                deliver(build_payload("resolved", Signal(row.incident_key, row.alert, row.severity, row.component,
                                                          f"{row.alert} cleared", {}), row.id))
        if opened or resolved:
            events.prune(db, config.OPS_EVENT_RETENTION_DAYS) if int(time.time()) % 50 == 0 else None
    except Exception:  # noqa: BLE001 - DB unavailable: still tell humans, de-duplicated
        logger.warning("alert reconciliation could not use the incident table", extra={"event": "alert_db_unavailable"})
        if notify:
            for s in sigs:
                if _first_send(s.key):
                    deliver(build_payload("firing", s, None))
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
    return {"active": active, "opened": opened, "resolved": resolved}


# ---------------------------------------------------------------------------------------------------------------
# Scheduling (one elected evaluator per interval)
# ---------------------------------------------------------------------------------------------------------------
_local_next = 0.0
_thread: Optional[threading.Thread] = None
_lock = threading.Lock()


def _is_leader() -> bool:
    global _local_next
    interval = config.ALERT_EVAL_INTERVAL_SECONDS
    client = get_store().redis_client()
    if client is not None:
        try:
            return bool(client.set("qs:alerts:leader", str(os.getpid()), nx=True, ex=max(1, interval - 1)))
        except Exception:  # noqa: BLE001
            pass
    now = time.monotonic()
    with _lock:
        if now < _local_next:
            return False
        _local_next = now + interval - 1
        return True


def run_once_if_leader() -> Optional[dict]:
    """One maintenance tick, executed by at most one process per interval: reap stale jobs, then (if ALERTS_ENABLED) reconcile
    alerts. Returns the alert result, or None when this process was not elected / alerts are off."""
    if not _is_leader():
        return None
    try:
        from jobs import reap_stale_jobs
        reap_stale_jobs()
    except Exception:  # noqa: BLE001
        logger.debug("stale job reaper failed", exc_info=True)
    if not config.ALERTS_ENABLED:
        return None
    return evaluate()


def start_maintenance() -> None:
    """Background maintenance thread (web processes). Idempotent. The stale-job reaper always runs; alert evaluation only when
    ALERTS_ENABLED."""
    global _thread
    if _thread is not None:
        return

    def _loop():
        time.sleep(5)
        while True:
            try:
                run_once_if_leader()
            except Exception:  # noqa: BLE001
                logger.debug("maintenance loop error", exc_info=True)
            time.sleep(config.ALERT_EVAL_INTERVAL_SECONDS)

    _thread = threading.Thread(target=_loop, name="qs-maintenance", daemon=True)
    _thread.start()
