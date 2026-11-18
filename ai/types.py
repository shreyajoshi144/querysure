"""Value types shared by every AI provider call (moved unchanged from modules/sql_generator.py in Sprint 2)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ProviderAttempt:
    name: str
    error: Optional[str] = None
    # Real provider-reported token usage for a successful call, or an estimate (estimated=True).
    usage: Optional[dict] = None
    # Sprint 2 trace fields (optional, so existing constructors keep working).
    latency_ms: Optional[float] = None
    task: Optional[str] = None


@dataclass
class LLMResult:
    """What a provider transport returns. Plain ``str`` is still accepted from transports (tests
    and custom adapters) and is accounted with the chars/4 heuristic, flagged ``estimated``."""
    text: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


class ProviderError(Exception):
    pass
