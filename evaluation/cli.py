"""
Command line interface.

    python -m evaluation run --mode oracle              # offline harness self-check
    python -m evaluation run --mode synthetic           # offline, defect-injected demo
    python -m evaluation run --mode replay --responses recorded.json
    python -m evaluation live                           # REAL LLM: needs API keys, costs money
    python -m evaluation validate-dataset
    python -m evaluation list-cases

Exit codes: 0 ok | 1 a --min/--max threshold failed | 2 usage / credentials / dataset error
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from typing import List, Optional

EXIT_OK, EXIT_THRESHOLD, EXIT_ERROR = 0, 1, 2


_LLM_ENV_VARS = (
    "GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "GROQ_MODEL", "GROQ_FALLBACK_MODEL", "OPENAI_FALLBACK_MODEL", "ANTHROPIC_FALLBACK_MODEL",
)


def load_llm_env_from_dotenv(path: Optional[str] = None) -> List[str]:
    """Copy LLM credentials/model overrides from the repo's .env into os.environ.

    The web app does this in api.py; the evaluation CLI does not import api.py,
    so without this a user who keeps keys in .env (the project's documented
    setup) would be told no credentials exist. Deliberately narrow: only the
    LLM variables are read (never ENVIRONMENT/DATABASE_URL/etc.), values already
    set in the real environment win, and it is used by `live` only so offline
    runs stay hermetic. Must run before `config` is first imported.
    Returns the names it set.
    """
    from . import REPO_ROOT
    path = path or os.path.join(REPO_ROOT, ".env")
    if not os.path.isfile(path):
        return []
    from dotenv import dotenv_values
    loaded = []
    for key, value in dotenv_values(path).items():
        if key in _LLM_ENV_VARS and value and not os.environ.get(key):
            os.environ[key] = value
            loaded.append(key)
    return loaded


def _csv(value: Optional[str]) -> Optional[List[str]]:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dataset", help="benchmark JSON (default: evaluation/datasets/retail_benchmark.json)")
    p.add_argument("--db", help="SQLite file to evaluate against (copied; never modified). "
                                "Default: build the sample retail DB with create_retail.py")
    p.add_argument("--category", help="comma-separated categories to run")
    p.add_argument("--difficulty", help="comma-separated difficulties to run")
    p.add_argument("--ids", help="comma-separated case ids to run")
    p.add_argument("--limit", type=int, help="run only the first N selected cases")
    p.add_argument("--output-dir", default=None, help="where to write <name>.json / <name>.md (default evaluation/results)")
    p.add_argument("--name", help="report file stem (default <mode>_<timestamp>)")
    p.add_argument("--no-write", action="store_true", help="print only; do not write report files")
    p.add_argument("--quiet", action="store_true", help="suppress the console summary")
    g = p.add_argument_group("pass/fail thresholds (exit code 1 when violated)")
    g.add_argument("--min-execution-accuracy", type=float)
    g.add_argument("--min-sql-validity", type=float)
    g.add_argument("--min-behavior-accuracy", type=float)
    g.add_argument("--min-rejection-accuracy", type=float, help="strict rejection/clarification accuracy")
    g.add_argument("--max-hallucination-rate", type=float, help="case-level hallucination rate")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m evaluation", description="QuerySure evaluation harness")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="offline evaluation (no API keys, no network, no cost)")
    _add_common(run)
    run.add_argument("--mode", choices=("oracle", "synthetic", "replay"), default="oracle")
    run.add_argument("--responses", help="recorded responses JSON (replay mode)")

    live = sub.add_parser("live", help="REAL LLM evaluation -- REQUIRES API credentials and spends money")
    _add_common(live)
    live.add_argument("--api-key", default="", help="Groq API key (else GROQ_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY from the environment)")
    live.add_argument("--delay-seconds", type=float, default=0.0, help="pause between cases (rate limits)")
    live.add_argument("--record-responses", help="save raw LLM replies here for free, exact offline replay later")
    live.add_argument("--yes", action="store_true", help="skip the paid-API confirmation prompt")

    de = sub.add_parser("deepeval-golden", help="supplement the existing benchmark with DeepEval metrics")
    _add_common(de)
    de.add_argument("--mode", choices=("live", "replay"), default="live", help="QuerySure response source")
    de.add_argument("--responses", help="recorded QuerySure LLM replies for replay mode")
    de.add_argument("--api-key", default="", help="Groq key for QuerySure live generation (same provider config)")
    de.add_argument("--delay-seconds", type=float, default=0.0)
    de.add_argument("--judge-provider", choices=("auto", "groq", "openai", "anthropic"), default="auto")
    de.add_argument("--judge-model", default="")
    de.add_argument("--yes", action="store_true", help="skip the paid-API confirmation prompt")

    syn = sub.add_parser("deepeval-synthetic", help="generate and optionally evaluate synthetic DeepEval goldens")
    syn.add_argument("--num", type=int, default=25, help="number of synthetic questions to generate (1-500)")
    syn.add_argument("--output", default="evaluation/results/deepeval_synthetic.json")
    syn.add_argument("--run", action="store_true", help="also run QuerySure against the generated questions and DeepEval-grade the outputs")
    syn.add_argument("--db")
    syn.add_argument("--judge-provider", choices=("auto", "groq", "openai", "anthropic"), default="auto")
    syn.add_argument("--judge-model", default="")

    v = sub.add_parser("validate-dataset", help="check the benchmark itself (gold SQL validity, ties, expected tables)")
    v.add_argument("--dataset")
    v.add_argument("--db")

    lc = sub.add_parser("list-cases", help="print the benchmark cases")
    lc.add_argument("--dataset")
    lc.add_argument("--category")
    return p


def _thresholds(args) -> dict:
    return {
        "min_execution_accuracy": getattr(args, "min_execution_accuracy", None),
        "min_sql_validity": getattr(args, "min_sql_validity", None),
        "min_behavior_accuracy": getattr(args, "min_behavior_accuracy", None),
        "min_rejection_accuracy": getattr(args, "min_rejection_accuracy", None),
        "max_hallucination_rate": getattr(args, "max_hallucination_rate", None),
    }


def _write_json(path: str, payload: dict) -> None:
    import os
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def _cmd_deepeval_golden(args) -> int:
    from .deepeval_adapter import (
        DeepEvalCredentialsMissing, DeepEvalUnavailable, evaluate_deepeval_report,
        _import_deepeval,
    )
    try:
        _import_deepeval()
    except DeepEvalUnavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    from .harness import run_evaluation
    load_llm_env_from_dotenv()
    if args.mode == "live":
        # QuerySure's existing live evaluator remains the application LLM path.
        # DeepEval adds a second judge call, so require an explicit confirmation.
        if not args.yes:
            if not sys.stdin.isatty():
                print("error: live DeepEval evaluation can spend money; pass --yes in non-interactive CI.", file=sys.stderr)
                return EXIT_ERROR
            if input("Proceed with QuerySure + DeepEval live evaluation? [y/N] ").strip().lower() not in ("y", "yes"):
                print("aborted.")
                return EXIT_ERROR
    try:
        report = run_evaluation(
            mode=args.mode,
            benchmark_path=args.dataset,
            db_path=args.db,
            categories=_csv(args.category),
            difficulties=_csv(args.difficulty),
            ids=_csv(args.ids),
            limit=args.limit,
            responses_path=args.responses,
            api_key=args.api_key,
            delay_seconds=args.delay_seconds,
        )
        # The current evaluator remains the source of truth. DeepEval only sees
        # the already-produced QuerySure outputs.
        de_report = evaluate_deepeval_report(
            report, provider=args.judge_provider, model=args.judge_model, limit=args.limit,
            api_key_override=args.api_key, benchmark_path=args.dataset,
        )
    except (DeepEvalUnavailable, DeepEvalCredentialsMissing) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR

    payload = {"querysure": report, "deepeval": de_report}
    stem = args.name or f"deepeval_golden_{time.strftime('%Y%m%d-%H%M%S')}"
    if not args.no_write:
        out_dir = args.output_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, stem + ".json")
        _write_json(path, payload)
        print(f"DeepEval report written: {path}")
    if not args.quiet:
        print(f"DeepEval golden evaluation: {de_report['cases']} cases; judge={de_report['judge']['provider']}:{de_report['judge']['model']}")
        print("Deterministic QuerySure metrics remain authoritative for SQL/result correctness and safety.")
    return EXIT_OK


def _cmd_deepeval_synthetic(args) -> int:
    from .deepeval_adapter import DeepEvalCredentialsMissing, DeepEvalUnavailable, generate_synthetic_goldens, run_synthetic_deepeval
    try:
        goldens = generate_synthetic_goldens(args.num, provider=args.judge_provider, model=args.judge_model)
    except (DeepEvalUnavailable, DeepEvalCredentialsMissing) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    payload = {"framework": "DeepEval", "version": "4.2.6", "cases": goldens}
    _write_json(args.output, payload)
    print(f"Generated {len(goldens)} synthetic DeepEval goldens -> {args.output}")
    if args.run:
        try:
            result = run_synthetic_deepeval(goldens, provider=args.judge_provider, model=args.judge_model, db_path=args.db)
        except (DeepEvalUnavailable, DeepEvalCredentialsMissing) as e:
            print(f"error: {e}", file=sys.stderr)
            return EXIT_ERROR
        run_path = args.output.rsplit(".", 1)[0] + "_run.json"
        _write_json(run_path, result)
        print(f"Synthetic QuerySure + DeepEval report -> {run_path}")
    return EXIT_OK


def _cmd_list(args) -> int:
    from .benchmark import load_benchmark
    from .models import select_cases
    bench = load_benchmark(args.dataset)
    cases = select_cases(bench.cases, categories=_csv(args.category))
    print(f"{bench.version}  ({len(cases)} of {len(bench.cases)} cases)  sha256={bench.sha256[:12]}")
    for c in cases:
        print(f"{c.id:<12} {c.category:<17} {c.difficulty:<7} {c.expected_behavior:<8} {c.question}")
    return EXIT_OK


def _cmd_validate(args) -> int:
    from .benchmark import load_benchmark
    from .db import prepare_database
    from .integrity import check_dataset
    from .runner import build_environment
    bench = load_benchmark(args.dataset)
    with tempfile.TemporaryDirectory(prefix="querysure_eval_") as wd:
        env = build_environment(prepare_database(args.db, wd))
        problems = check_dataset(bench.cases, env)
    if problems:
        print(f"Dataset has {len(problems)} problem(s):")
        for pr in problems:
            print(f"  - {pr}")
        return EXIT_ERROR
    n_ans = sum(1 for c in bench.cases if not c.should_decline)
    print(f"OK: {len(bench.cases)} cases ({n_ans} answerable, {len(bench.cases) - n_ans} should-decline); "
          "all gold SQL validates, executes and is unambiguous.")
    return EXIT_OK


def _emit(report: dict, args, mode: str) -> int:
    from .metrics import check_thresholds
    from .report import render_console, write_report
    if not args.quiet:
        print(render_console(report))
    if not args.no_write:
        out_dir = args.output_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
        stem = args.name or f"{mode}_{time.strftime('%Y%m%d-%H%M%S')}"
        paths = write_report(report, out_dir, stem)
        if not args.quiet:
            print(f"\nReport written: {paths['json']}\n                {paths['markdown']}")
    failures = check_thresholds(report["metrics"], _thresholds(args))
    if failures:
        print("\nTHRESHOLD FAILURES:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return EXIT_THRESHOLD
    return EXIT_OK


def _cmd_run(args, live: bool) -> int:
    from .harness import record_responses, run_evaluation
    from .sources import CredentialsMissing

    mode = "live" if live else args.mode
    if live:
        load_llm_env_from_dotenv()
    kwargs = dict(
        mode=mode, benchmark_path=args.dataset, db_path=args.db,
        categories=_csv(args.category), difficulties=_csv(args.difficulty),
        ids=_csv(args.ids), limit=args.limit,
        responses_path=getattr(args, "responses", None),
    )
    if live:
        from .harness import resolve_source
        from .benchmark import load_benchmark
        from .models import select_cases
        try:
            cases = select_cases(load_benchmark(args.dataset).cases, kwargs["categories"],
                                 kwargs["difficulties"], kwargs["ids"], args.limit)
            source = resolve_source("live", cases, api_key=args.api_key, delay_seconds=args.delay_seconds)
        except CredentialsMissing as e:
            print(f"error: {e}", file=sys.stderr)
            return EXIT_ERROR
        chain = source.provider_chain_names()
        print("LIVE evaluation -- this calls real LLM APIs and costs real money.")
        print(f"  provider chain (in fallback order): {', '.join(chain)}")
        print(f"  cases: {len(cases)}  (up to {len(cases) * len(chain)} API calls if every provider fails over)")
        if not args.yes:
            if not sys.stdin.isatty():
                print("error: refusing to spend money non-interactively; pass --yes to confirm.", file=sys.stderr)
                return EXIT_ERROR
            if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
                print("aborted.")
                return EXIT_ERROR
        kwargs["source"] = source

    def progress(i, n, r):
        if not args.quiet:
            print(f"  [{i:>3}/{n}] {r.id:<12} {r.pipeline_status:<19} {r.verdict}", flush=True)

    report = run_evaluation(progress=progress, **kwargs)
    if live and args.record_responses:
        n = record_responses(report, args.record_responses)
        print(f"Recorded {n} raw LLM replies -> {args.record_responses} (replay: --mode replay --responses ...)")
    return _emit(report, args, mode)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        from .models import DatasetError
        from .db import DatabaseError
    except Exception as e:  # pragma: no cover
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    try:
        if args.command == "list-cases":
            return _cmd_list(args)
        if args.command == "validate-dataset":
            return _cmd_validate(args)
        if args.command == "run":
            return _cmd_run(args, live=False)
        if args.command == "deepeval-golden":
            return _cmd_deepeval_golden(args)
        if args.command == "deepeval-synthetic":
            return _cmd_deepeval_synthetic(args)
        if args.command == "live":
            return _cmd_run(args, live=True)
    except (DatasetError, DatabaseError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    except RuntimeError as e:
        # config.py raises RuntimeError for production-only guards.
        print(f"error: {e}\n(hint: run the evaluation with ENVIRONMENT unset or 'development')", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_ERROR
