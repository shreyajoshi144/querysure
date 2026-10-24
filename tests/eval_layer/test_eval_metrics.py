"""Metric arithmetic on hand-built results, so every denominator is pinned."""
from __future__ import annotations

import pytest

from evaluation.metrics import check_thresholds, compute_metrics, percentile, rate
from evaluation.runner import CaseResult


def mk(id="x-001", behavior="answer", **kw):
    base = dict(id=id, category="aggregation" if behavior == "answer" else "impossible", difficulty="easy",
                question=id, expected_behavior=behavior, decline_reason=None if behavior == "answer" else "nonexistent_data")
    base.update(kw)
    return CaseResult(**base)


def correct(id, **kw):
    kw = {"sql": "SELECT 1", "sql_parseable": True, "validator_valid": True, "execution_success": True,
          "pipeline_status": "executed", "verdict": "correct", "result_match": True, "result_match_kind": "exact",
          "table_grounded": True, "table_exact": True, "identifier_refs": 4, "latency_ms": {"total": 10.0}, **kw}
    return mk(id, **kw)


def test_rate_returns_none_for_empty_denominator_never_zero():
    assert rate(0, 0) is None
    assert rate(0, 5) == 0.0
    assert rate(1, 3) == 0.3333


def test_percentile_nearest_rank():
    assert percentile([], 95) is None
    vals = list(range(1, 21))            # 1..20
    assert percentile(vals, 95) == 19     # ceil(0.95*20) = 19th value
    assert percentile(vals, 50) == 10
    assert percentile([7], 95) == 7


def test_answerable_denominators_include_cases_that_produced_no_sql():
    results = [
        correct("a-001"),
        mk("a-002", pipeline_status="no_sql", verdict="incorrect", failure_mode="no_sql"),
        mk("a-003", pipeline_status="validator_rejected", verdict="incorrect", failure_mode="validator_rejected",
           sql="SELECT *", sql_parseable=True, validator_valid=False),
        mk("a-004", pipeline_status="execution_error", verdict="incorrect", failure_mode="execution_error",
           sql="SELECT 2", sql_parseable=True, validator_valid=True),
        mk("a-005", pipeline_status="executed", verdict="incorrect", failure_mode="result_mismatch", sql="SELECT 3",
           sql_parseable=True, validator_valid=True, execution_success=True, result_match=False),
    ]
    a = compute_metrics(results)["answerable"]
    assert a["n"] == 5
    assert a["sql_produced_rate"] == 0.8               # 4/5
    assert a["sql_validity_rate"] == 0.6               # a-001, a-004, a-005
    assert a["execution_success_rate"] == 0.4          # a-001, a-005
    assert a["execution_accuracy"] == 0.2              # a-001 only
    assert a["execution_accuracy_given_executed"] == 0.5   # 1 of the 2 that executed
    assert a["over_rejection_rate"] == 0.4             # no_sql + validator_rejected


def test_grounding_and_optional_rates_use_only_applicable_cases():
    results = [
        correct("a-001", table_grounded=True, columns_grounded=True, characteristics_match=True),
        correct("a-002", table_grounded=False, columns_grounded=False, characteristics_match=None),
        correct("a-003", table_grounded=True, columns_grounded=None, characteristics_match=None),
    ]
    a = compute_metrics(results)["answerable"]
    assert a["table_grounding_rate"] == round(2 / 3, 4)
    assert a["column_grounding_rate"] == 0.5          # 1 of the 2 that declared columns
    assert a["characteristics_match_rate"] == 1.0     # only a-001 declared characteristics
    assert compute_metrics([correct("a-004")])["answerable"]["column_grounding_rate"] is None


def test_hallucination_metrics_and_validator_escape():
    results = [
        correct("a-001"),
        mk("a-002", sql="SELECT bad FROM t", sql_parseable=True, hallucination=True, hallucinated_columns=["bad"],
           hallucinated_identifier_count=1, identifier_refs=5, validator_valid=False, pipeline_status="validator_rejected",
           failure_mode="validator_rejected"),
        mk("a-003", sql="SELECT bad2 FROM t", sql_parseable=True, hallucination=True, hallucinated_columns=["bad2"],
           hallucinated_identifier_count=1, identifier_refs=5, validator_valid=True, pipeline_status="executed",
           failure_mode="result_mismatch"),   # validator missed it
        mk("a-004", pipeline_status="no_sql", failure_mode="no_sql"),      # no SQL: not in the denominator
        mk("a-005", sql="SELEC x", sql_parseable=False, pipeline_status="validator_rejected", failure_mode="validator_rejected"),
    ]
    h = compute_metrics(results)["hallucination"]
    assert h["cases_with_sql"] == 4
    assert h["unparseable_sql_cases"] == 1
    assert h["case_level_rate"] == 0.5                       # 2 of 4
    assert h["identifier_level_rate"] == round(2 / 14, 4)    # 2 bad of (4+5+5) refs in parseable SQL
    assert h["validator_escape_rate"] == 0.5                 # 1 of the 2 hallucinating passed the validator
    assert h["executed_despite_hallucination"] == 1
    assert h["hallucinated_columns"] == 2


def test_hallucination_rates_are_none_when_no_sql_at_all():
    h = compute_metrics([mk("a-001", pipeline_status="no_sql", failure_mode="no_sql")])["hallucination"]
    assert h["case_level_rate"] is None and h["identifier_level_rate"] is None and h["validator_escape_rate"] is None


def test_rejection_metrics_strict_vs_lenient_and_by_reason():
    results = [
        mk("d-001", "reject", pipeline_status="no_sql", verdict="correct", lenient_correct=True),
        mk("d-002", "reject", pipeline_status="validator_rejected", verdict="correct", lenient_correct=True),
        mk("d-003", "reject", pipeline_status="execution_error", verdict="incorrect", lenient_correct=True, failure_mode="execution_error"),
        mk("d-004", "reject", pipeline_status="executed", verdict="incorrect", failure_mode="answered_instead_of_declining"),
        mk("d-005", "reject", pipeline_status="generation_error", verdict="incorrect", failure_mode="generation_error"),
    ]
    r = compute_metrics(results)["rejection"]
    assert r["n"] == 5
    assert r["strict_accuracy"] == 0.4
    assert r["lenient_accuracy"] == 0.6
    assert r["answered_instead_rate"] == 0.2
    assert r["declined_by_refusal"] == 1 and r["declined_by_validator"] == 1
    assert r["by_reason"]["nonexistent_data"]["n"] == 5


def test_rejection_metrics_none_without_decline_cases():
    r = compute_metrics([correct("a-001")])["rejection"]
    assert r["strict_accuracy"] is None and r["lenient_accuracy"] is None


def test_confidence_signal():
    results = [
        correct("a-001", confidence="Low"), correct("a-002", confidence="Medium"),
        correct("a-003", confidence="Low"), correct("a-004", confidence="High"),
        mk("d-001", "reject", pipeline_status="executed", verdict="incorrect",
           failure_mode="answered_instead_of_declining", confidence="Low"),
    ]
    cs = compute_metrics(results)["confidence_signal"]
    assert cs["low_on_correct_answers"] == 0.5
    assert cs["low_on_wrongly_answered_decline_cases"] == 1.0


def test_latency_and_cost_offline_vs_live():
    rs = [correct(f"a-00{i}", latency_ms={"total": float(i * 10), "retrieval": 1.0}) for i in range(1, 5)]
    off = compute_metrics(rs, calls_llm=False)
    assert off["latency_ms"]["mean"] == 25.0 and off["latency_ms"]["median"] == 25.0
    assert off["latency_ms"]["max"] == 40.0 and off["latency_ms"]["includes_real_llm_time"] is False
    assert off["cost"]["applicable"] is False
    for r, c in zip(rs, (0.5, 0.25, 0.0, 0.25)):
        r.estimated_cost_cents = c
    live = compute_metrics(rs, calls_llm=True)
    assert live["cost"]["applicable"] and live["cost"]["estimated_total_usd"] == 0.01
    assert live["cost"]["estimated_mean_usd_per_query"] == 0.0025
    assert live["latency_ms"]["includes_real_llm_time"] is True


def test_breakdowns_and_failure_modes():
    rs = [correct("a-001"), mk("a-002", pipeline_status="no_sql", verdict="incorrect", failure_mode="no_sql"),
          mk("d-001", "reject", verdict="correct", pipeline_status="no_sql")]
    rs[1].difficulty = "hard"
    m = compute_metrics(rs)
    assert m["failure_modes"] == {"correct": 2, "no_sql": 1}
    assert m["by_difficulty"]["hard"] == {"n": 1, "pass_rate": 0.0, "execution_success_rate": 0.0}
    assert m["by_category"]["impossible"]["pass_rate"] == 1.0
    assert m["overall"]["behavior_accuracy"] == round(2 / 3, 4)


# ---- thresholds -----------------------------------------------------------

def _m(**over):
    m = {"answerable": {"execution_accuracy": 0.8, "sql_validity_rate": 0.9},
         "overall": {"behavior_accuracy": 0.85}, "rejection": {"strict_accuracy": 0.7},
         "hallucination": {"case_level_rate": 0.05}}
    return m


def test_thresholds_pass_and_fail():
    assert check_thresholds(_m(), {"min_execution_accuracy": 0.8, "max_hallucination_rate": 0.05}) == []
    fails = check_thresholds(_m(), {"min_execution_accuracy": 0.81, "max_hallucination_rate": 0.04,
                                    "min_rejection_accuracy": 0.7, "min_sql_validity": None})
    assert len(fails) == 2 and "execution_accuracy" in fails[0] and "case_level_rate" in fails[1]


def test_unmeasurable_metric_fails_its_threshold_instead_of_passing():
    m = _m()
    m["hallucination"]["case_level_rate"] = None
    fails = check_thresholds(m, {"max_hallucination_rate": 0.5})
    assert len(fails) == 1 and "could not be computed" in fails[0]


def test_unknown_threshold_name_is_an_error():
    with pytest.raises(KeyError):
        check_thresholds(_m(), {"min_vibes": 0.5})
