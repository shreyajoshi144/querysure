"""The evaluation's cost estimate must equal what QuerySure itself would record."""
from __future__ import annotations

import pytest

import api as api_module
from evaluation import cost as eval_cost

CASES = [
    ("How many orders?", None, {"sql": "SELECT COUNT(*) FROM orders"}),
    ("x" * 500, {"retrieved_context_text": "y" * 4000}, {"sql": "SELECT " + "a, " * 200 + "b FROM t"}),
    ("", {"retrieved_context_text": ""}, {"sql": ""}),
    ("q", {"retrieved_context_text": "ctx"}, {"sql": "s", "usage": {"input_tokens": 1000, "output_tokens": 500}}),
    ("q", None, {"sql": "s", "usage": {"total_tokens": 2500}}),
    ("q", None, {"usage": {}}),
]


@pytest.mark.parametrize("question, retrieval, result", CASES)
def test_cost_estimate_matches_api_py(question, retrieval, result):
    assert eval_cost.estimate_cost_cents(question, retrieval, result) == pytest.approx(
        api_module._estimate_cost_cents(question, retrieval, result))


def test_rate_constant_has_not_drifted():
    assert eval_cost.EST_COST_PER_1K_TOKENS_CENTS == api_module._EST_COST_PER_1K_TOKENS_CENTS


def test_estimate_is_never_zero_or_negative():
    assert eval_cost.estimate_cost_cents("", None, {}) > 0
