"""High-level entry point: one call = one complete, reproducible evaluation."""
from __future__ import annotations

import datetime as _dt
import json
import os
import platform
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import __version__
from .benchmark import DEFAULT_BENCHMARK_PATH, load_benchmark
from .db import db_fingerprint, prepare_database
from .metrics import compute_metrics
from .models import BenchmarkCase, DatasetError, select_cases
from .runner import CaseResult, build_environment, run_cases
from .sources import (
    CredentialsMissing, LiveSource, ResponseSource, build_oracle_source,
    build_replay_source, build_synthetic_source, load_synthetic_spec,
)

REPORT_VERSION = 1
SYNTHETIC_SPEC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "synthetic_flawed.json")

OFFLINE_MODES = ("oracle", "synthetic", "replay")
ALL_MODES = OFFLINE_MODES + ("live",)


def resolve_source(
    mode: str,
    cases: Sequence[BenchmarkCase],
    responses_path: Optional[str] = None,
    api_key: str = "",
    delay_seconds: float = 0.0,
    synthetic_spec_path: Optional[str] = None,
) -> ResponseSource:
    if mode == "oracle":
        return build_oracle_source(cases)
    if mode == "synthetic":
        return build_synthetic_source(cases, load_synthetic_spec(synthetic_spec_path or SYNTHETIC_SPEC_PATH))
    if mode == "replay":
        if not responses_path:
            raise DatasetError("replay mode needs --responses <file>")
        return build_replay_source(responses_path)
    if mode == "live":
        src = LiveSource(api_key=api_key, delay_seconds=delay_seconds)
        src.check_credentials()  # raises CredentialsMissing before any work is done
        return src
    raise DatasetError(f"unknown mode '{mode}' (expected one of {ALL_MODES})")


def _warnings(mode: str, source: ResponseSource, n_run: int, n_total: int, fp_ok: Optional[bool]) -> List[str]:
    w: List[str] = []
    if not source.calls_llm:
        w.append(
            f"Offline source '{source.name}': no LLM was called. These numbers describe the "
            "harness, the dataset and QuerySure's validation/execution layers -- NOT the quality of any LLM."
        )
        w.append("Latency covers retrieval, prompt construction, validation and execution only; it excludes LLM time.")
    if mode == "oracle":
        w.append("Oracle mode replays the gold SQL, so a perfect score is expected. It is a self-consistency "
                 "check of the benchmark and harness, not evidence that QuerySure answers questions well.")
    if mode == "synthetic":
        w.append("Synthetic mode injects hand-written, labelled defects to exercise the metrics. It is a demo "
                 "of what the report looks like when things go wrong, not a measurement of a real model.")
    if n_run < n_total:
        w.append(f"Subset run: {n_run} of {n_total} benchmark cases. Metrics cover only the selected cases.")
    if fp_ok is False:
        w.append("The evaluation database differs from the benchmark's reference fingerprint (different Python/"
                 "sqlite build or a custom --db). Expected results are recomputed from the gold SQL on the actual "
                 "database, so scoring stays valid, but row-level numbers may differ from other runs.")
    return w


def run_evaluation(
    mode: str = "oracle",
    benchmark_path: Optional[str] = None,
    db_path: Optional[str] = None,
    categories: Optional[Sequence[str]] = None,
    difficulties: Optional[Sequence[str]] = None,
    ids: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
    responses_path: Optional[str] = None,
    api_key: str = "",
    delay_seconds: float = 0.0,
    synthetic_spec_path: Optional[str] = None,
    progress: Optional[Callable[[int, int, CaseResult], None]] = None,
    source: Optional[ResponseSource] = None,
) -> Dict[str, Any]:
    """Run one evaluation and return the full report dict (JSON-serialisable).

    `source` lets tests/callers inject a custom ResponseSource.
    """
    bench = load_benchmark(benchmark_path)
    cases = select_cases(bench.cases, categories, difficulties, ids, limit)
    if not cases:
        raise DatasetError("no benchmark cases match the given filters")
    if source is None:
        source = resolve_source(mode, cases, responses_path, api_key, delay_seconds, synthetic_spec_path)

    with tempfile.TemporaryDirectory(prefix="querysure_eval_") as workdir:
        working_db = prepare_database(db_path, workdir)
        env = build_environment(working_db)
        fingerprint = db_fingerprint(working_db)
        results = run_cases(cases, source, env, progress=progress)
        table_rows = {t: info["row_count"] for t, info in env.schema["tables"].items()}

    metrics = compute_metrics(results, calls_llm=source.calls_llm)
    ref = bench.reference_db_fingerprint
    fp_ok = None if not ref else (fingerprint == ref)

    try:
        import sqlglot
        sqlglot_version = getattr(sqlglot, "__version__", "unknown")
    except Exception:  # pragma: no cover
        sqlglot_version = "unknown"

    return {
        "report_version": REPORT_VERSION,
        "meta": {
            "tool_version": __version__,
            "mode": mode,
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "source": {"name": source.name, "calls_llm": source.calls_llm, "description": source.description},
            "dataset": {
                "path": os.path.relpath(bench.path) if bench.path and os.path.exists(bench.path) else bench.path,
                "version": bench.version, "sha256": bench.sha256,
                "cases_run": len(cases), "cases_total": len(bench.cases),
                "reference_db_fingerprint": ref,
            },
            "database": {
                "fingerprint": fingerprint,
                "matches_reference": fp_ok,
                "source": "generated by create_retail.py" if db_path is None else "user supplied (copied)",
                "table_rows": table_rows,
            },
            "environment": {
                "python": platform.python_version(), "sqlglot": sqlglot_version,
                "sqlite": __import__("sqlite3").sqlite_version,
            },
            "warnings": _warnings(mode, source, len(cases), len(bench.cases), fp_ok),
        },
        "metrics": metrics,
        "cases": [r.to_dict() for r in results],
    }


def record_responses(report: Dict[str, Any], path: str) -> int:
    """Write the raw LLM replies from a (live) run so it can be replayed offline,
    for free and bit-for-bit, with `--mode replay --responses <path>`."""
    out: Dict[str, Any] = {}
    for c in report["cases"]:
        if c["pipeline_status"] == "generation_error":
            out[c["id"]] = {"provider_error": c.get("error") or "provider failure"}
        else:
            out[c["id"]] = {"raw_response": c.get("raw_response", "")}
    payload = {
        "meta": {
            "recorded_from_mode": report["meta"]["mode"],
            "recorded_at": report["meta"]["generated_at"],
            "dataset_sha256": report["meta"]["dataset"]["sha256"],
        },
        "responses": out,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return len(out)
