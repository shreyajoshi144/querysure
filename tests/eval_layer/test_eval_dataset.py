"""The benchmark file and the case model."""
from __future__ import annotations

import copy
import json

import pytest

from evaluation.integrity import check_dataset
from evaluation.models import (
    CATEGORIES, DatasetError, parse_benchmark, select_cases,
)


def _case(**over):
    base = {
        "id": "t-001", "category": "aggregation", "difficulty": "easy",
        "question": "How many orders are there?", "expected_behavior": "answer",
        "gold_sql": "SELECT COUNT(*) FROM orders", "expected_tables": ["orders"],
    }
    base.update(over)
    return base


def _parse(*cases):
    return parse_benchmark(json.dumps({"version": "t", "cases": list(cases)}))


# ---- the shipped benchmark ------------------------------------------------

def test_shipped_benchmark_covers_every_required_category(benchmark):
    present = {c.category for c in benchmark.cases}
    assert present == set(CATEGORIES)
    for cat in CATEGORIES:
        assert sum(c.category == cat for c in benchmark.cases) >= 5, cat


def test_shipped_benchmark_mixes_answerable_and_decline_cases(benchmark):
    answer = [c for c in benchmark.cases if not c.should_decline]
    decline = [c for c in benchmark.cases if c.should_decline]
    assert len(answer) >= 40 and len(decline) >= 15
    assert {c.difficulty for c in benchmark.cases} == {"easy", "medium", "hard"}
    assert {c.decline_reason for c in decline} == {"ambiguous", "nonexistent_data", "adversarial", "invalid_input"}


def test_every_answerable_case_declares_expectations(benchmark):
    for c in benchmark.cases:
        if not c.should_decline:
            assert c.gold_sql and c.expected_tables, c.id
        else:
            assert not c.gold_sql and not c.expected_tables, c.id


def test_ordered_cases_are_the_ranking_questions(benchmark):
    ordered = {c.id for c in benchmark.cases if c.ordered}
    assert {"multi-001", "multi-002", "multi-007", "date-008"} <= ordered


def test_shipped_gold_sql_is_sound_against_the_real_database(benchmark, env):
    """Every gold query validates with QuerySure's validator, executes, references
    only real identifiers, touches the declared tables, and has an unambiguous order."""
    problems = check_dataset(benchmark.cases, env)
    assert problems == []


def test_database_matches_the_benchmark_reference_fingerprint(benchmark, eval_db):
    """create_retail.py is deterministic; if this fails, either the generator
    changed (re-verify every gold result and refresh the fingerprint) or the
    platform produced different data."""
    from evaluation.db import db_fingerprint
    assert benchmark.reference_db_fingerprint
    assert db_fingerprint(eval_db) == benchmark.reference_db_fingerprint


# ---- strict validation ----------------------------------------------------

def test_valid_minimal_case_parses():
    b = _parse(_case())
    assert b.cases[0].expected_tables == ("orders",)
    assert len(b.sha256) == 64


@pytest.mark.parametrize("mutation, message", [
    ({"category": "sorcery"}, "category"),
    ({"difficulty": "extreme"}, "difficulty"),
    ({"expected_behavior": "maybe"}, "expected_behavior"),
    ({"id": "BAD ID"}, "id must"),
    ({"question": "   "}, "question"),
    ({"gold_sql": None}, "gold_sql"),
    ({"expected_tables": []}, "expected_tables"),
    ({"typo_field": 1}, "unknown field"),
    ({"expected_characteristics": {"aggregtion": True}}, "expected_characteristics"),
    ({"expected_characteristics": {"aggregation": "yes"}}, "expected_characteristics"),
    ({"ordered": True}, "order_by_column"),
    ({"category": "ambiguous"}, "requires expected_behavior"),  # answer + ambiguous category
])
def test_malformed_answerable_case_is_rejected(mutation, message):
    with pytest.raises(DatasetError, match=message):
        _parse(_case(**mutation))


def test_missing_required_field_is_rejected():
    c = _case()
    del c["difficulty"]
    with pytest.raises(DatasetError, match="missing required field 'difficulty'"):
        _parse(c)


@pytest.mark.parametrize("extra, message", [
    ({"gold_sql": "SELECT 1"}, "must not have gold_sql"),
    ({"expected_tables": ["orders"]}, "must not declare"),
    ({"decline_reason": "adversarial"}, "decline_reason"),  # wrong reason for category
    ({"decline_reason": None}, "decline_reason"),
    ({"ordered": True}, "cannot be ordered"),
])
def test_malformed_decline_case_is_rejected(extra, message):
    base = {"id": "t-002", "category": "ambiguous", "difficulty": "easy", "question": "Compare the regions.",
            "expected_behavior": "clarify", "decline_reason": "ambiguous"}
    base.update(extra)
    with pytest.raises(DatasetError, match=message):
        _parse(base)


def test_category_and_behavior_must_agree():
    with pytest.raises(DatasetError, match="requires expected_behavior 'reject'"):
        _parse({"id": "t-003", "category": "impossible", "difficulty": "easy", "question": "salary?",
                "expected_behavior": "clarify", "decline_reason": "nonexistent_data"})


def test_duplicate_ids_and_questions_are_rejected():
    with pytest.raises(DatasetError, match="duplicate case ids"):
        _parse(_case(), _case(question="A different question?"))
    with pytest.raises(DatasetError, match="duplicate questions"):
        _parse(_case(), _case(id="t-002", question="HOW MANY ORDERS ARE THERE?"))


@pytest.mark.parametrize("raw", ["not json", "[]", '{"cases": []}', '{"cases": "x"}'])
def test_malformed_files_are_rejected(raw):
    with pytest.raises(DatasetError):
        parse_benchmark(raw)


# ---- integrity checker catches bad gold ----------------------------------

def _one(case_dict):
    return list(_parse(case_dict).cases)


def test_integrity_flags_gold_sql_with_a_hallucinated_column(env):
    probs = check_dataset(_one(_case(gold_sql="SELECT COUNT(nonexistent_col) FROM orders")), env)
    assert probs and "gold SQL is rejected" in probs[0]


def test_integrity_flags_wrong_expected_tables(env):
    probs = check_dataset(_one(_case(expected_tables=["orders", "stores"])), env)
    assert any("expected_tables" in p for p in probs)


def test_integrity_flags_empty_gold_result(env):
    probs = check_dataset(_one(_case(gold_sql="SELECT order_id FROM orders WHERE status = 'nope'")), env)
    assert any("no rows" in p for p in probs)
    ok = check_dataset(_one(_case(gold_sql="SELECT order_id FROM orders WHERE status = 'nope'",
                                  allow_empty_result=True)), env)
    assert ok == []


def test_integrity_flags_ties_in_a_ranked_case(env):
    tied = _case(
        gold_sql="SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY 2 DESC LIMIT 2",
        expected_tables=["orders"], ordered=True, order_by_column="n")
    # counts are all distinct (1979/563/458), so this must pass...
    assert check_dataset(_one(tied), env) == []
    # ...but ordering by a low-cardinality column has ties within the ranked window.
    ambiguous = _case(
        gold_sql="SELECT store_id, status FROM orders ORDER BY status LIMIT 5",
        expected_tables=["orders"], ordered=True, order_by_column="status")
    assert any("ambiguous" in p for p in check_dataset(_one(ambiguous), env))


def test_integrity_flags_declared_characteristic_mismatch(env):
    probs = check_dataset(_one(_case(expected_characteristics={"group_by": True})), env)
    assert any("group_by" in p for p in probs)


# ---- filtering -------------------------------------------------------------

def test_select_cases_filters_and_rejects_unknown_values(benchmark):
    got = select_cases(benchmark.cases, categories=["joins"])
    assert got and all(c.category == "joins" for c in got)
    assert len(select_cases(benchmark.cases, limit=3)) == 3
    assert [c.id for c in select_cases(benchmark.cases, ids=["agg-001"])] == ["agg-001"]
    for kwargs in ({"categories": ["nope"]}, {"difficulties": ["nope"]}, {"ids": ["nope-999"]}, {"limit": 0}):
        with pytest.raises(DatasetError):
            select_cases(benchmark.cases, **kwargs)
