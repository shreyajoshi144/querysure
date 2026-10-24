"""
Runs benchmark cases through QuerySure's real pipeline, in the same order and
with the same gates the API uses:

    retrieve_context -> generate_sql -> validate_sql -> (only if valid) execute_query

`api.py` re-validates before executing (a P0 fix in the audit), so SQL the
validator rejects is never executed here either.

Outcome model
-------------
Each case gets a `pipeline_status` describing what the system did:

    generation_error    every LLM provider failed (infrastructure, not a "no")
    no_sql              the generator produced no SQL (e.g. the LLM declined)
    validator_rejected  SQL produced but QuerySure's validator blocked it
    execution_error     validator passed, SQLite raised at run time
    executed            ran successfully

and a `verdict` (correct/incorrect) judged against the case's expectation:

    answerable case : correct  <=> executed AND result set equals the gold result
    reject/clarify  : correct  <=> no_sql OR validator_rejected            (strict)
                      lenient  also credits execution_error

A provider outage is never credited as a correct rejection.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .analysis import Catalog, SQLAnalysis, analyze_sql, build_catalog, sqlite_compile_check
from .compare import Comparison, compare_results
from .cost import estimate_cost_cents
from .db import open_readonly
from .models import BenchmarkCase, DatasetError
from .sources import LiveSource, ResponseSource

DEFAULT_MAX_ROWS = 1000  # same default as modules.query_result_engine.execute_query

STATUS_GENERATION_ERROR = "generation_error"
STATUS_NO_SQL = "no_sql"
STATUS_VALIDATOR_REJECTED = "validator_rejected"
STATUS_EXECUTION_ERROR = "execution_error"
STATUS_EXECUTED = "executed"


@dataclass
class EvalEnvironment:
    db_path: str
    schema: Dict[str, Any]
    semantic: Dict[str, Any]
    catalog: Catalog
    cache_key: str
    max_rows: int = DEFAULT_MAX_ROWS


def build_environment(db_path: str, max_rows: int = DEFAULT_MAX_ROWS) -> EvalEnvironment:
    """Same setup as api.connect_sample: schema -> semantic layer -> retrieval index."""
    from modules import build_semantic_layer, build_vector_store, load_database

    schema = load_database(db_path)
    semantic = build_semantic_layer(schema)
    cache_key = f"eval-{uuid.uuid4().hex[:12]}"
    build_vector_store(semantic, cache_key=cache_key)
    return EvalEnvironment(db_path, schema, semantic, build_catalog(schema), cache_key, max_rows)


@dataclass
class CaseResult:
    id: str
    category: str
    difficulty: str
    question: str
    expected_behavior: str
    decline_reason: Optional[str]
    pipeline_status: str = ""
    verdict: str = "incorrect"                  # correct | incorrect
    failure_mode: Optional[str] = None          # None when correct
    lenient_correct: bool = False               # decline cases only
    # generation
    sql: str = ""
    raw_response: str = ""
    confidence: Optional[str] = None
    provider: Optional[str] = None
    # validation / execution
    validator_valid: Optional[bool] = None
    validator_issue_types: List[str] = field(default_factory=list)
    execution_success: bool = False
    row_count: Optional[int] = None
    gold_row_count: Optional[int] = None
    # correctness
    result_match: Optional[bool] = None
    result_match_kind: Optional[str] = None     # exact | superset_columns
    result_mismatch_reason: Optional[str] = None
    # grounding
    tables_used: List[str] = field(default_factory=list)
    expected_tables: List[str] = field(default_factory=list)
    table_grounded: Optional[bool] = None
    table_exact: Optional[bool] = None
    columns_grounded: Optional[bool] = None
    retrieval_table_recall: Optional[bool] = None
    characteristics_match: Optional[bool] = None
    # hallucination
    sql_parseable: Optional[bool] = None
    hallucinated_tables: List[str] = field(default_factory=list)
    hallucinated_columns: List[str] = field(default_factory=list)
    hallucination: bool = False
    hallucination_sources: List[str] = field(default_factory=list)   # ast | sqlite
    identifier_refs: int = 0
    hallucinated_identifier_count: int = 0
    # cost / latency
    latency_ms: Dict[str, float] = field(default_factory=dict)
    estimated_cost_cents: Optional[float] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def rows_as_tuples(exec_result: Dict[str, Any], db_path: str) -> List[Tuple]:
    """execute_query() returns rows as dicts, which silently collapse
    duplicate output column names (e.g. two columns both called `id`). For
    comparison we need every column, so in that rare case re-read the exact
    executed statement as raw tuples (read-only)."""
    cols = exec_result.get("columns", [])
    if len(set(cols)) == len(cols):
        return [tuple(r[c] for c in cols) for r in exec_result.get("rows", [])]
    conn = open_readonly(db_path)
    try:
        return [tuple(r) for r in conn.execute(exec_result["sql"]).fetchall()]
    finally:
        conn.close()


@dataclass
class GoldResult:
    columns: List[str]
    rows: List[Tuple]
    truncated: bool


def compute_gold(case: BenchmarkCase, env: EvalEnvironment) -> GoldResult:
    """Run the case's gold SQL through the *same* executor the candidate uses."""
    from modules import execute_query, validate_sql

    v = validate_sql(case.gold_sql or "", env.schema, env.semantic)
    if not v.get("is_valid"):
        raise DatasetError(f"{case.id}: gold SQL is rejected by QuerySure's validator: "
                           f"{[i['message'] for i in v.get('issues', [])]}")
    res = execute_query(case.gold_sql or "", env.db_path, max_rows=env.max_rows)
    if not res.get("success"):
        raise DatasetError(f"{case.id}: gold SQL failed to execute: {res.get('error')}")
    if not res.get("rows") and not case.allow_empty_result:
        raise DatasetError(f"{case.id}: gold SQL returned no rows (set allow_empty_result if intended)")
    return GoldResult(res["columns"], rows_as_tuples(res, env.db_path), bool(res["execution_metadata"]["truncated"]))


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 3)


# ---------------------------------------------------------------------------
# one case
# ---------------------------------------------------------------------------

def evaluate_case(
    case: BenchmarkCase,
    env: EvalEnvironment,
    source: ResponseSource,
    gold: Optional[GoldResult],
) -> CaseResult:
    from modules import execute_query, generate_sql, retrieve_context, validate_sql

    r = CaseResult(
        id=case.id, category=case.category, difficulty=case.difficulty, question=case.question,
        expected_behavior=case.expected_behavior, decline_reason=case.decline_reason,
        expected_tables=list(case.expected_tables),
    )
    lat: Dict[str, float] = {}
    source.begin_case(case)
    t_total = time.perf_counter()

    # 1. retrieval (real)
    t = time.perf_counter()
    retrieval = retrieve_context(case.question, env.semantic, cache_key=env.cache_key)
    lat["retrieval"] = _ms(time.perf_counter() - t)
    if not case.should_decline:
        r.retrieval_table_recall = set(case.expected_tables) <= {x.lower() for x in retrieval.get("relevant_tables", [])}

    # 2. generation (real generate_sql; transport depends on the source)
    api_key = source.effective_api_key() if isinstance(source, LiveSource) else ""
    t = time.perf_counter()
    gen = generate_sql(question=case.question, schema=env.schema, api_key=api_key,
                       semantic=env.semantic, retrieval=retrieval)
    lat["generation"] = _ms(time.perf_counter() - t)

    r.raw_response = gen.get("raw_response", "") or ""
    r.confidence = gen.get("confidence")
    r.provider = gen.get("provider_used")
    sql = (gen.get("sql") or "").strip()
    r.sql = sql
    if source.calls_llm:
        # api.py refunds the reservation when generation fails: nothing was spent.
        generation_failed = gen.get("success") is False
        r.estimated_cost_cents = 0.0 if generation_failed else round(estimate_cost_cents(case.question, retrieval, gen), 6)

    # classify generation outcome
    if gen.get("success") is False:
        r.pipeline_status = STATUS_GENERATION_ERROR
        r.error = gen.get("error")
    elif not sql:
        r.pipeline_status = STATUS_NO_SQL

    # 3. independent analysis of whatever SQL exists (hallucination, grounding)
    analysis: Optional[SQLAnalysis] = None
    if sql:
        analysis = analyze_sql(sql, env.catalog)
        sq = sqlite_compile_check(sql, env.db_path)
        r.sql_parseable = analysis.parse_ok
        r.tables_used = list(analysis.tables)
        r.hallucinated_tables = list(analysis.hallucinated_tables)
        r.hallucinated_columns = list(analysis.hallucinated_columns)
        sources = []
        if analysis.hallucinated_tables or analysis.hallucinated_columns:
            sources.append("ast")
        if sq.is_hallucination:
            sources.append("sqlite")
        r.hallucination_sources = sources
        r.hallucination = bool(sources)
        if analysis.parse_ok:
            r.identifier_refs = analysis.table_refs + analysis.column_refs
            r.hallucinated_identifier_count = len(analysis.hallucinated_identifiers) or (1 if sq.is_hallucination else 0)

    # 4. validation (real) -- only meaningful when there is SQL
    if not r.pipeline_status:
        t = time.perf_counter()
        validation = validate_sql(sql, env.schema, env.semantic)
        lat["validation"] = _ms(time.perf_counter() - t)
        r.validator_valid = bool(validation.get("is_valid"))
        r.validator_issue_types = [i.get("type", "?") for i in validation.get("issues", [])]
        if not r.validator_valid:
            r.pipeline_status = STATUS_VALIDATOR_REJECTED
            r.error = "; ".join(i.get("message", "") for i in validation.get("issues", []))[:400]

    # 5. execution (real) -- only for validated SQL, exactly like /api/execute-sql
    exec_result: Optional[Dict[str, Any]] = None
    if not r.pipeline_status:
        t = time.perf_counter()
        exec_result = execute_query(sql, env.db_path, max_rows=env.max_rows)
        lat["execution"] = _ms(time.perf_counter() - t)
        if exec_result.get("success"):
            r.pipeline_status = STATUS_EXECUTED
            r.execution_success = True
            r.row_count = exec_result.get("row_count")
        else:
            r.pipeline_status = STATUS_EXECUTION_ERROR
            r.error = exec_result.get("error")

    lat["total"] = _ms(time.perf_counter() - t_total)
    r.latency_ms = lat

    # 6. judge against the expectation
    if case.should_decline:
        _judge_decline(r)
    else:
        _judge_answer(r, case, gold, exec_result, analysis, env)
    return r


def _judge_decline(r: CaseResult) -> None:
    strict = r.pipeline_status in (STATUS_NO_SQL, STATUS_VALIDATOR_REJECTED)
    # Lenient additionally credits a run-time database error (the bad request
    # did not produce data). It deliberately does NOT credit "answered with
    # Low confidence": generate_sql() forces confidence="Low" whenever mean
    # retrieval score < config.LOW_RETRIEVAL_CONFIDENCE. It is a hint, not a
    # refusal, so it is never credited as declining. Its calibration is
    # reported separately (metrics: confidence_signal).
    r.lenient_correct = strict or r.pipeline_status == STATUS_EXECUTION_ERROR
    if strict:
        r.verdict, r.failure_mode = "correct", None
        return
    r.verdict = "incorrect"
    if r.pipeline_status == STATUS_GENERATION_ERROR:
        r.failure_mode = "generation_error"          # outage: never credited as a rejection
    elif r.pipeline_status == STATUS_EXECUTION_ERROR:
        r.failure_mode = "execution_error"
    else:
        r.failure_mode = "answered_instead_of_declining"   # returned data for a question it should have declined


def _judge_answer(r: CaseResult, case: BenchmarkCase, gold: Optional[GoldResult],
                  exec_result: Optional[Dict[str, Any]], analysis: Optional[SQLAnalysis],
                  env: EvalEnvironment) -> None:
    used = set(analysis.tables) if analysis and analysis.parse_ok else set()
    expected_t = set(case.expected_tables)
    r.table_grounded = bool(expected_t) and expected_t <= used
    r.table_exact = bool(expected_t) and expected_t == used
    cols = set(analysis.columns) if analysis and analysis.parse_ok else set()
    r.columns_grounded = set(case.expected_columns) <= cols if case.expected_columns else None
    if case.expected_characteristics:
        feats = analysis.features if analysis and analysis.parse_ok else {}
        r.characteristics_match = all(feats.get(k) == v for k, v in case.expected_characteristics.items())

    if gold is not None:
        r.gold_row_count = len(gold.rows)

    if r.pipeline_status == STATUS_EXECUTED and exec_result is not None and gold is not None:
        cand_rows = rows_as_tuples(exec_result, env.db_path)
        strict: Comparison = compare_results(gold.rows, cand_rows, ordered=case.ordered)
        loose: Comparison = strict if strict.match else compare_results(
            gold.rows, cand_rows, ordered=case.ordered, allow_extra_columns=True)
        r.result_match = strict.match
        r.result_match_kind = "exact" if strict.match else ("superset_columns" if loose.match else None)
        r.result_mismatch_reason = None if strict.match else strict.reason
        if strict.match:
            r.verdict, r.failure_mode = "correct", None
        else:
            r.verdict, r.failure_mode = "incorrect", "result_mismatch"
        return

    r.verdict = "incorrect"
    r.failure_mode = r.pipeline_status  # generation_error | no_sql | validator_rejected | execution_error


# ---------------------------------------------------------------------------
# many cases
# ---------------------------------------------------------------------------

def run_cases(
    cases: Sequence[BenchmarkCase],
    source: ResponseSource,
    env: EvalEnvironment,
    progress: Optional[Callable[[int, int, CaseResult], None]] = None,
) -> List[CaseResult]:
    # Gold results are computed (and gold SQL validated) BEFORE any candidate
    # runs, and are excluded from latency. A broken gold query is a dataset bug
    # and aborts loudly instead of quietly failing every candidate.
    golds: Dict[str, Optional[GoldResult]] = {}
    for c in cases:
        golds[c.id] = None if c.should_decline else compute_gold(c, env)

    results: List[CaseResult] = []
    with source.activate():
        for i, case in enumerate(cases, 1):
            res = evaluate_case(case, env, source, golds[case.id])
            results.append(res)
            if progress:
                progress(i, len(cases), res)
    return results
