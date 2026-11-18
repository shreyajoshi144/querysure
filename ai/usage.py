"""Token usage and estimated cost per provider call (moved unchanged from modules/sql_generator.py in Sprint 2)."""
from __future__ import annotations

from typing import List

from ai.types import LLMResult, ProviderAttempt
from config import llm_cost_cents


def approx_tokens(*texts: str) -> int:
    return max(1, sum(len(t or "") for t in texts) // 4)


def usage_for_call(provider_name: str, system_prompt: str, user_prompt: str, text: str, raw) -> dict:
    """Token usage + cost for ONE successful provider call. Uses the provider's own numbers when
    present; otherwise falls back to chars/4 and marks the record ``estimated``."""
    model = provider_name.partition(":")[2] or provider_name
    in_tok = getattr(raw, "input_tokens", None) if isinstance(raw, LLMResult) else None
    out_tok = getattr(raw, "output_tokens", None) if isinstance(raw, LLMResult) else None
    estimated = in_tok is None or out_tok is None
    if estimated:
        in_tok = approx_tokens(system_prompt, user_prompt)
        out_tok = approx_tokens(text)
    return {"model": model, "input_tokens": int(in_tok), "output_tokens": int(out_tok),
            "estimated": estimated, "cost_cents": llm_cost_cents(model, int(in_tok), int(out_tok))}


def aggregate_usage(attempts: List[ProviderAttempt]) -> dict:
    """Sum usage over every successful call made for one request (initial + repairs).
    ``estimated`` is True if ANY call had to be estimated."""
    calls = [a.usage for a in attempts if a.usage]
    inp = sum(c["input_tokens"] for c in calls)
    out = sum(c["output_tokens"] for c in calls)
    return {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
            "estimated": any(c["estimated"] for c in calls) if calls else True,
            "cost_cents": sum(c["cost_cents"] for c in calls), "calls": len(calls)}
