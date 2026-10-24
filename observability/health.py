"""Readiness + dependency health (Sprint 4): CRITICAL vs DEGRADED.

Critical  (-> HTTP 503 "not_ready", the platform stops routing traffic here):
    database   - cannot run ``SELECT 1`` within READINESS_DB_TIMEOUT_MS (pool exhaustion counts: the app cannot serve).
    redis      - configured (or production) but unreachable. Same rule Sprint 1 introduced; kept.
Degraded  (-> HTTP 200 "degraded", keep serving, alert instead of draining the instance):
    worker     - no live RQ worker while jobs are waiting (uploads/reports/e-mail stall, analytics reads keep working).
    queue      - backlog depth / oldest-queued age over threshold.
    storage    - object store unreachable (affected routes already answer 503 STORAGE_UNAVAILABLE).
    ai         - every configured provider's circuit is open, the kill switch is on, or the global spend cap is reached.
    db_pool    - connection pool utilisation over threshold (early warning before it becomes "database").
    metrics    - the metrics store lost Redis (dashboards are partial; requests are unaffected).
Rationale: draining the only web instance because a *non-critical* dependency is down turns a partial outage into a total one.

Non-critical probes are cached for READINESS_CACHE_SECONDS so a probe storm cannot hammer S3 / Redis. The public response
contains component NAMES only -- no hostnames, versions, counts or error text. Detail is admin-only (ops_api).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

import config
from observability import gauges
from observability.store import get_store

logger = logging.getLogger("querysure.health")

_probes: Dict[str, Callable[[], Optional[dict]]] = {}
_conn = None
_conn_lock = threading.Lock()


def register_probe(name: str, fn: Callable[[], Optional[dict]]) -> None:
    """Let api.py expose state that lives there (AI budget) without observability importing api."""
    _probes[name] = fn


def probe(name: str) -> Optional[dict]:
    fn = _probes.get(name)
    if fn is None:
        return None
    try:
        return fn()
    except Exception:  # noqa: BLE001
        logger.debug("probe %s failed", name, exc_info=True)
        return None


def _rq_connection():
    global _conn
    if not config.REDIS_URL:
        return None
    if _conn is None:
        with _conn_lock:
            if _conn is None:
                import redis
                _conn = redis.Redis.from_url(config.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    return _conn


def set_rq_connection(conn) -> None:
    """Test hook."""
    global _conn
    _conn = conn


_probe_lock = threading.Lock()
_probe_inflight = threading.Event()


def _probe_database(timeout_ms: int, box: list) -> None:
    """The actual probe (runs in a helper thread so the caller can bound the WHOLE thing, including pool checkout)."""
    from sqlalchemy import text

    from db import SessionLocal
    db = None
    try:
        db = SessionLocal()
        if config.DATABASE_URL.startswith("postgresql"):
            db.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
        db.execute(text("SELECT 1"))
        box.append(True)
    except Exception:  # noqa: BLE001
        box.append(False)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
        _probe_inflight.clear()


def check_database(timeout_ms: Optional[int] = None) -> tuple:
    """(ok, latency_ms), bounded by READINESS_DB_TIMEOUT_MS *in total*.

    Found by the Sprint 4 closure test: with the connection pool exhausted, ``SessionLocal()`` blocks for the pool timeout
    (DB_POOL_TIMEOUT, 10 s by default) and the probe took 10 s to say "not ready" -- longer than a platform health-check timeout, so
    the platform saw a hung probe instead of a clean 503. The probe now runs in a helper thread and is abandoned after the budget.
    Only one probe thread exists at a time: while an earlier one is still stuck waiting for a connection the database is, by
    definition, not answering, so further calls fail immediately instead of piling up threads."""
    budget_ms = int(timeout_ms or config.READINESS_DB_TIMEOUT_MS)
    start = time.monotonic()
    with _probe_lock:
        if _probe_inflight.is_set():
            return False, round((time.monotonic() - start) * 1000, 1)
        _probe_inflight.set()
    box: list = []
    t = threading.Thread(target=_probe_database, args=(budget_ms, box), name="qs-ready-probe", daemon=True)
    t.start()
    t.join(budget_ms / 1000.0)
    ok = bool(box and box[0])
    return ok, round((time.monotonic() - start) * 1000, 1)


def redis_info() -> dict:
    """Admin-only Redis saturation figures. Empty dict when Redis is not configured / unreachable."""
    conn = _rq_connection()
    if conn is None:
        return {"configured": False}
    start = time.monotonic()
    try:
        conn.ping()
        ping_ms = round((time.monotonic() - start) * 1000, 1)
        info = conn.info()
        maxmem = int(info.get("maxmemory") or 0)
        used = int(info.get("used_memory") or 0)
        return {"configured": True, "reachable": True, "ping_ms": ping_ms,
                "connected_clients": int(info.get("connected_clients", 0)), "blocked_clients": int(info.get("blocked_clients", 0)),
                "used_memory_mb": round(used / 1048576, 2), "maxmemory_mb": round(maxmem / 1048576, 2) if maxmem else None,
                "memory_utilization_pct": round(100.0 * used / maxmem, 1) if maxmem else None,
                "ops_per_sec": int(info.get("instantaneous_ops_per_sec", 0)),
                "evicted_keys": int(info.get("evicted_keys", 0)), "rejected_connections": int(info.get("rejected_connections", 0)),
                "keyspace_hits": int(info.get("keyspace_hits", 0)), "keyspace_misses": int(info.get("keyspace_misses", 0))}
    except Exception:  # noqa: BLE001
        return {"configured": True, "reachable": False}


def queue_stats() -> dict:
    """RQ depth, oldest waiting job age, in-flight/failed counts and live workers (heartbeat documents)."""
    workers = [d for d in gauges.read_all() if d.get("role") == "worker"]
    out: dict = {"available": False, "workers_alive": len(workers),
                 "workers_busy": sum(1 for w in workers if w.get("job_kind"))}
    conn = _rq_connection()
    if conn is None:
        return out
    try:
        from rq import Queue
        from rq.registry import FailedJobRegistry, StartedJobRegistry

        from jobs import QUEUE_NAME
        q = Queue(QUEUE_NAME, connection=conn)
        depth = q.count
        oldest_age = None
        ids = q.get_job_ids(0, 1)
        if ids:
            job = q.fetch_job(ids[0])
            enq = getattr(job, "enqueued_at", None) if job else None
            if enq is not None:
                enq = enq if enq.tzinfo else enq.replace(tzinfo=timezone.utc)
                oldest_age = max(0.0, (datetime.now(timezone.utc) - enq).total_seconds())
        out.update({"available": True, "depth": int(depth), "oldest_age_s": None if oldest_age is None else round(oldest_age, 1),
                    "started": int(StartedJobRegistry(queue=q).count), "failed": int(FailedJobRegistry(queue=q).count)})
    except Exception:  # noqa: BLE001
        logger.debug("queue stats failed", exc_info=True)
    return out


def pool_summary() -> dict:
    procs = [d for d in gauges.read_all() if (d.get("db_pool") or {}).get("supported")]
    if not procs:
        return {"processes": 0}
    utils = [d["db_pool"]["utilization_pct"] for d in procs]
    return {"processes": len(procs), "max_utilization_pct": max(utils), "avg_utilization_pct": round(sum(utils) / len(utils), 1),
            "checked_out_total": sum(d["db_pool"]["checked_out"] for d in procs),
            "capacity_total": sum(d["db_pool"]["capacity"] for d in procs)}


def breaker_summary() -> dict:
    """provider -> worst state across live processes (open > half_open > closed)."""
    rank = {"closed": 0, "half_open": 1, "open": 2}
    worst: Dict[str, str] = {}
    for d in gauges.read_all():
        for provider, state in (d.get("breakers") or {}).items():
            if rank.get(state, 0) >= rank.get(worst.get(provider, "closed"), 0):
                worst[provider] = state
    return worst


def configured_providers() -> list:
    names = []
    if config.GROQ_API_KEY:
        names.append("groq")
    if config.OPENAI_API_KEY:
        names.append("openai")
    if config.ANTHROPIC_API_KEY:
        names.append("anthropic")
    return names


# ---------------------------------------------------------------------------------------------------------------
# Non-critical component evaluation (cached)
# ---------------------------------------------------------------------------------------------------------------
_cache: dict = {"at": 0.0, "value": None}
_cache_lock = threading.Lock()


def degraded_components(force: bool = False) -> list:
    now = time.monotonic()
    with _cache_lock:
        if not force and _cache["value"] is not None and now - _cache["at"] < config.READINESS_CACHE_SECONDS:
            return list(_cache["value"])
    result = _compute_degraded()
    with _cache_lock:
        _cache["at"], _cache["value"] = now, list(result)
    return result


def reset_cache() -> None:
    with _cache_lock:
        _cache["at"], _cache["value"] = 0.0, None


def _compute_degraded() -> list:
    degraded = []
    try:
        if config.REDIS_URL:
            q = queue_stats()
            if q.get("available"):
                if q["workers_alive"] == 0 and q.get("depth", 0) > 0:
                    degraded.append("worker")
                if q.get("depth", 0) >= config.ALERT_QUEUE_DEPTH or (q.get("oldest_age_s") or 0) >= config.ALERT_QUEUE_OLDEST_SECONDS:
                    degraded.append("queue")
        import storage
        if not storage.health_check():
            degraded.append("storage")
        if config.AI_KILL_SWITCH:
            degraded.append("ai")
        else:
            states = breaker_summary()
            providers = configured_providers()
            if providers and all(states.get(p) == "open" for p in providers):
                degraded.append("ai")
            else:
                budget = probe("ai_budget")
                if budget and budget.get("global_cap_reached"):
                    degraded.append("ai")
        pool = pool_summary()
        if pool.get("max_utilization_pct", 0) >= config.ALERT_DB_POOL_UTILIZATION_PCT:
            degraded.append("db_pool")
        if not get_store().healthy():
            degraded.append("metrics")
    except Exception:  # noqa: BLE001 - a broken probe must never turn into a failed readiness check
        logger.debug("degraded evaluation failed", exc_info=True)
    return degraded


def readiness(db_ok: bool, redis_ok: bool) -> dict:
    """Public readiness document (see module docstring for the critical/degraded split)."""
    critical_ok = bool(db_ok and redis_ok)
    degraded = degraded_components() if critical_ok else []
    status = "not_ready" if not critical_ok else ("degraded" if degraded else "ready")
    return {"success": critical_ok, "status": status, "checks": {"database": bool(db_ok), "redis": bool(redis_ok)},
            "degraded": degraded}
