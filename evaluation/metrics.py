"""
Aggregate metrics. Every rate documents its denominator, and `None` (never 0)
is returned when a denominator is empty, so "no data" cannot masquerade as
"0% accuracy" or "100% accuracy".

Populations
-----------
  A = answerable cases (expected_behavior == "answer")
  D = cases that should be rejected / clarified
  S = cases (any kind) where the pipeline produced non-empty SQL
  P = the subset of S whose SQL parsed

A-metrics count "no SQL produced" as a failure (an answerable question that
got no query is a miss, not a case to be excluded).
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .runner import (
    STATUS_EXECUTED, STATUS_EXECUTION_ERROR, STATUS_GENERATION_ERROR,
    STATUS_NO_SQL, STATUS_VALIDATOR_REJECTED, CaseResult,
)


def rate(numerator: int, denominator: int) -> Optional[float]:
    return None if denominator == 0 else round(numerator / denominator, 4)


def _count(items: Iterable[Any]) -> int:
    return sum(1 for x in items if x)


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile (no interpolation, no numpy)."""
    if not values:
        return None
    s = sorted(values)
    k = max(1, math.ceil(pct / 100.0 * len(s)))
    return round(s[k - 1], 3)


def _mean(values: Sequence[float]) -> Optional[float]:
    return round(sum(values) / len(values), 3) if values else None


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    return round(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2, 3)


def compute_metrics(results: Sequence[CaseResult], calls_llm: bool = False) -> Dict[str, Any]:
    rs = list(results)
    A = [r for r in rs if r.expected_behavior == "answer"]
    D = [r for r in rs if r.expected_behavior != "answer"]
    S = [r for r in rs if r.sql]
    P = [r for r in S if r.sql_parseable]

    executed_A = [r for r in A if r.pipeline_status == STATUS_EXECUTED]

    # ---- answerable ------------------------------------------------------
    answerable = {
        "n": len(A),
        # of A: how many produced any SQL at all
        "sql_produced_rate": rate(_count(bool(r.sql) for r in A), len(A)),
        # of A: SQL that QuerySure's own validator accepts
        "sql_validity_rate": rate(_count(r.validator_valid for r in A), len(A)),
        # of A: validated SQL that ran without a database error
        "execution_success_rate": rate(_count(r.execution_success for r in A), len(A)),
        # of A: executed AND result set equals the gold result (strict)
        "execution_accuracy": rate(_count(r.verdict == "correct" for r in A), len(A)),
        # same, but tolerates extra columns in the candidate's result
        "execution_accuracy_superset_columns": rate(
            _count(r.result_match_kind in ("exact", "superset_columns") for r in A), len(A)),
        # of A that executed: fraction whose result was right (isolates "wrong answer" from "no answer")
        "execution_accuracy_given_executed": rate(_count(r.result_match for r in executed_A), len(executed_A)),
        # of A: SQL references every expected table
        "table_grounding_rate": rate(_count(r.table_grounded for r in A), len(A)),
        # of A: SQL references exactly the expected table set
        "table_exact_match_rate": rate(_count(r.table_exact for r in A), len(A)),
        # of A that declare expected columns: SQL references all of them
        "column_grounding_rate": _rate_over(A, "columns_grounded"),
        # of A that declare structural expectations: SQL exhibits them
        "characteristics_match_rate": _rate_over(A, "characteristics_match"),
        # of A: retrieval surfaced every expected table (trivially high on a small schema)
        "retrieval_table_recall_rate": _rate_over(A, "retrieval_table_recall"),
        # of A: valid answerable question that the pipeline declined (no SQL / validator block)
        "over_rejection_rate": rate(_count(r.pipeline_status in (STATUS_NO_SQL, STATUS_VALIDATOR_REJECTED) for r in A), len(A)),
    }

    # ---- hallucination ---------------------------------------------------
    hallucinated = [r for r in S if r.hallucination]
    total_refs = sum(r.identifier_refs for r in P)
    total_bad = sum(r.hallucinated_identifier_count for r in S)
    hallucination = {
        "cases_with_sql": len(S),
        "unparseable_sql_cases": len(S) - len(P),
        "cases_with_hallucination": len(hallucinated),
        # of S: SQL references a table/column that does not exist (AST scope check OR SQLite compile)
        "case_level_rate": rate(len(hallucinated), len(S)),
        # of all identifier references in parseable SQL: fraction that do not exist
        "identifier_level_rate": rate(total_bad, total_refs),
        "hallucinated_tables": sum(len(r.hallucinated_tables) for r in S),
        "hallucinated_columns": sum(len(r.hallucinated_columns) for r in S),
        # of hallucinating SQL: fraction QuerySure's validator still marked valid (should be 0)
        "validator_escape_rate": rate(_count(r.validator_valid for r in hallucinated), len(hallucinated)),
        # hallucinating SQL that reached execution (SQLite would reject; should be 0)
        "executed_despite_hallucination": _count(r.pipeline_status == STATUS_EXECUTED for r in hallucinated),
    }

    # ---- rejection / clarification --------------------------------------
    by_reason: Dict[str, Dict[str, Any]] = {}
    for reason in sorted({r.decline_reason for r in D if r.decline_reason}):
        sub = [r for r in D if r.decline_reason == reason]
        by_reason[reason] = {
            "n": len(sub),
            "strict_accuracy": rate(_count(r.verdict == "correct" for r in sub), len(sub)),
            "lenient_accuracy": rate(_count(r.lenient_correct for r in sub), len(sub)),
        }
    rejection = {
        "n": len(D),
        # of D: no SQL produced, or validator blocked it. Outages are NOT credited.
        "strict_accuracy": rate(_count(r.verdict == "correct" for r in D), len(D)),
        # also credits run-time database errors
        "lenient_accuracy": rate(_count(r.lenient_correct for r in D), len(D)),
        # of D: the pipeline returned data instead of declining -- the dangerous failure
        "answered_instead_rate": rate(_count(r.failure_mode == "answered_instead_of_declining" for r in D), len(D)),
        "declined_by_refusal": _count(r.pipeline_status == STATUS_NO_SQL for r in D),
        "declined_by_validator": _count(r.pipeline_status == STATUS_VALIDATOR_REJECTED for r in D),
        "by_reason": by_reason,
    }

    # ---- confidence signal ----------------------------------------------
    # generate_sql() reports confidence High/Medium/Low. Does "Low" actually
    # separate good answers from bad ones? Two numbers answer that:
    correct_A = [r for r in A if r.verdict == "correct"]
    answered_D = [r for r in D if r.failure_mode == "answered_instead_of_declining"]
    is_low = lambda r: (r.confidence or "").lower() == "low"
    confidence_signal = {
        # of correct answerable results: fraction flagged Low anyway (false alarms)
        "low_on_correct_answers": rate(_count(is_low(r) for r in correct_A), len(correct_A)),
        # of should-decline cases that got data back: fraction flagged Low (true flags)
        "low_on_wrongly_answered_decline_cases": rate(_count(is_low(r) for r in answered_D), len(answered_D)),
        "interpretation": "A useful flag is Low on wrong answers and rarely Low on correct ones. If both rates are "
                          "high, the flag is not informative.",
    }

    # ---- latency ---------------------------------------------------------
    totals = [r.latency_ms.get("total", 0.0) for r in rs]
    stage_mean = {}
    for stage in ("retrieval", "generation", "validation", "execution"):
        vals = [r.latency_ms[stage] for r in rs if stage in r.latency_ms]
        stage_mean[stage] = _mean(vals)
    latency = {
        "includes_real_llm_time": calls_llm,
        "mean": _mean(totals), "median": _median(totals),
        "p95": percentile(totals, 95), "max": round(max(totals), 3) if totals else None,
        "stage_mean": stage_mean,
    }

    # ---- cost ------------------------------------------------------------
    if calls_llm:
        costs = [r.estimated_cost_cents for r in rs if r.estimated_cost_cents is not None]
        total_c = sum(costs)
        cost = {
            "applicable": True,
            "basis": "api.py character-count heuristic (no real token usage is exposed by the pipeline)",
            "estimated_total_usd": round(total_c / 100, 6),
            "estimated_mean_usd_per_query": round(total_c / 100 / len(costs), 6) if costs else None,
        }
    else:
        cost = {"applicable": False, "reason": "offline source: no LLM was called, so there is no cost to estimate"}

    # ---- breakdowns ------------------------------------------------------
    failure_modes = Counter(r.failure_mode or "correct" for r in rs)

    def group(key: str) -> Dict[str, Any]:
        buckets: Dict[str, List[CaseResult]] = defaultdict(list)
        for r in rs:
            buckets[getattr(r, key)].append(r)
        out = {}
        for k in sorted(buckets):
            b = buckets[k]
            entry = {
                "n": len(b),
                # answerable buckets: execution accuracy; decline buckets: strict rejection accuracy
                "pass_rate": rate(_count(r.verdict == "correct" for r in b), len(b)),
            }
            a_b = [r for r in b if r.expected_behavior == "answer"]
            if a_b:
                entry["execution_success_rate"] = rate(_count(r.execution_success for r in a_b), len(a_b))
            out[k] = entry
        return out

    return {
        "overall": {
            "cases": len(rs), "answerable_cases": len(A), "decline_cases": len(D),
            # every case judged against its own expectation (answer correctly OR decline correctly)
            "behavior_accuracy": rate(_count(r.verdict == "correct" for r in rs), len(rs)),
        },
        "answerable": answerable,
        "hallucination": hallucination,
        "rejection": rejection,
        "confidence_signal": confidence_signal,
        "latency_ms": latency,
        "cost": cost,
        "failure_modes": dict(sorted(failure_modes.items())),
        "by_category": group("category"),
        "by_difficulty": group("difficulty"),
    }


def _rate_over(rs: Sequence[CaseResult], attr: str) -> Optional[float]:
    """Rate of True among cases where the attribute is not None."""
    pool = [getattr(r, attr) for r in rs if getattr(r, attr) is not None]
    return rate(_count(pool), len(pool))


# ---------------------------------------------------------------------------
# Threshold gating (for CI / regression checks)
# ---------------------------------------------------------------------------

THRESHOLD_SPECS = {
    # name: (path into metrics, direction)
    "min_execution_accuracy": (("answerable", "execution_accuracy"), "min"),
    "min_sql_validity": (("answerable", "sql_validity_rate"), "min"),
    "min_behavior_accuracy": (("overall", "behavior_accuracy"), "min"),
    "min_rejection_accuracy": (("rejection", "strict_accuracy"), "min"),
    "max_hallucination_rate": (("hallucination", "case_level_rate"), "max"),
}


def check_thresholds(metrics: Dict[str, Any], thresholds: Dict[str, Optional[float]]) -> List[str]:
    """Return human-readable failures. A metric that could not be computed
    (None) fails its threshold: an unmeasured metric must not pass a gate."""
    failures: List[str] = []
    for name, limit in thresholds.items():
        if limit is None:
            continue
        if name not in THRESHOLD_SPECS:
            raise KeyError(f"unknown threshold '{name}'")
        path, direction = THRESHOLD_SPECS[name]
        value: Any = metrics
        for p in path:
            value = value[p]
        label = ".".join(path)
        if value is None:
            failures.append(f"{label} could not be computed (no applicable cases) but {name}={limit} was requested")
        elif direction == "min" and value < limit:
            failures.append(f"{label} = {value} is below {name} = {limit}")
        elif direction == "max" and value > limit:
            failures.append(f"{label} = {value} is above {name} = {limit}")
    return failures
