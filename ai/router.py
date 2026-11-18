"""Task-aware model routing: which providers/models to try, in which order, for one kind of AI task.

Default (no route for the task): every provider with a key, in ``LLM_PROVIDER_ORDER`` -- exactly the pre-Sprint-2 chain.
``AI_TASK_ROUTES`` (or ``route_override`` in evaluation code) can pin a task to an explicit ordered list of
``provider[:model]`` targets; only those are tried. The current task is a context variable, so callers mark a scope
(``with task_scope("sql_repair"): ...``) instead of threading a parameter through every function.

Callers supply ``ProviderSettings`` (keys + configured models) and ``transports`` (provider -> callable). That keeps this
module free of global state the SQL generator's tests patch, and lets a future task use different settings.
"""
from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Tuple

import config as _cfg

DEFAULT_TASK = "sql_generation"
TASK_DESCRIPTIONS = {
    "sql_generation": "Write SQL for a business question",
    "sql_repair": "Repair SQL after a diagnosed validation or result problem",
    "knowledge_answer": "Answer a question from retrieved workspace documents, with citations",
    "agent_plan": "Turn a workspace goal into a short plan of registered tool calls (no tool output is fed back)",
}
assert set(TASK_DESCRIPTIONS) == set(_cfg.AI_TASKS), "config.AI_TASKS and router task descriptions must match"

_task: ContextVar[str] = ContextVar("qs_ai_task", default=DEFAULT_TASK)
_override: ContextVar[Optional[Dict[str, List[Tuple[str, str]]]]] = ContextVar("qs_ai_route_override", default=None)

Transport = Callable[[str, str, str, str], object]   # (model, api_key, system_prompt, user_prompt) -> LLMResult | str


@dataclass
class ProviderSettings:
    keys: Dict[str, str] = field(default_factory=dict)            # provider -> API key ("" = not configured)
    models: Dict[str, List[str]] = field(default_factory=dict)    # provider -> configured model ids, in order


@contextlib.contextmanager
def task_scope(task: str) -> Iterator[str]:
    if task not in TASK_DESCRIPTIONS:
        raise ValueError(f"unknown AI task {task!r}")
    token = _task.set(task)
    try:
        yield task
    finally:
        _task.reset(token)


def current_task() -> str:
    return _task.get()


@contextlib.contextmanager
def route_override(routes: Dict[str, List[Tuple[str, str]]]) -> Iterator[None]:
    """Force routes for some tasks (side-by-side model evaluation). Same format as config.AI_TASK_ROUTES."""
    token = _override.set(routes)
    try:
        yield
    finally:
        _override.reset(token)


def routes_for(task: str) -> Optional[List[Tuple[str, str]]]:
    override = _override.get()
    if override and task in override:
        return override[task]
    return (getattr(_cfg, "AI_TASK_ROUTES", None) or {}).get(task)


def _entry(provider: str, model: str, key: str, transport: Transport) -> dict:
    return {"name": f"{provider}:{model}", "provider": provider, "model": model,
            "call": lambda sp, up: transport(model, key, sp, up)}


def build_chain(settings: ProviderSettings, transports: Dict[str, Transport], task: Optional[str] = None) -> List[dict]:
    """Ordered ``[{"name", "provider", "model", "call"}]`` for ``task``. Providers without a key are skipped."""
    task = task or current_task()
    routes = routes_for(task)
    chain: List[dict] = []
    if routes:
        for provider, model in routes:
            key = (settings.keys.get(provider) or "").strip()
            if not key or provider not in transports:
                continue
            for m in ([model] if model else settings.models.get(provider, [])):
                chain.append(_entry(provider, m, key, transports[provider]))
    else:
        order = list(getattr(_cfg, "LLM_PROVIDER_ORDER", None) or ["groq", "openai", "anthropic"])
        order += [p for p in _cfg._KNOWN_LLM_PROVIDERS if p not in order]
        for provider in order:
            key = (settings.keys.get(provider) or "").strip()
            if not key or provider not in transports:
                continue
            for m in settings.models.get(provider, []):
                chain.append(_entry(provider, m, key, transports[provider]))
    seen, unique = set(), []
    for e in chain:
        if e["name"] not in seen:
            seen.add(e["name"])
            unique.append(e)
    return unique


def describe(chain: List[dict]) -> List[dict]:
    """Admin/status view: provider + model + position. Never includes keys."""
    return [{"position": i + 1, "provider": e["name"].partition(":")[0], "model": e["name"].partition(":")[2]}
            for i, e in enumerate(chain)]
