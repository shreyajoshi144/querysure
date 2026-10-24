"""Tests for the optional DeepEval layer.

These tests intentionally do not call a network provider or require DeepEval to
be installed. The production evaluator remains testable independently.
"""
from __future__ import annotations

import hashlib
import json
import sys
import types

import pytest

from evaluation.benchmark import load_benchmark
from evaluation.deepeval_adapter import (
    DEEPEVAL_VERSION,
    DeepEvalCredentialsMissing,
    DeepEvalUnavailable,
    build_golden_test_cases,
    resolve_judge_config,
)


EXPECTED_BENCHMARK_SHA = "e2150163a71b2448754129e428685f7dcd11015cb6c1a5fc21017ddabfe4b4a0"


def _install_fake_deepeval(monkeypatch):
    class FakeTestCase:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    fake = types.ModuleType("deepeval")
    test_case = types.ModuleType("deepeval.test_case")
    test_case.LLMTestCase = FakeTestCase
    metrics = types.ModuleType("deepeval.metrics")
    metrics.AnswerRelevancyMetric = object
    metrics.FaithfulnessMetric = object
    monkeypatch.setitem(sys.modules, "deepeval", fake)
    monkeypatch.setitem(sys.modules, "deepeval.test_case", test_case)
    monkeypatch.setitem(sys.modules, "deepeval.metrics", metrics)


def test_golden_benchmark_remains_exactly_62_cases_and_unchanged():
    bench = load_benchmark()
    assert len(bench.cases) == 62
    digest = hashlib.sha256(open(bench.path, "rb").read()).hexdigest()
    assert digest == EXPECTED_BENCHMARK_SHA


def test_deepeval_dependency_is_optional_for_core_imports(monkeypatch):
    # Simulate "DeepEval not installed" regardless of the real environment:
    # requirements.txt DOES install it, so merely un-loading the module would
    # let the import succeed. A None entry in sys.modules makes `import
    # deepeval` raise ImportError exactly like a missing package.
    for name in list(sys.modules):
        if name == "deepeval" or name.startswith("deepeval."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "deepeval", None)
    import evaluation.deepeval_adapter as adapter
    with pytest.raises(DeepEvalUnavailable):
        adapter._import_deepeval()


def test_judge_config_reuses_existing_provider_configuration(monkeypatch):
    import config
    monkeypatch.setattr(config, "GROQ_API_KEY", "groq-test-key")
    monkeypatch.setattr(config, "GROQ_MODEL", "groq-test-model")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    cfg = resolve_judge_config("auto")
    assert cfg.provider == "groq"
    assert cfg.model == "groq-test-model"
    assert cfg.api_key == "groq-test-key"


def test_explicit_judge_provider_requires_its_existing_key(monkeypatch):
    import config
    monkeypatch.setattr(config, "GROQ_API_KEY", "")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    with pytest.raises(DeepEvalCredentialsMissing):
        resolve_judge_config("openai")


def test_golden_adapter_preserves_deterministic_verdict_metadata(monkeypatch, oracle_report):
    _install_fake_deepeval(monkeypatch)
    cases = build_golden_test_cases(oracle_report)
    assert len(cases) == 62
    assert cases[0].metadata["deterministic_verdict"] == "correct"
    assert cases[0].metadata["deterministic_result_match"] is True
    assert cases[0].expected_output
    assert cases[0].input


def test_deepeval_version_is_pinned():
    assert DEEPEVAL_VERSION == "4.2.6"
