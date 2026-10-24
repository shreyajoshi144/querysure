"""Named metric recorders (Sprint 4). The ONLY place metric names and label sets are defined.

Every recorder is exception-safe: metrics must never fail or slow a request. Labels are deliberately low-cardinality and carry
no user data: route *templates* (``/api/reports/{report_id}/download``), status classes, provider/model names, outcomes.
"""
from __future__ import annotations

import functools
import logging
from typing import Optional

from observability.store import get_store

logger = logging.getLogger("querysure.metrics")

# --- metric names (also the keys the SLO / alert / ops code reads) ---------------------------------------------------
HTTP_REQUESTS = "http_requests"          # {route, method, status_class}
HTTP_LATENCY = "http_request_ms"         # histogram {route}
RATE_LIMITED = "rate_limited"            # {route}
AI_REQUESTS = "ai_requests"              # {provider, outcome=success|failure}
AI_LATENCY = "ai_latency_ms"             # histogram {provider}
AI_TOKENS = "ai_tokens"                  # {provider, direction=input|output}
AI_COST = "ai_cost_cents"                # {provider}
AI_PROVIDER_FAILURES = "ai_provider_failures"   # {provider, reason}
AI_FALLBACKS = "ai_fallbacks"            # {}
AI_CIRCUIT_SKIPS = "ai_circuit_skips"    # {provider}
AI_BUDGET_DENIED = "ai_budget_denied"    # {scope=user|global|kill_switch|unavailable}
AI_DEADLINE = "ai_deadline_exceeded"     # {}
JOBS = "jobs"                            # {kind, outcome}
JOB_QUEUE_WAIT = "job_queue_wait_ms"     # histogram {kind}
JOB_DURATION = "job_duration_ms"         # histogram {kind}
REDIS_FALLBACK = "redis_fallback"        # {component}
DB_BUSY = "db_busy"                      # {}
ADMISSION_REJECTED = "admission_rejected"  # {}
QUERY_SLOTS = "query_slots"              # {outcome=reserved|refunded|refund_failed|denied}
ERRORS = "errors"                        # {component, type}
OPS_EVENTS = "ops_events"                # {kind}
ALERT_DELIVERY = "alert_delivery"        # {outcome}


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            logger.debug("metric recording failed", exc_info=True)
            return None
    return wrapper


def status_class(status: int) -> str:
    return f"{int(status) // 100}xx"


@_safe
def record_http(route: str, method: str, status: int, duration_ms: float) -> None:
    s = get_store()
    s.inc(HTTP_REQUESTS, route=route, method=method, status_class=status_class(status))
    s.observe(HTTP_LATENCY, duration_ms, route=route)


@_safe
def record_rate_limited(route: str) -> None:
    get_store().inc(RATE_LIMITED, route=route)


@_safe
def record_ai_call(provider: Optional[str], *, success: bool, latency_ms: Optional[float] = None,
                   input_tokens: int = 0, output_tokens: int = 0, cost_cents: float = 0.0,
                   fallback: bool = False) -> None:
    s = get_store()
    p = provider or "none"
    s.inc(AI_REQUESTS, provider=p, outcome="success" if success else "failure")
    if latency_ms is not None:
        s.observe(AI_LATENCY, latency_ms, provider=p)
    if input_tokens:
        s.inc(AI_TOKENS, float(input_tokens), provider=p, direction="input")
    if output_tokens:
        s.inc(AI_TOKENS, float(output_tokens), provider=p, direction="output")
    if cost_cents:
        s.inc(AI_COST, float(cost_cents), provider=p)
    if fallback:
        s.inc(AI_FALLBACKS)


@_safe
def record_provider_failure(provider: str, reason: str) -> None:
    get_store().inc(AI_PROVIDER_FAILURES, provider=provider, reason=reason)


@_safe
def record_circuit_skip(provider: str) -> None:
    get_store().inc(AI_CIRCUIT_SKIPS, provider=provider)


@_safe
def record_ai_budget_denied(scope: str) -> None:
    get_store().inc(AI_BUDGET_DENIED, scope=scope)


@_safe
def record_ai_deadline() -> None:
    get_store().inc(AI_DEADLINE)


@_safe
def record_job(kind: str, outcome: str, queue_wait_ms: Optional[float] = None, duration_ms: Optional[float] = None) -> None:
    s = get_store()
    s.inc(JOBS, kind=kind, outcome=outcome)
    if queue_wait_ms is not None:
        s.observe(JOB_QUEUE_WAIT, queue_wait_ms, kind=kind)
    if duration_ms is not None:
        s.observe(JOB_DURATION, duration_ms, kind=kind)


@_safe
def record_redis_fallback(component: str) -> None:
    get_store().inc(REDIS_FALLBACK, component=component)


@_safe
def record_db_busy() -> None:
    get_store().inc(DB_BUSY)


@_safe
def record_admission_rejected() -> None:
    get_store().inc(ADMISSION_REJECTED)


@_safe
def record_query_slot(outcome: str) -> None:
    get_store().inc(QUERY_SLOTS, outcome=outcome)


@_safe
def record_error(component: str, error_type: str) -> None:
    get_store().inc(ERRORS, component=component, type=error_type)


@_safe
def record_ops_event(kind: str) -> None:
    get_store().inc(OPS_EVENTS, kind=kind)


@_safe
def record_alert_delivery(outcome: str) -> None:
    get_store().inc(ALERT_DELIVERY, outcome=outcome)
