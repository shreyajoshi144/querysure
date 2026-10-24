# QuerySure evaluation layer

A reproducible harness that measures whether the NL → SQL pipeline works and how
often it hallucinates. It drives the **existing** modules (`retrieve_context` →
`generate_sql` → `validate_sql` → `execute_query`) without modifying them.

```
python -m evaluation validate-dataset            # is the benchmark itself sound?
python -m evaluation run --mode oracle           # harness self-check      (offline, free)
python -m evaluation run --mode synthetic        # injected-defect demo    (offline, free)
python -m evaluation run --mode replay --responses recorded.json   # re-score saved LLM replies (offline, free)
python -m evaluation live                        # REAL LLM  ->  needs API keys, costs money
python -m evaluation list-cases --category joins
```

Everything except `live` needs no API key, no network, no AWS, Redis or Docker.
The sample retail database is built on the fly by the repo's own `create_retail.py`
into a temp directory and discarded afterwards.

## Read this before quoting a number

| Mode | What produces the SQL | What the numbers mean |
|---|---|---|
| `oracle` | gold SQL for answerable cases, a refusal for the rest | **Self-check only.** 100% is expected. It proves the dataset and harness are consistent, and it measures the deterministic layers (validator, executor, retrieval). It says nothing about an LLM. |
| `synthetic` | oracle + 23 hand-written, labelled defects (`fixtures/synthetic_flawed.json`) | **A demo of the metrics reacting to failures.** The 72.7% it prints is a property of the fixture I wrote, not of any model. |
| `replay` | raw LLM replies recorded from an earlier `live` run | Real LLM quality, re-scored offline, exactly and for free. |
| `live` | QuerySure's real provider chain (Groq → OpenAI → Anthropic, whichever are configured) | **The only mode that measures LLM quality.** Costs money. |

In all offline modes the real `generate_sql()` still runs: prompt construction, SQL
extraction from the reply text, confidence parsing and the retrieval-score downgrade.
Only the single network call (`_run_provider_chain`) is swapped, inside a context
manager that restores it afterwards. Offline latency therefore excludes LLM time.

## Metrics and their denominators

Every rate names its denominator; an empty denominator yields `null`, never `0`.

**A** = answerable cases (44), **D** = should-decline cases (18), **S** = cases where any SQL was produced.

| Metric | Definition |
|---|---|
| SQL validity rate | of A: SQL that QuerySure's own validator accepts |
| Execution success rate | of A: validated SQL that ran without a database error |
| **Execution accuracy** | of A: executed **and** result set equals the gold result |
| …tolerating extra columns | same, but a candidate with extra columns still counts (reported separately, never blended in) |
| …given the query executed | of A that executed: how many were right. Separates "wrong answer" from "no answer" |
| Expected-table grounding | of A: SQL references every table the case expects |
| Column grounding / characteristics | of the cases that declare expected columns / structure (e.g. needs a `GROUP BY`) |
| **Hallucination, case level** | of S: SQL references a table or column that does not exist |
| Hallucination, identifier level | bad identifier references ÷ all identifier references in parseable SQL |
| Validator escape rate | of hallucinating SQL: fraction the validator still marked valid (should be 0) |
| **Rejection/clarification (strict)** | of D: no SQL was produced, or the validator blocked it |
| …lenient | also credits a run-time database error |
| Answered instead of declining | of D: data came back for a question that should have been declined |
| Behavior accuracy | of all cases: answered correctly *or* declined correctly |
| Latency | mean / median / p95 / max per case, plus per-stage means |
| Estimated cost | `live` only; see below |

### Execution accuracy compares results, not SQL text

Rows are compared as a multiset. Row order is ignored unless the case is a ranking
question (`ordered`). Column order and names are ignored (columns are matched by their
contents). Ints equal floats; floats compare at 4 decimals. NULL matches only NULL.
Types are otherwise strict: `'2023'` ≠ `2023`. Duplicate rows matter, so a missing
`DISTINCT` that changes the row count is a mismatch. `tests/eval_layer/test_eval_compare.py`
pins each rule, and three synthetic cases (`join-007`, `grp-002`, `agg-001`) are
correct answers written differently from the gold SQL to prove they aren't penalised.

### Hallucination is measured independently of the validator

If the evaluation only believed `validate_sql`, a validator blind spot would show up as
"no hallucination". So a case counts as hallucinating if **either** of two mechanisms
finds a non-existent identifier: an AST scope check against the real database catalog
(`analysis.py`), or SQLite itself (`EXPLAIN` on a read-only connection reports "no such
table/column"). The report then states how many hallucinating queries the validator
let through.

### Cost

The app charges a user's daily AI budget with a
character-count heuristic (`api._estimate_cost_cents`, ~0.15¢ per 1K tokens). The
harness mirrors that formula exactly (a parity test fails if `api.py` drifts), so the
figure equals what QuerySure would have charged against a user's daily budget. It is
**budget accounting, not a provider invoice** — treat it as order-of-magnitude. Offline
modes report `n/a`. A failed generation costs 0, as in `api.py`.

## The benchmark

`datasets/retail_benchmark.json` — 62 cases over the sample retail database.

| Category | Cases | Expected behaviour |
|---|---:|---|
| simple_filtering, aggregation, group_by, joins, date_time, multi_step | 44 | answer; result compared to gold |
| ambiguous | 5 | ask for clarification |
| impossible (no such table/column) | 6 | decline |
| adversarial / invalid (injection, writes, prompt exfiltration, gibberish) | 7 | decline |

Difficulty: 20 easy / 29 medium / 13 hard. Each answerable case stores the question,
gold SQL, expected tables, expected columns, expected structural characteristics
(only ones inherent to the question — a "per region" question needs a `GROUP BY`; a
join vs. subquery choice is left open), and whether row order matters. Decline cases
store a reason (`ambiguous`, `nonexistent_data`, `adversarial`, `invalid_input`).

`python -m evaluation validate-dataset` checks the yardstick itself: every gold query
must pass QuerySure's validator, execute, reference only real identifiers, touch exactly
the declared tables, return a non-truncated result, and (for ranking cases) have **no
ties** in the ranked rows — otherwise two correct answers could legitimately differ. The
database is pinned by a content fingerprint stored in the dataset.

Adding a case: append to the JSON (loading is strict: unknown keys, missing gold SQL,
category/behaviour mismatches and duplicate questions all fail loudly), then run
`validate-dataset`.

## Running against a real LLM

```
export GROQ_API_KEY=...            # and/or OPENAI_API_KEY / ANTHROPIC_API_KEY, or put them in .env
python -m evaluation live --record-responses evaluation/results/run1_responses.json
python -m evaluation run --mode replay --responses evaluation/results/run1_responses.json   # re-score free
```

`live` refuses to start without credentials (exit 2), prints the provider fallback chain
and the case count, and asks for confirmation (`--yes` to skip; it will not spend money
non-interactively without it). Use `--limit`, `--category`, `--ids` and `--delay-seconds`
to control spend and rate limits. Only the LLM variables from `.env` are read, and
variables already in the environment win.

**Gating in CI:** `--min-execution-accuracy 0.6 --max-hallucination-rate 0.05` etc. exit 1
when violated. A metric that could not be computed fails its gate rather than passing.

## Findings about the existing system (from the oracle run, no LLM involved)

* **The "Low confidence" flag was recalibrated (v1.0.7).** It used to fire whenever the mean
  retrieval score was < 0.15, which marked ~82% of *correctly* answered questions Low. On this
  benchmark every answerable question scores >= 0.062 and "nothing in the schema matches" sits
  at ~0.035, so the cutoff is now `LOW_RETRIEVAL_CONFIDENCE=0.05`: 0% of correct answers are
  flagged (oracle) and 2 of the 4 wrongly answered should-decline cases are (synthetic). It was
  calibrated on the same 62 cases it is measured on, so re-check it on live results. The lenient
  rejection metric still never credits a Low flag.
* **Result-verifier false alarms on correct SQL were fixed (v1.0.7).** Running the verifier on
  the 44 gold queries used to give 3 `mismatch` verdicts (counting one table grouped by another,
  a HAVING threshold, and a date range the planner could not model); now 0. Remaining warnings on
  correct SQL: one `suspicious_empty` (the question asks for a status value that does not exist,
  which is worth flagging) and one semantic-check note on a nested "average per order".
* The validator (`sql_validator.py`) blocked every hallucinated identifier and every
  write statement injected in the synthetic run (validator escape rate 0%).

## Known limitations

* One database (the retail sample), 62 cases. Treat single-digit percentage differences
  as noise; the categories have 5–8 cases each.
* The gold SQL was written by the harness author, not independently reviewed. The
  integrity check guarantees it is executable and unambiguous, not that the question
  wording is the only reasonable reading. Ambiguity notes are in each case's `notes`.
* "Ambiguous" and "impossible" verdicts are behavioural, not semantic. A refusal counts
  as correct even if its stated reason were wrong; the harness does not grade
  explanations. A model that refuses everything scores 100% on D and 0% on A —
  hence `over_rejection_rate` and `behavior_accuracy`.
* QuerySure currently has no clarification mechanism: it either produces SQL or not.
  "Clarify" is therefore scored as "declined to answer", same as "reject".
* Unqualified columns are resolved against the union of referenced real tables, not
  per-subquery scope. This can under-count a hallucination, never invent one.
* Comparison caps at 1000 rows (the executor's default). The integrity check refuses any
  gold query that reaches the cap.
* `retrieval_table_recall_rate` is trivially high on a 5-table schema; it becomes
  informative only on larger databases (`--db`, `--dataset`).
* Latency is single-threaded wall-clock on the machine that ran it.

## Layout

```
evaluation/
  models.py       case schema + strict validation
  benchmark.py    loader
  datasets/       retail_benchmark.json
  db.py           sample DB via create_retail.py, private working copy, fingerprint
  analysis.py     independent hallucination + structure analysis (sqlglot AST + SQLite EXPLAIN)
  compare.py      result-set equivalence
  sources.py      where LLM text comes from: oracle / synthetic / replay / live
  runner.py       runs each case through the real pipeline, judges the outcome
  metrics.py      aggregation, denominators, threshold gates
  cost.py         mirror of api.py's cost heuristic
  integrity.py    benchmark self-checks
  harness.py      one call = one full evaluation
  report.py       console / Markdown / JSON rendering
  cli.py          python -m evaluation
  fixtures/       synthetic_flawed.json
  examples/       real output of the oracle and synthetic runs
  results/        your reports land here (git-ignored)
tests/eval_layer/ 179 tests for the framework itself
```

## DeepEval (supplementary layer)

DeepEval is integrated as an additional LLM-as-a-judge layer. The original 62-case
benchmark and deterministic SQL/result-equivalence metrics remain unchanged and
remain the source of truth for SQL correctness, execution, hallucination, grounding,
and rejection behaviour.

```bash
python -m evaluation deepeval-golden --mode live --yes
python -m evaluation deepeval-golden --mode replay --responses evaluation/results/run1_responses.json
python -m evaluation deepeval-synthetic --num 25
python -m evaluation deepeval-synthetic --num 25 --run
```

See `evaluation/DEEPEVAL.md` for provider configuration, synthetic-data limitations,
metric interpretation, and cost boundaries.

## Multi-format evaluation (`evaluation/multiformat/`)

Real files in every supported format (CSV, Excel, JSON, SQLite `.sqlite` and `.db`) go through QuerySure's real upload
path. Each question has an English version plus Spanish, French, German and Hindi translations, informal, typo-ridden and
incomplete variants. Expected answers are computed from the raw source files with pandas, `json`, `openpyxl` or `sqlite3`
table reads, never from QuerySure's SQL.

```
python -m evaluation.multiformat              # tuned set: candidate SQL supplied, no LLM, free
python -m evaluation.multiformat --holdout    # held-out questions, never used to tune the pipeline
python -m evaluation.multiformat --negatives  # deliberately wrong SQL: is it flagged?
python -m evaluation.multiformat --live --yes [--holdout] [--delay 20]   # the configured LLM writes the SQL (costs money)
python -m evaluation.multiformat --replay live.json [--holdout]         # re-check SQL saved by --live --json (free)
```

Source files are read from `$QUERYSURE_EVAL_DATA` or `tests/fixtures/user/` (`business-financial-data-march-2026-quarter.csv`,
`Project-Management-Sample-Data.xlsx`, `european-countries.json`, `northwind_small.sqlite`, `chinook.db`); missing
datasets are skipped and listed. Reported per dataset and language:

| Metric | Meaning |
|---|---|
| exact-result correct | the executed result equals the independent answer (rules as above) |
| semantically correct | same answer, tolerating extra columns and rounding to 2 decimals |
| clean | correct, with no bogus plan/intent warning, verification mismatch or repair request |
| retrieval recall | every table the correct SQL needs was in the schema shown to the model |
| ingestion fidelity | each ingested column equals the source column (row counts for SQLite) |
| detected (negatives) | a wrong query was rejected or flagged |

Offline modes measure the deterministic pipeline given correct SQL. Only `--live` measures a model. `--replay` re-checks a live run's final SQL with today's pipeline (repair rounds are not re-run). Live reports count calls the provider refused (quota, rate limit, auth) separately and give accuracy over the answered runs.
