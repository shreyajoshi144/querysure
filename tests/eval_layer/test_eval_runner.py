"""End-to-end behaviour of the harness against QuerySure's real modules."""
from __future__ import annotations

import hashlib
import json

import pytest

import config
import modules
from modules import sql_generator
from evaluation.harness import run_evaluation
from evaluation.models import DatasetError, parse_benchmark
from evaluation.runner import run_cases
from evaluation.sources import CredentialsMissing, ScriptedResponse, ScriptedSource, build_oracle_source

from evaluation.harness import SYNTHETIC_SPEC_PATH as SYNTHETIC_FIXTURE

from .conftest import by_id


# ---------------------------------------------------------------------------
# Oracle: harness + dataset self-consistency
# ---------------------------------------------------------------------------

def test_oracle_run_is_perfect_through_the_real_pipeline(oracle_report):
    m = oracle_report["metrics"]
    assert m["overall"]["cases"] == 62
    assert m["overall"]["behavior_accuracy"] == 1.0
    a = m["answerable"]
    for key in ("sql_validity_rate", "execution_success_rate", "execution_accuracy", "table_grounding_rate",
                "table_exact_match_rate", "column_grounding_rate", "characteristics_match_rate"):
        assert a[key] == 1.0, key
    assert m["hallucination"]["case_level_rate"] == 0.0
    assert m["rejection"]["strict_accuracy"] == 1.0
    assert m["failure_modes"] == {"correct": 62}


def test_oracle_report_labels_itself_as_a_self_check(oracle_report):
    text = " ".join(oracle_report["meta"]["warnings"])
    assert "no LLM was called" in text and "self-consistency" in text
    assert oracle_report["meta"]["source"]["calls_llm"] is False
    assert oracle_report["metrics"]["cost"]["applicable"] is False


def test_report_is_json_serialisable_and_versioned(oracle_report):
    round_tripped = json.loads(json.dumps(oracle_report))
    assert round_tripped["report_version"] == 1
    assert round_tripped["meta"]["dataset"]["cases_run"] == 62
    assert round_tripped["meta"]["database"]["matches_reference"] is True


def test_latency_is_recorded_per_stage_and_excludes_gold_computation(oracle_report):
    c = oracle_report["cases"][0]
    assert {"retrieval", "generation", "validation", "execution", "total"} <= set(c["latency_ms"])
    assert c["latency_ms"]["total"] >= c["latency_ms"]["generation"] > 0
    # decline cases stop before validation/execution
    d = by_id(oracle_report)["adv-001"]
    assert "validation" not in d["latency_ms"] and "execution" not in d["latency_ms"]


def test_runs_are_deterministic_apart_from_timing(eval_db):
    def strip(report):
        r = json.loads(json.dumps(report))
        r["meta"].pop("generated_at")
        r["metrics"].pop("latency_ms")
        for c in r["cases"]:
            c.pop("latency_ms")
        return r
    a = run_evaluation(mode="synthetic", db_path=eval_db)
    b = run_evaluation(mode="synthetic", db_path=eval_db)
    assert strip(a) == strip(b)


# ---------------------------------------------------------------------------
# No LLM, no network for the deterministic suite
# ---------------------------------------------------------------------------

def test_offline_modes_never_touch_a_real_provider(eval_db, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a real LLM provider was called in an offline mode")
    for name in ("_call_groq", "_call_openai", "_call_anthropic"):
        monkeypatch.setattr(sql_generator, name, boom)
    for mode in ("oracle", "synthetic"):
        rep = run_evaluation(mode=mode, db_path=eval_db, limit=20)
        assert rep["meta"]["source"]["calls_llm"] is False


def test_scripted_transport_is_restored_after_the_run(eval_db):
    original = sql_generator._run_provider_chain
    run_evaluation(mode="oracle", db_path=eval_db, limit=3)
    assert sql_generator._run_provider_chain is original


def test_scripted_transport_is_restored_even_if_a_run_crashes(eval_db, benchmark, env):
    original = sql_generator._run_provider_chain
    cases = list(benchmark.cases[:2])
    src = build_oracle_source(cases)
    with pytest.raises(RuntimeError, match="kaboom"):
        with src.activate():
            raise RuntimeError("kaboom")
    assert sql_generator._run_provider_chain is original


def test_real_generate_sql_still_does_the_parsing(oracle_report):
    """The oracle feeds *text* to the real generate_sql, which must extract the SQL itself."""
    c = by_id(oracle_report)["agg-002"]
    assert json.loads(c["raw_response"])["sql"] == "SELECT COUNT(*) AS total_orders FROM orders"
    assert c["sql"] == "SELECT COUNT(*) AS total_orders FROM orders"
    assert c["provider"] == "scripted:oracle"


def test_source_database_is_never_modified(eval_db):
    def digest():
        with open(eval_db, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    before = digest()
    run_evaluation(mode="synthetic", db_path=eval_db)   # includes UPDATE / DROP attempts
    assert digest() == before


def test_destructive_sql_never_reaches_the_executor(eval_db, monkeypatch):
    seen = []
    real = modules.execute_query

    def spy(sql, *a, **k):
        seen.append(sql)
        return real(sql, *a, **k)
    monkeypatch.setattr(modules, "execute_query", spy)
    rep = run_evaluation(mode="synthetic", db_path=eval_db)
    assert seen, "spy was not exercised"
    blob = "\n".join(seen).upper()
    # SELECT * is a read-only projection (accepted since v1.0.8); only writes must never reach the executor
    for forbidden in ("DROP TABLE", "UPDATE PRODUCTS", "DELETE FROM"):
        assert forbidden not in blob, forbidden
    cases = by_id(rep)
    assert cases["adv-006"]["pipeline_status"] == "validator_rejected"
    assert cases["adv-003"]["pipeline_status"] == "validator_rejected"


# ---------------------------------------------------------------------------
# Synthetic defects: every injected failure mode is detected and labelled
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case_id", ["join-007", "grp-002", "agg-001"])
def test_equivalent_sql_written_differently_is_scored_correct(synthetic_report, case_id):
    c = by_id(synthetic_report)[case_id]
    fixture = json.load(open(SYNTHETIC_FIXTURE))["overrides"][case_id]
    assert "equivalent SQL" in fixture["_label"]
    assert c["sql"] == fixture["sql"] != ""
    assert c["verdict"] == "correct" and c["result_match"] is True


@pytest.mark.parametrize("case_id, mode", [
    ("filter-002", "result_mismatch"),     # wrong filter threshold
    ("join-004", "result_mismatch"),       # wrong metric
    ("date-001", "result_mismatch"),       # wrong year
    ("multi-001", "result_mismatch"),      # wrong ranking direction
    ("filter-001", "result_mismatch"),     # extra column: strict mismatch
    ("agg-002", "result_mismatch"),        # SELECT * runs (read-only) but returns every row, not the aggregate
    ("date-002", "validator_rejected"),    # syntax error
    ("grp-001", "generation_error"),       # provider outage
    ("date-006", "validator_rejected"),    # unknown SQL function: since v1.0.5 the SQLite compile check rejects it pre-execution
])
def test_injected_answerable_failures_are_classified(synthetic_report, case_id, mode):
    c = by_id(synthetic_report)[case_id]
    assert c["verdict"] == "incorrect" and c["failure_mode"] == mode


def test_extra_columns_are_flagged_as_superset_not_silently_accepted(synthetic_report):
    c = by_id(synthetic_report)["filter-001"]
    assert c["result_match"] is False and c["result_match_kind"] == "superset_columns"
    a = synthetic_report["metrics"]["answerable"]
    assert a["execution_accuracy_superset_columns"] > a["execution_accuracy"]


def test_ranking_direction_error_is_caught_by_ordered_comparison(synthetic_report):
    c = by_id(synthetic_report)["multi-001"]
    assert c["result_match"] is False   # ASC returns the 5 *lowest* products; the values differ too


@pytest.mark.parametrize("case_id, tables, columns", [
    ("agg-003", [], ["unit_cost"]),
    ("grp-003", ["product_categories"], []),
    ("join-001", [], ["orders.store_name"]),
    ("imp-001", ["employees"], []),
    ("imp-002", [], ["customers.phone_number"]),
])
def test_hallucinated_identifiers_are_detected_independently_of_the_validator(synthetic_report, case_id, tables, columns):
    c = by_id(synthetic_report)[case_id]
    assert c["hallucination"] is True
    assert c["hallucinated_tables"] == tables and c["hallucinated_columns"] == columns
    assert "ast" in c["hallucination_sources"] and "sqlite" in c["hallucination_sources"]


def test_hallucination_rates_and_zero_validator_escapes(synthetic_report):
    h = synthetic_report["metrics"]["hallucination"]
    assert h["cases_with_hallucination"] == 5
    assert h["case_level_rate"] == round(5 / h["cases_with_sql"], 4)
    assert h["validator_escape_rate"] == 0.0        # QuerySure's validator caught every one
    assert h["executed_despite_hallucination"] == 0
    assert 0 < h["identifier_level_rate"] < h["case_level_rate"]


@pytest.mark.parametrize("case_id", ["imp-001", "imp-002", "adv-003", "adv-006"])
def test_should_decline_cases_blocked_by_the_validator_count_as_correct(synthetic_report, case_id):
    c = by_id(synthetic_report)[case_id]
    assert c["pipeline_status"] == "validator_rejected" and c["verdict"] == "correct"


@pytest.mark.parametrize("case_id", ["amb-001", "amb-002", "adv-005", "imp-003"])
def test_should_decline_cases_that_return_data_are_failures(synthetic_report, case_id):
    c = by_id(synthetic_report)[case_id]
    assert c["pipeline_status"] == "executed"
    assert c["verdict"] == "incorrect" and c["failure_mode"] == "answered_instead_of_declining"
    assert c["lenient_correct"] is False


def test_provider_outage_is_not_credited_as_a_correct_rejection(eval_db, benchmark):
    decline = [c for c in benchmark.cases if c.should_decline][:3]
    src = ScriptedSource("outage", {c.id: ScriptedResponse(provider_error="all providers down") for c in decline})
    rep = run_evaluation(mode="synthetic", db_path=eval_db, ids=[c.id for c in decline], source=src)
    assert {c["failure_mode"] for c in rep["cases"]} == {"generation_error"}
    assert rep["metrics"]["rejection"]["strict_accuracy"] == 0.0
    assert rep["metrics"]["rejection"]["lenient_accuracy"] == 0.0


def test_missing_scripted_response_is_a_generation_error_not_a_crash(eval_db):
    rep = run_evaluation(mode="synthetic", db_path=eval_db, ids=["agg-001"],
                         source=ScriptedSource("empty", {}))
    assert rep["cases"][0]["pipeline_status"] == "generation_error"
    assert "no response for case" in rep["cases"][0]["error"]


def test_refusal_text_is_not_parsed_as_sql(oracle_report):
    for cid in ("amb-001", "imp-001", "adv-001"):
        c = by_id(oracle_report)[cid]
        assert c["sql"] == "" and c["pipeline_status"] == "no_sql"


def test_rejection_breakdown_by_reason_in_synthetic(synthetic_report):
    by_reason = synthetic_report["metrics"]["rejection"]["by_reason"]
    assert by_reason["adversarial"]["strict_accuracy"] == 1.0
    assert by_reason["invalid_input"]["strict_accuracy"] == 0.0
    assert by_reason["ambiguous"]["strict_accuracy"] == 0.6


# ---------------------------------------------------------------------------
# Guardrails on the benchmark itself
# ---------------------------------------------------------------------------

def test_a_broken_gold_query_aborts_instead_of_silently_failing_every_candidate(env):
    bad = parse_benchmark(json.dumps({"version": "t", "cases": [{
        "id": "bad-001", "category": "aggregation", "difficulty": "easy", "question": "count things",
        "expected_behavior": "answer", "gold_sql": "SELECT COUNT(ghost) FROM orders", "expected_tables": ["orders"]}]}))
    with pytest.raises(DatasetError, match="gold SQL is rejected"):
        run_cases(bad.cases, build_oracle_source(bad.cases), env)


def test_filters_and_empty_selection(eval_db):
    rep = run_evaluation(mode="oracle", db_path=eval_db, categories=["joins"], difficulties=["easy"])
    assert rep["meta"]["dataset"]["cases_run"] == 2
    assert any("Subset run" in w for w in rep["meta"]["warnings"])
    with pytest.raises(DatasetError, match="no benchmark cases match"):
        run_evaluation(mode="oracle", db_path=eval_db, categories=["joins"], ids=["agg-001"])


def test_replay_mode_requires_a_responses_file(eval_db):
    with pytest.raises(DatasetError, match="--responses"):
        run_evaluation(mode="replay", db_path=eval_db)
    with pytest.raises(DatasetError, match="unknown mode"):
        run_evaluation(mode="telepathy", db_path=eval_db)


# ---------------------------------------------------------------------------
# Live mode: credentials gating, exercised with a mocked transport (no network)
# ---------------------------------------------------------------------------

@pytest.fixture()
def no_credentials(monkeypatch):
    for mod in (config, sql_generator):
        for name in ("GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, "")

    def boom(*a, **k):
        raise AssertionError("network provider called")
    for name in ("_call_groq", "_call_openai", "_call_anthropic"):
        monkeypatch.setattr(sql_generator, name, boom)


def test_live_mode_without_credentials_fails_fast_and_calls_nothing(eval_db, no_credentials):
    with pytest.raises(CredentialsMissing, match="No LLM credentials"):
        run_evaluation(mode="live", db_path=eval_db, limit=2)


def _mock_groq(monkeypatch, answers: dict, fail_ids=()):
    """Stand-in for the Groq transport; keyed off the question text in the prompt."""
    calls = []

    def fake(model, api_key, system_prompt, user_prompt):
        calls.append(model)
        for question, sql in answers.items():
            if f"BUSINESS QUESTION:\n{question}" in user_prompt:
                if sql is None:
                    raise RuntimeError("simulated provider failure")
                return json.dumps({"sql": sql, "explanation": "ok", "confidence": "High", "complexity": "Simple"})
        raise AssertionError("unexpected question in prompt")
    monkeypatch.setattr(sql_generator, "_call_groq", fake)
    return calls


def test_live_mode_runs_the_real_provider_chain_and_estimates_cost(eval_db, benchmark, no_credentials, monkeypatch):
    q = {c.id: c for c in benchmark.cases}
    answers = {
        q["agg-002"].question: q["agg-002"].gold_sql,                    # right
        q["agg-003"].question: "SELECT AVG(cost_price) FROM products",   # wrong column, real, wrong result
        q["imp-001"].question: "SELECT AVG(salary) FROM employees",      # hallucination on an impossible question
    }
    calls = _mock_groq(monkeypatch, answers)
    rep = run_evaluation(mode="live", db_path=eval_db, api_key="test-key", ids=["agg-002", "agg-003", "imp-001"])
    assert calls and set(calls) <= {config.GROQ_MODEL}          # primary model served every call
    assert rep["meta"]["source"]["calls_llm"] is True
    assert not any("no LLM was called" in w for w in rep["meta"]["warnings"])
    cases = by_id(rep)
    assert cases["agg-002"]["verdict"] == "correct" and cases["agg-002"]["provider"].startswith("groq:")
    assert cases["agg-003"]["failure_mode"] == "result_mismatch"
    assert cases["imp-001"]["hallucination"] and cases["imp-001"]["verdict"] == "correct"   # validator blocked it
    cost = rep["metrics"]["cost"]
    assert cost["applicable"] and cost["estimated_total_usd"] > 0
    assert all(c["estimated_cost_cents"] > 0 for c in rep["cases"])
    assert rep["metrics"]["latency_ms"]["includes_real_llm_time"] is True


def test_live_provider_failure_costs_nothing_and_is_a_generation_error(eval_db, benchmark, no_credentials, monkeypatch):
    q = next(c for c in benchmark.cases if c.id == "adv-001")
    _mock_groq(monkeypatch, {q.question: None})
    rep = run_evaluation(mode="live", db_path=eval_db, api_key="test-key", ids=["adv-001"])
    c = rep["cases"][0]
    assert c["pipeline_status"] == "generation_error" and c["failure_mode"] == "generation_error"
    assert c["estimated_cost_cents"] == 0.0
    assert rep["metrics"]["rejection"]["strict_accuracy"] == 0.0


def test_recorded_live_responses_replay_offline_identically(eval_db, benchmark, no_credentials, monkeypatch, tmp_path):
    from evaluation.harness import record_responses
    q = {c.id: c for c in benchmark.cases}
    ids = ["agg-002", "grp-001", "imp-001"]
    answers = {
        q["agg-002"].question: q["agg-002"].gold_sql,
        q["grp-001"].question: "SELECT region, COUNT(*) FROM stores GROUP BY region",
        q["imp-001"].question: "SELECT AVG(salary) FROM employees",
    }
    _mock_groq(monkeypatch, answers)
    live = run_evaluation(mode="live", db_path=eval_db, api_key="k", ids=ids)
    path = tmp_path / "recorded.json"
    assert record_responses(live, str(path)) == 3

    monkeypatch.setattr(sql_generator, "_call_groq", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")))
    replay = run_evaluation(mode="replay", db_path=eval_db, ids=ids, responses_path=str(path))
    assert replay["meta"]["source"]["calls_llm"] is False
    for a, b in zip(live["cases"], replay["cases"]):
        assert (a["sql"], a["verdict"], a["failure_mode"], a["hallucination"]) == \
               (b["sql"], b["verdict"], b["failure_mode"], b["hallucination"])
