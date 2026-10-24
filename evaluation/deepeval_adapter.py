"""DeepEval integration for QuerySure's existing evaluation harness.

DeepEval is deliberately an additional layer. The deterministic evaluator in
this package remains authoritative for SQL validity, execution success,
result equivalence, hallucinated identifiers, schema grounding, and
rejection/clarification behaviour.

This module is optional at import time: the core evaluation suite still works
when DeepEval is not installed. Commands that actually invoke DeepEval fail
with a clear dependency message instead of changing QuerySure behaviour.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .benchmark import load_benchmark
from .db import prepare_database
from .models import DatasetError

DEEPEVAL_VERSION = "4.2.6"


class DeepEvalUnavailable(RuntimeError):
    """DeepEval is not installed in the current environment."""


class DeepEvalCredentialsMissing(RuntimeError):
    """No provider configured for DeepEval's judge model."""


@dataclass(frozen=True)
class JudgeConfig:
    provider: str
    model: str
    api_key: str


def _import_deepeval():
    try:
        import deepeval  # type: ignore
        from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric
        from deepeval.test_case import LLMTestCase
    except ImportError as exc:
        raise DeepEvalUnavailable(
            f"DeepEval is required for this command. Install deepeval=={DEEPEVAL_VERSION} "
            "from requirements.txt. The deterministic QuerySure evaluator does not require it."
        ) from exc
    return deepeval, AnswerRelevancyMetric, FaithfulnessMetric, LLMTestCase


def resolve_judge_config(provider: str = "", model: str = "", api_key_override: str = "") -> JudgeConfig:
    """Select the first configured judge using QuerySure's existing provider configuration.

    Order intentionally matches QuerySure's production provider chain:
    Groq -> OpenAI -> Anthropic. No new API key is introduced.
    """
    import config

    requested = (provider or os.environ.get("DEEPEVAL_JUDGE_PROVIDER", "auto")).strip().lower()
    model = model or os.environ.get("DEEPEVAL_JUDGE_MODEL", "")
    if requested not in {"auto", "groq", "openai", "anthropic"}:
        raise DatasetError("DeepEval judge provider must be auto, groq, openai or anthropic")

    candidates = {
        "groq": ((api_key_override or config.GROQ_API_KEY or "").strip(), model or config.GROQ_MODEL),
        "openai": ((config.OPENAI_API_KEY or "").strip(), model or config.OPENAI_FALLBACK_MODEL),
        "anthropic": ((config.ANTHROPIC_API_KEY or "").strip(), model or config.ANTHROPIC_FALLBACK_MODEL),
    }
    order = [requested] if requested != "auto" else ["groq", "openai", "anthropic"]
    for name in order:
        key, chosen_model = candidates[name]
        if key:
            return JudgeConfig(name, chosen_model, key)
    raise DeepEvalCredentialsMissing(
        "DeepEval needs a judge provider. Set GROQ_API_KEY, OPENAI_API_KEY or "
        "ANTHROPIC_API_KEY using the same configuration as QuerySure."
    )


def _provider_text(config: JudgeConfig, prompt: str) -> str:
    """One synchronous judge call using the same provider SDKs as QuerySure."""
    if config.provider == "groq":
        from groq import Groq
        resp = Groq(api_key=config.api_key).chat.completions.create(
            model=config.model,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content or ""
    if config.provider == "openai":
        from openai import OpenAI
        resp = OpenAI(api_key=config.api_key).chat.completions.create(
            model=config.model,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content or ""
    import anthropic
    resp = anthropic.Anthropic(api_key=config.api_key).messages.create(
        model=config.model,
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(getattr(block, "text", "") for block in resp.content if getattr(block, "type", None) == "text")


def build_deepeval_judge(provider: str = "auto", model: str = "", api_key_override: str = ""):
    """Return a DeepEvalBaseLLM backed by QuerySure's configured providers."""
    try:
        from deepeval.models.base_model import DeepEvalBaseLLM
    except ImportError as exc:
        raise DeepEvalUnavailable(
            f"DeepEval {DEEPEVAL_VERSION} is required for the judge adapter."
        ) from exc

    cfg = resolve_judge_config(provider, model, api_key_override)

    class QuerySureJudge(DeepEvalBaseLLM):
        def load_model(self):
            return cfg

        def generate(self, prompt: str, schema: Any = None):
            text = _provider_text(cfg, prompt)
            if schema is None:
                return text
            # DeepEval passes a Pydantic schema for structured judge responses.
            # Prefer Pydantic's JSON validation; if the provider returned fenced
            # JSON, strip the fence before validating. Never silently fabricate a
            # structured response.
            cleaned = text.strip()
            if "```json" in cleaned:
                cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
            try:
                if hasattr(schema, "model_validate_json"):
                    return schema.model_validate_json(cleaned)
                return schema.parse_raw(cleaned)
            except Exception as exc:
                raise ValueError(f"DeepEval judge returned invalid structured output: {exc}") from exc

        async def a_generate(self, prompt: str, schema: Any = None):
            return await asyncio.to_thread(self.generate, prompt, schema)

        def get_model_name(self):
            return f"querysure:{cfg.provider}:{cfg.model}"

    return QuerySureJudge(), cfg


def _schema_context(case_result: Mapping[str, Any], schema: Optional[Mapping[str, Any]] = None) -> List[str]:
    """Build compact trusted context for DeepEval without changing the app pipeline."""
    contexts: List[str] = []
    if schema:
        for table, info in schema.get("tables", {}).items():
            cols = ", ".join(f"{c.get('name')} ({c.get('type')})" for c in info.get("columns", []))
            contexts.append(f"Table {table}: {cols}")
    expected = case_result.get("expected_tables") or []
    if expected:
        contexts.append("Expected tables for this benchmark case: " + ", ".join(expected))
    return contexts


def _output_text(case_result: Mapping[str, Any]) -> str:
    sql = case_result.get("sql") or "<no SQL produced>"
    status = case_result.get("pipeline_status") or "unknown"
    result = case_result.get("result_match")
    return (
        f"Pipeline status: {status}\n"
        f"Generated SQL:\n{sql}\n"
        f"Execution success: {case_result.get('execution_success')}\n"
        f"Result-equivalence status: {result}\n"
        f"Rows returned: {case_result.get('row_count')}"
    )


def _metric_record(metric: Any) -> Dict[str, Any]:
    score = getattr(metric, "score", None)
    reason = getattr(metric, "reason", None)
    return {
        "name": getattr(metric, "name", metric.__class__.__name__),
        "score": score,
        "reason": reason,
        "threshold": getattr(metric, "threshold", None),
        "successful": metric.is_successful() if hasattr(metric, "is_successful") else None,
    }


def build_golden_test_cases(report: Mapping[str, Any], schema: Optional[Mapping[str, Any]] = None):
    """Convert deterministic runner output into DeepEval LLMTestCases.

    The conversion never changes the deterministic verdicts. For answerable
    cases, gold SQL is supplied as expected_output when available; DeepEval is
    explicitly supplemental and does not replace result-set equivalence.
    """
    _, _, _, LLMTestCase = _import_deepeval()
    gold_by_id = {}
    for c in load_benchmark().cases:
        gold_by_id[c.id] = c.gold_sql

    test_cases = []
    for case in report["cases"]:
        expected = gold_by_id.get(case["id"]) if case.get("expected_behavior") == "answer" else None
        test_cases.append(
            LLMTestCase(
                input=case["question"],
                actual_output=_output_text(case),
                expected_output=expected,
                retrieval_context=_schema_context(case, schema),
                metadata={
                    "case_id": case["id"],
                    "deterministic_verdict": case["verdict"],
                    "deterministic_result_match": case.get("result_match"),
                    "deterministic_hallucination": case.get("hallucination"),
                },
            )
        )
    return test_cases


def evaluate_deepeval_report(
    report: Mapping[str, Any],
    provider: str = "auto",
    model: str = "",
    schema: Optional[Mapping[str, Any]] = None,
    limit: Optional[int] = None,
    api_key_override: str = "",
    benchmark_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Run supplementary DeepEval metrics against an existing QuerySure report."""
    _, AnswerRelevancyMetric, FaithfulnessMetric, _ = _import_deepeval()
    judge, cfg = build_deepeval_judge(provider, model, api_key_override)
    cases = list(report["cases"])
    if limit is not None:
        cases = cases[:limit]

    # Keep the metric set deliberately small. These evaluate answer relevance
    # and grounding; deterministic SQL/result correctness remains authoritative.
    metrics = [
        AnswerRelevancyMetric(model=judge, threshold=None, include_reason=True, async_mode=False),
        FaithfulnessMetric(model=judge, threshold=None, include_reason=True, async_mode=False),
    ]

    test_cases = []
    source_cases = []
    _, _, _, LLMTestCase = _import_deepeval()
    bench = load_benchmark(benchmark_path)
    gold_by_id = {c.id: c.gold_sql for c in bench.cases}
    for case in cases:
        expected = gold_by_id.get(case["id"]) if case.get("expected_behavior") == "answer" else None
        test_cases.append(LLMTestCase(
            input=case["question"],
            actual_output=_output_text(case),
            expected_output=expected,
            retrieval_context=_schema_context(case, schema),
            metadata={
                "case_id": case["id"],
                "deterministic_verdict": case["verdict"],
                "deterministic_result_match": case.get("result_match"),
                "deterministic_hallucination": case.get("hallucination"),
            },
        ))
        source_cases.append(case)

    per_case: List[Dict[str, Any]] = []
    for source_case, tc in zip(source_cases, test_cases):
        records = []
        for metric in metrics:
            try:
                metric.measure(tc)
                records.append(_metric_record(metric))
            except Exception as exc:  # metric/provider failure is reported, never fabricated as a score
                records.append({
                    "name": getattr(metric, "name", metric.__class__.__name__),
                    "score": None,
                    "reason": None,
                    "threshold": getattr(metric, "threshold", None),
                    "successful": None,
                    "error": str(exc),
                })
        per_case.append({"id": source_case["id"], "metrics": records})

    aggregate: Dict[str, Dict[str, Any]] = {}
    for item in per_case:
        for metric in item["metrics"]:
            name = metric["name"]
            aggregate.setdefault(name, {"scores": [], "errors": 0})
            if isinstance(metric.get("score"), (int, float)):
                aggregate[name]["scores"].append(float(metric["score"]))
            if metric.get("error"):
                aggregate[name]["errors"] += 1
    for name, data in aggregate.items():
        scores = data.pop("scores")
        data["cases_scored"] = len(scores)
        data["cases_with_errors"] = data.pop("errors")
        data["mean_score"] = round(sum(scores) / len(scores), 4) if scores else None

    return {
        "framework": "DeepEval",
        "version": DEEPEVAL_VERSION,
        "judge": {"provider": cfg.provider, "model": cfg.model},
        "cases": len(per_case),
        "metrics": aggregate,
        "per_case": per_case,
        "deterministic_source_of_truth": [
            "sql_validity", "execution_success", "result_correctness",
            "hallucinated_identifiers", "schema_grounding", "rejection_clarification_accuracy",
        ],
        "note": "DeepEval metrics are supplementary LLM-as-a-judge signals and do not override deterministic QuerySure metrics.",
    }


def generate_synthetic_goldens(
    num_goldens: int,
    provider: str = "auto",
    model: str = "",
    seed_questions: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Use DeepEval's Golden Synthesizer to expand question coverage.

    The generated items intentionally remain DeepEval goldens rather than being
    promoted to deterministic SQL benchmark cases. DeepEval does not guarantee
    a gold SQL result for synthetic questions, so these cases cannot replace
    the 62-case deterministic benchmark without human/database validation.
    """
    if num_goldens < 1 or num_goldens > 500:
        raise DatasetError("--num must be between 1 and 500")
    try:
        from deepeval.synthesizer import Synthesizer
        from deepeval.synthesizer.config import StylingConfig
        from deepeval.dataset import Golden
    except ImportError as exc:
        raise DeepEvalUnavailable(
            f"DeepEval {DEEPEVAL_VERSION} is required for synthetic generation."
        ) from exc

    judge, cfg = build_deepeval_judge(provider, model)
    styling = StylingConfig(
        input_format="Natural-language questions asking for data from the connected SQLite schema.",
        expected_output_format="A concise natural-language description of the intended analytics request; do not invent schema identifiers.",
        task="Generate realistic text-to-SQL analytics questions for a structured retail dataset.",
        scenario="A non-technical analyst exploring stores, products, customers, orders and order items.",
    )
    synth = Synthesizer(model=judge, styling_config=styling, async_mode=False)

    if seed_questions:
        seeds = [Golden(input=q) for q in seed_questions if q.strip()]
        generated = synth.generate_goldens_from_goldens(
            goldens=seeds,
            max_goldens_per_golden=max(1, (num_goldens + len(seeds) - 1) // len(seeds)),
            include_expected_output=False,
        )
        generated = generated[:num_goldens]
    else:
        generated = synth.generate_goldens_from_scratch(num_goldens=num_goldens)

    out = []
    for i, golden in enumerate(generated, 1):
        out.append({
            "id": f"synthetic-{i:03d}",
            "input": getattr(golden, "input", ""),
            "expected_output": getattr(golden, "expected_output", None),
            "context": getattr(golden, "context", None),
            "source": "DeepEval Golden Synthesizer",
        })
    return out


def run_synthetic_deepeval(
    goldens: Sequence[Mapping[str, Any]],
    provider: str = "auto",
    model: str = "",
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Run QuerySure against synthetic questions and grade with DeepEval.

    This uses the same production functions as the existing harness but does
    not invent deterministic gold SQL. It is deliberately a supplementary
    coverage signal.
    """
    _, AnswerRelevancyMetric, FaithfulnessMetric, LLMTestCase = _import_deepeval()
    judge, cfg = build_deepeval_judge(provider, model)
    from .runner import build_environment
    from modules import execute_query, generate_sql, retrieve_context, validate_sql
    import tempfile

    cases = []
    with tempfile.TemporaryDirectory(prefix="querysure_deepeval_synth_") as workdir:
        working_db = prepare_database(db_path, workdir)
        env = build_environment(working_db)
        schema = env.schema
        for golden in goldens:
            q = str(golden.get("input", "")).strip()
            if not q:
                continue
            retrieval = retrieve_context(q, env.semantic, cache_key=env.cache_key)
            import config
            gen = generate_sql(
                question=q, schema=schema,
                api_key=(config.GROQ_API_KEY or "").strip(),
                semantic=env.semantic, retrieval=retrieval,
            )
            sql = (gen.get("sql") or "").strip()
            status = "no_sql"
            if gen.get("success") is False:
                status = "generation_error"
            elif sql:
                validation = validate_sql(sql, schema, env.semantic)
                if not validation.get("is_valid"):
                    status = "validator_rejected"
                else:
                    result = execute_query(sql, working_db, max_rows=1000)
                    status = "executed" if result.get("success") else "execution_error"
            actual = (
                f"Pipeline status: {status}\nGenerated SQL:\n{sql or '<none>'}\n"
                f"Raw model response:\n{gen.get('raw_response', '')}"
            )
            tc = LLMTestCase(
                input=q,
                actual_output=actual,
                retrieval_context=_schema_context({}, schema),
                metadata={"synthetic_id": golden.get("id"), "pipeline_status": status},
            )
            metric_records = []
            for metric in (
                AnswerRelevancyMetric(model=judge, threshold=None, include_reason=True, async_mode=False),
                FaithfulnessMetric(model=judge, threshold=None, include_reason=True, async_mode=False),
            ):
                try:
                    metric.measure(tc)
                    metric_records.append(_metric_record(metric))
                except Exception as exc:
                    metric_records.append({"name": getattr(metric, "name", metric.__class__.__name__), "score": None, "reason": None, "error": str(exc)})
            cases.append({"id": golden.get("id"), "input": q, "pipeline_status": status, "metrics": metric_records})

    return {
        "framework": "DeepEval",
        "version": DEEPEVAL_VERSION,
        "judge": {"provider": cfg.provider, "model": cfg.model},
        "cases": len(cases),
        "cases_source": "DeepEval synthetic goldens",
        "results": cases,
        "note": "Synthetic cases are coverage expansion only until their intended behavior/result is independently validated."
    }
