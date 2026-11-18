"""AI call tracing: one ``ai_calls`` row per provider attempt, grouped by a trace id.

A trace is opened around one user-facing AI operation (today: one /api/generate-sql request, including its JSON retry
and repair rounds; later: a multi-step workflow). Every provider attempt made inside it -- successes, failures and
attempts skipped by the circuit breaker, deadline or attempt cap -- is appended to the trace and, when the trace belongs
to a user, written to ``ai_calls`` in its own database session.

Privacy: rows hold ids, provider, model, task, timings, token counts, cost and sanitized error CODES. Never prompts,
SQL, questions, result rows, keys or provider error text. Recording never raises into the AI call path.
Outside a trace (offline evaluation, scripts) nothing is written.
"""
from __future__ import annotations

import contextlib
import logging
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator, List, Optional

logger = logging.getLogger("querysure.ai")

SKIP_CODES = frozenset({"circuit_open", "deadline_exceeded", "attempt_limit"})


@dataclass
class Trace:
    trace_id: str
    user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    request_id: Optional[str] = None
    calls: List[dict] = field(default_factory=list)


_current: ContextVar[Optional[Trace]] = ContextVar("qs_ai_trace", default=None)


@contextlib.contextmanager
def start(user_id: Optional[str] = None, workspace_id: Optional[str] = None,
          request_id: Optional[str] = None) -> Iterator[Trace]:
    """Open a trace (nested calls join the outer one)."""
    outer = _current.get()
    if outer is not None:
        yield outer
        return
    trace = Trace(trace_id=uuid.uuid4().hex, user_id=user_id, workspace_id=workspace_id, request_id=request_id)
    token = _current.set(trace)
    try:
        yield trace
    finally:
        _current.reset(token)


def current() -> Optional[Trace]:
    return _current.get()


def record_attempt(*, provider: str, model: str, task: str, attempt_no: int, retry_index: int,
                   error: Optional[str], latency_ms: Optional[float], usage: Optional[dict],
                   tool_name: Optional[str] = None) -> None:
    trace = _current.get()
    if trace is None:
        return
    outcome = "success" if not error else ("skipped" if error in SKIP_CODES else "failure")
    call = {
        "provider": provider, "model": model, "task_type": task, "attempt_no": attempt_no, "retry_index": retry_index,
        "outcome": outcome, "error_category": error, "latency_ms": None if latency_ms is None else int(round(latency_ms)),
        "input_tokens": (usage or {}).get("input_tokens"), "output_tokens": (usage or {}).get("output_tokens"),
        "usage_estimated": bool((usage or {}).get("estimated", False)),
        "estimated_cost_cents": float((usage or {}).get("cost_cents") or 0.0),
        "tool_calls": [tool_name] if tool_name and outcome == "success" else None,
    }
    trace.calls.append(call)
    if trace.user_id:
        _persist(trace, call)


def _persist(trace: Trace, call: dict) -> None:
    db = None
    try:
        from db import AICall, SessionLocal
        db = SessionLocal()
        db.add(AICall(trace_id=trace.trace_id, request_id=trace.request_id, user_id=trace.user_id,
                      workspace_id=trace.workspace_id, **call))
        db.commit()
    except Exception as exc:  # noqa: BLE001 - tracing must never break generation
        if db is not None:
            db.rollback()
        logger.warning("ai call trace write failed: %s", type(exc).__name__)
    finally:
        if db is not None:
            db.close()
