"""Point-in-time gauges (Sprint 4): DB pool, circuit-breaker state, process liveness.

Counters/histograms (store.py) answer "what happened"; gauges answer "what is the state right now". Each web process and
each RQ worker publishes a small JSON document under ``qs:g:<role>:<host>:<pid>`` with a short TTL, so a dead process
disappears by itself and its absence is the liveness signal. The reader aggregates across processes.
Without Redis only the local process is visible (development).
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from typing import List, Optional

import config
from observability.store import get_store

logger = logging.getLogger("querysure.gauges")

_PREFIX = "qs:g:"
_role = "web"
_current_job_kind: Optional[str] = None
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def process_id(role: Optional[str] = None) -> str:
    return f"{role or _role}:{socket.gethostname()}:{os.getpid()}"


def set_role(role: str) -> None:
    global _role
    _role = role


def set_current_job(kind: Optional[str]) -> None:
    global _current_job_kind
    _current_job_kind = kind


def db_pool_stats() -> dict:
    """Connection-pool occupancy for THIS process (QueuePool). ``utilization`` = checked-out / (pool_size + max_overflow)."""
    try:
        from db import engine
        pool = engine.pool
        if not hasattr(pool, "checkedout"):
            return {"supported": False}
        size = int(pool.size())
        checked_out = int(pool.checkedout())
        overflow = max(0, int(pool.overflow()))
        capacity = size + config.DB_MAX_OVERFLOW
        return {"supported": True, "size": size, "checked_out": checked_out, "overflow": overflow,
                "capacity": capacity, "utilization_pct": round(100.0 * checked_out / capacity, 1) if capacity else 0.0}
    except Exception:  # noqa: BLE001
        return {"supported": False}


def breaker_states() -> dict:
    try:
        import ai_guard
        return ai_guard.states()
    except Exception:  # noqa: BLE001
        return {}


def collect_local() -> dict:
    return {"proc": process_id(), "role": _role, "pid": os.getpid(), "ts": time.time(),
            "db_pool": db_pool_stats(), "breakers": breaker_states(), "job_kind": _current_job_kind}


def publish() -> bool:
    store = get_store()
    client = store.redis_client()
    if client is None or not store.healthy():
        return False
    doc = collect_local()
    try:
        client.set(_PREFIX + doc["proc"], json.dumps(doc, separators=(",", ":")), ex=config.GAUGE_TTL_SECONDS)
        return True
    except Exception:  # noqa: BLE001
        logger.debug("gauge publish failed", exc_info=True)
        return False


def read_all() -> List[dict]:
    """Live process documents (web + worker). Always includes this process."""
    docs = {}
    local = collect_local()
    docs[local["proc"]] = local
    client = get_store().redis_client()
    if client is not None:
        try:
            for key in client.scan_iter(match=_PREFIX + "*", count=200):
                raw = client.get(key)
                if raw:
                    d = json.loads(raw)
                    if d.get("proc") != local["proc"]:
                        docs[d["proc"]] = d
        except Exception:  # noqa: BLE001
            logger.debug("gauge read failed", exc_info=True)
    return list(docs.values())


def start_publisher(role: str, interval: Optional[float] = None) -> None:
    """Daemon thread publishing this process's gauges (no-op without Redis). Idempotent."""
    global _thread
    set_role(role)
    if _thread is not None or get_store().redis_client() is None:
        return
    every = interval or max(2.0, config.GAUGE_TTL_SECONDS / 3.0)

    def _loop():
        while not _stop.wait(every):
            publish()

    publish()
    _thread = threading.Thread(target=_loop, name="qs-gauge-publisher", daemon=True)
    _thread.start()


def stop_publisher() -> None:
    global _thread
    _stop.set()
    _thread = None
    _stop.clear()
