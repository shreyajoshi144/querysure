"""Per-provider circuit breaker for LLM calls (Sprint 3).

closed    -> calls flow; consecutive failures are counted.
open      -> after N consecutive failures; calls are skipped (no network) until the cooldown elapses.
half-open -> after the cooldown exactly ONE probe call is allowed; success closes the breaker,
             failure re-opens it for another full cooldown. Other callers keep being skipped while
             the probe is in flight.

State is process-local (each web worker/RQ worker learns about an outage independently); that is
deliberate -- it needs no shared infrastructure and fails safe. Thresholds come from config
(CIRCUIT_FAILURE_THRESHOLD / CIRCUIT_COOLDOWN_SECONDS).
"""
from __future__ import annotations

import threading
import time
from contextvars import ContextVar
from typing import Callable, Dict, Optional

import config

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class CircuitBreaker:
    def __init__(self, name: str, threshold: int | None = None, cooldown: float | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.name = name
        self._threshold = threshold
        self._cooldown = cooldown
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def threshold(self) -> int:
        return self._threshold if self._threshold is not None else config.CIRCUIT_FAILURE_THRESHOLD

    @property
    def cooldown(self) -> float:
        return self._cooldown if self._cooldown is not None else config.CIRCUIT_COOLDOWN_SECONDS

    @property
    def state(self) -> str:
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> str:
        if self._opened_at is None:
            return CLOSED
        if self._probe_in_flight:
            return HALF_OPEN
        return OPEN if self._clock() - self._opened_at < self.cooldown else HALF_OPEN

    def allow(self) -> bool:
        """True if the caller may make a provider call now. In half-open, only the first caller
        gets True (it becomes the probe and MUST report record_success/record_failure)."""
        with self._lock:
            if self._opened_at is None:
                return True
            if self._probe_in_flight:
                return False
            if self._clock() - self._opened_at >= self.cooldown:
                self._probe_in_flight = True
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            was_open = self._opened_at is not None
            self._failures = 0
            self._opened_at = None
            self._probe_in_flight = False
        if was_open:  # Sprint 4: operators see recovery as an event, not just the absence of failures
            _emit_transition(self.name, "open", "closed")

    def record_failure(self) -> None:
        opened_now = False
        with self._lock:
            if self._probe_in_flight:  # failed probe -> straight back to open, fresh cooldown
                self._probe_in_flight = False
                self._opened_at = self._clock()
                return
            self._failures += 1
            if self._failures >= self.threshold:
                opened_now = self._opened_at is None
                self._opened_at = self._clock()
        if opened_now:
            _emit_transition(self.name, "closed", "open")

    def snapshot(self) -> dict:
        with self._lock:
            return {"state": self._state_locked(), "consecutive_failures": self._failures}


_registry: Dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def get_breaker(provider: str) -> CircuitBreaker:
    with _registry_lock:
        if provider not in _registry:
            _registry[provider] = CircuitBreaker(provider)
        return _registry[provider]


def states() -> Dict[str, str]:
    """provider -> closed|open|half_open for every provider this process has called (Sprint 4 gauges / readiness)."""
    with _registry_lock:
        items = list(_registry.items())
    return {name: br.state for name, br in items}


def _emit_transition(provider: str, from_state: str, to_state: str) -> None:
    """Breaker state change -> operational event (log + DB, off the request path). Never raises into the caller."""
    try:
        from observability import events
        events.record_event(
            "circuit_opened" if to_state == "open" else "circuit_closed",
            severity="warning" if to_state == "open" else "info", component="ai",
            detail={"provider": provider, "from_state": from_state, "to_state": to_state,
                    "threshold": config.CIRCUIT_FAILURE_THRESHOLD})
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Overall deadline for ONE generate-sql request (Sprint 4). A fallback chain of N providers x a JSON retry x up to 2 SQL repair
# rounds, each with SDK retries, could otherwise pin a worker thread for minutes. The deadline lives in a ContextVar so the
# provider-call functions keep their existing signatures.
# ---------------------------------------------------------------------------
_deadline: ContextVar[Optional[float]] = ContextVar("qs_ai_deadline", default=None)


_attempts: ContextVar[Optional[list]] = ContextVar("qs_ai_attempts", default=None)


def start_deadline(seconds: Optional[float] = None):
    """Begin a deadline ``seconds`` from now (default AI_REQUEST_DEADLINE_SECONDS). Returns a token for reset_deadline.
    Also starts the shared per-request provider-call budget (LLM_MAX_ATTEMPTS_PER_REQUEST)."""
    _attempts.set([0])
    return _deadline.set(time.monotonic() + (seconds if seconds is not None else config.AI_REQUEST_DEADLINE_SECONDS))


def reset_deadline(token) -> None:
    _deadline.reset(token)
    _attempts.set(None)


def remaining() -> Optional[float]:
    """Seconds left on the active deadline, or None when no deadline is active."""
    end = _deadline.get()
    return None if end is None else end - time.monotonic()


def consume_attempt() -> bool:
    """Reserve one provider call from the request's shared budget. False once LLM_MAX_ATTEMPTS_PER_REQUEST is used up.
    Outside a request (no deadline started) there is no shared budget."""
    box = _attempts.get()
    if box is None:  # no request budget active (direct/library use): only the per-chain bounds apply
        return True
    if box[0] >= config.LLM_MAX_ATTEMPTS_PER_REQUEST:
        return False
    box[0] += 1
    return True


def reset_attempts() -> None:
    _attempts.set(None)


# Retried on the same provider/model. 429 is NOT here: the SDK already honours Retry-After once, so a rate-limited
# provider fails over to the next model instead of adding load.
TRANSIENT_ERROR_CODES = frozenset({"timeout", "connection", "server_error"})


def can_start_call() -> bool:
    left = remaining()
    return left is None or left >= config.AI_MIN_CALL_BUDGET_SECONDS


def call_timeout(default: Optional[float] = None) -> float:
    """Per-attempt timeout for a provider SDK call: the configured per-call timeout, capped so that the SDK's single retry
    (max_retries=1 => up to two attempts) still fits inside what is left of the request deadline."""
    base = default if default is not None else config.AI_PROVIDER_TIMEOUT_SECONDS
    left = remaining()
    if left is None:
        return base
    return max(1.0, min(base, left / 2.0))


def classify_provider_error(exc: BaseException) -> str:
    """Low-cardinality failure reason for metrics, derived from the exception TYPE / status code only (never its message)."""
    name = type(exc).__name__.lower()
    status = getattr(exc, "status_code", None)
    if "timeout" in name:
        return "timeout"
    if status == 429 or "ratelimit" in name:
        return "rate_limited"
    if status in (401, 403) or "authentication" in name or "permission" in name:
        return "auth"
    if status == 404 or "notfound" in name:
        return "model_not_found"
    if "connection" in name:
        return "connection"
    if isinstance(status, int) and status >= 500:
        return "server_error"
    if status == 400 or "badrequest" in name:
        return "bad_request"
    return "error"


def reset_all() -> None:
    """Forget all breaker state (tests, and an operator hook)."""
    with _registry_lock:
        _registry.clear()
