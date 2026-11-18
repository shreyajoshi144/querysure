"""Run one prompt through an ordered provider chain with breakers, bounded retries, deadline and attempt budget.

Moved unchanged from modules/sql_generator.py (Sprint 2); the only addition is that every attempt -- success, failure or
skip -- is also reported to ai/trace.py with its task, latency and retry index.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

import ai_guard
from ai import router, trace
from ai.types import LLMResult, ProviderAttempt, ProviderError
from ai.usage import usage_for_call
from observability import metrics as observability_metrics

_attempt_logger = logging.getLogger("querysure.ai")


def log_attempt(provider: str, model: str, attempt: int, latency_ms: Optional[float], outcome: str,
                error_code: Optional[str] = None) -> None:
    """One structured line per provider call. Codes and timings only: no prompt, SQL, key or exception message."""
    _attempt_logger.info("llm attempt", extra={
        "event": "llm_attempt", "provider": provider, "model": model, "attempt": attempt,
        "latency_ms": None if latency_ms is None else round(latency_ms, 1), "outcome": outcome, "error_code": error_code})


def record_ops_event(kind: str, severity: str, detail: dict) -> None:
    try:
        from observability import events as _events
        _events.record_event(kind, severity=severity, component="ai", detail=detail)
    except Exception:  # noqa: BLE001 - observability must never break generation
        pass


def _note(attempts: List[ProviderAttempt], attempt: ProviderAttempt, *, attempt_no: int, retry_index: int,
          tool_name: Optional[str]) -> None:
    attempts.append(attempt)
    provider, _, model = attempt.name.partition(":")
    trace.record_attempt(provider=provider, model=model, task=attempt.task or router.current_task(),
                         attempt_no=attempt_no, retry_index=retry_index, error=attempt.error,
                         latency_ms=attempt.latency_ms, usage=attempt.usage, tool_name=tool_name)


def run_chain(chain: List[dict], system_prompt: str, user_prompt: str, *, tool_names: Optional[dict] = None,
              ) -> tuple[str, str, List[ProviderAttempt]]:
    """Returns (raw_text, provider_name_used, attempts). Raises ProviderError only if every provider in the chain failed.

    Per provider/model: transient failures (timeout, connection, 429, 5xx) are retried up to LLM_TRANSIENT_RETRIES times
    with exponential backoff; permanent ones (auth, invalid model, bad request, empty/malformed reply) move straight to the
    next provider. Every call draws from the request-wide deadline AND attempt budget, so fallbacks cannot multiply cost.
    ``ProviderAttempt.error`` holds a sanitized error CODE, never the provider's exception text.
    ``tool_names`` maps provider -> structured-output tool name forced on that provider (recorded in the trace)."""
    import config as _cfg
    task = router.current_task()
    tool_names = tool_names or {}
    if not chain:
        if router.routes_for(task):
            raise ProviderError(f"No configured LLM provider for task {task!r} (check AI_TASK_ROUTES and the API keys).")
        raise ProviderError(
            "No LLM provider is configured. Set GROQ_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY."
        )

    attempts: List[ProviderAttempt] = []
    attempt_no = 0
    stop = False
    for position, provider in enumerate(chain):
        if stop:
            break
        provider_key, _, model = provider["name"].partition(":")
        tool = tool_names.get(provider_key)
        # Sprint 4: never START a provider call that cannot finish inside the request deadline. Not a provider fault, so the
        # breaker is not touched; the request fails fast instead of pinning a worker thread.
        if not ai_guard.can_start_call():
            _note(attempts, ProviderAttempt(name=provider["name"], error="deadline_exceeded", task=task),
                  attempt_no=attempt_no, retry_index=0, tool_name=tool)
            observability_metrics.record_ai_deadline()
            break
        breaker = ai_guard.get_breaker(provider_key)
        if not breaker.allow():
            # Provider is tripped: skip without a network call (and without counting another failure).
            _note(attempts, ProviderAttempt(name=provider["name"], error="circuit_open", task=task),
                  attempt_no=attempt_no, retry_index=0, tool_name=tool)
            observability_metrics.record_circuit_skip(provider_key)
            continue
        retries_left = int(getattr(_cfg, "LLM_TRANSIENT_RETRIES", 0))
        try_no = 0
        while True:
            if not ai_guard.consume_attempt():
                _note(attempts, ProviderAttempt(name=provider["name"], error="attempt_limit", task=task),
                      attempt_no=attempt_no, retry_index=try_no, tool_name=tool)
                stop = True
                break
            attempt_no += 1
            t0 = time.monotonic()
            try:
                raw = provider["call"](system_prompt, user_prompt)
                latency = (time.monotonic() - t0) * 1000
                text = raw.text if isinstance(raw, LLMResult) else raw
                if isinstance(text, str) and text.strip():
                    breaker.record_success()
                    _note(attempts, ProviderAttempt(
                        name=provider["name"], latency_ms=latency, task=task,
                        usage=usage_for_call(provider["name"], system_prompt, user_prompt, text, raw)),
                        attempt_no=attempt_no, retry_index=try_no, tool_name=tool)
                    log_attempt(provider_key, model, attempt_no, latency, "success")
                    failures = [a for a in attempts if a.error]
                    if failures:
                        record_ops_event("llm_fallback", "warning", {
                            "provider": provider_key, "model": model, "count": len(failures),
                            "reason": failures[0].error, "attempts": attempt_no})
                    return text, provider["name"], attempts
                breaker.record_failure()
                observability_metrics.record_provider_failure(provider_key, "empty")
                _note(attempts, ProviderAttempt(name=provider["name"], error="empty_response", latency_ms=latency, task=task),
                      attempt_no=attempt_no, retry_index=try_no, tool_name=tool)
                log_attempt(provider_key, model, attempt_no, latency, "failure", "empty_response")
                break
            except Exception as e:  # noqa: BLE001 - deliberately broad, this is a fallback boundary
                latency = (time.monotonic() - t0) * 1000
                code = ai_guard.classify_provider_error(e)
                breaker.record_failure()
                observability_metrics.record_provider_failure(provider_key, code)
                _note(attempts, ProviderAttempt(name=provider["name"], error=code, latency_ms=latency, task=task),
                      attempt_no=attempt_no, retry_index=try_no, tool_name=tool)
                log_attempt(provider_key, model, attempt_no, latency, "failure", code)
                if (code in ai_guard.TRANSIENT_ERROR_CODES and try_no < retries_left
                        and breaker.allow() and ai_guard.can_start_call()):
                    try_no += 1
                    delay = float(getattr(_cfg, "LLM_RETRY_BACKOFF_SECONDS", 0.0)) * (2 ** (try_no - 1))
                    left = ai_guard.remaining()
                    if left is not None:
                        delay = max(0.0, min(delay, left - _cfg.AI_MIN_CALL_BUDGET_SECONDS))
                    if delay:
                        time.sleep(delay)
                    continue
                break

    failure_summary = "; ".join(f"{a.name}: {a.error}" for a in attempts)
    last = next((a.error for a in reversed(attempts) if a.error), "error")
    record_ops_event("llm_all_providers_failed", "critical", {
        "providers_total": len(chain), "count": len(attempts), "reason": last, "attempts": attempt_no})
    raise ProviderError(f"All configured LLM providers failed. {failure_summary}")
