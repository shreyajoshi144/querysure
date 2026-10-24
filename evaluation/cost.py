"""
Estimated LLM cost.

The existing project does not measure real token usage: generate_sql()
returns no `usage` block, and api.py budgets spend with a character-count
heuristic (api._estimate_cost_cents). To stay consistent with what
QuerySure itself would record against a user's daily budget, this module
reproduces that exact formula. A parity test
(tests/eval_layer/test_cost.py) fails if api.py's version ever diverges.

Importing api.py here would drag in FastAPI, the ORM and auth for a
one-line formula, so the formula is mirrored rather than imported.

IMPORTANT: this is *budget-accounting* cost, not a provider invoice. See the
README ("Cost") for caveats.
"""
from __future__ import annotations

# Mirrors api._EST_COST_PER_1K_TOKENS_CENTS.
EST_COST_PER_1K_TOKENS_CENTS = 0.15


def estimate_cost_cents(question: str, retrieval: dict | None, result: dict) -> float:
    usage = result.get("usage") if isinstance(result, dict) else None
    if isinstance(usage, dict) and usage.get("cost_cents") is not None:  # mirrors api.py (Sprint 3)
        return max(0.0001, float(usage["cost_cents"]))
    if isinstance(usage, dict) and (usage.get("total_tokens") or usage.get("input_tokens")):
        total_tokens = usage.get("total_tokens") or (
            (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
        )
        return max(0.0001, total_tokens / 1000 * EST_COST_PER_1K_TOKENS_CENTS * 100)
    context_text = retrieval.get("retrieved_context_text", "") if isinstance(retrieval, dict) else ""
    approx_chars = len(question or "") + len(context_text or "") + len(str((result or {}).get("sql", "") or ""))
    approx_tokens = max(1, approx_chars / 4)
    return max(0.0001, approx_tokens / 1000 * EST_COST_PER_1K_TOKENS_CENTS * 100)
