# QuerySure — Evaluation

What has been measured, how, and what has not. Results are grouped by where they come from:

- **Verified in this review**: re-run on 2026-10-23 and 2026-10-24 on macOS with Python 3.12 (fresh virtualenv, `requirements.txt` + `requirements-dev.txt`), SQLite, without the Node.js test dependencies. **The pytest suite ran on SQLite, without a PostgreSQL server. The owner's Docker Compose stack (PostgreSQL 16, Redis, worker, API on port 5001) was exercised by hand on 2026-10-24 (section 3.2). A live LLM was called only for section 3.1's partial runs and section 3.2's questions.**
- **Recorded earlier**: results written down in this repository by earlier sprints and not reproduced in this review. They are labelled where they appear.

Related: [Product](01_PRODUCT.md) · [Architecture](02_ARCHITECTURE.md) · [Sprint history](SPRINT_HISTORY.md)

## 1. Automated tests

| Check | Command | Result (verified in this review) |
|---|---|---|
| Full test suite (Python 3.12, SQLite) | `python -m pytest -q` | **1,171 passed, 0 failed, 14 skipped** (2026-10-24, after the hierarchy and packaging fixes; run on a copy without `.env`). From the unpacked release ZIP: 1,167 passed, 0 failed, 18 skipped (the 4 extra skips need the private evaluation files) |
| Lint (project configuration) | `ruff check .` | Passed |
| Dependency audit | `pip-audit -r requirements.txt` | No known vulnerabilities |
| Compose file | `docker compose config -q` | Valid (configuration only; containers not started) |

The suite has 79 test files. Areas covered: SQL validation and the execution sandbox, prompt injection, authentication and sessions, tenant isolation, quota and AI-budget atomicity under concurrency, ingestion limits, provider fallback, history, billing webhooks, log redaction, deployment configuration, and UI rendering.

**The 14 skipped tests**, all for missing prerequisites:

| Count | Tests | Prerequisite not present |
|---:|---|---|
| 5 | `test_xss_escaping.py` (jsdom runs the real `static/js/app.js`) | Node.js dependencies (`npm ci` not run) |
| 2 | `test_sprint5_e2e.py` (real-browser end-to-end) | Node.js, puppeteer-core, Chromium |
| 1 | `test_ui_rendered.py` | Playwright |
| 2 | `test_sprint4_backup_recovery.py` (restore drill), `test_v103_render_blockers.py` (concurrent `init_db`) | PostgreSQL server |
| 1 | `test_sprint4_ops_privacy.py` (Sentry scrubbing) | `sentry-sdk` (optional dependency) |
| 3 | `test_v104_format_reliability.py` (`sales.csv/.xlsx/.json`) | Private sample files not in the repository |

In addition, `test_sprint2_concurrency.py::test_postgres_concurrent_writers_no_lost_update` is only collected when `TEST_DATABASE_URL` points at PostgreSQL, so it did not run. **The PostgreSQL-only, XSS and browser tests did not run in this review.** CI (`.github/workflows/ci.yml`) defines PostgreSQL 16, migration and Node-enabled jobs; their results are not part of this review.

**Recorded earlier (not reproduced):** 1,094 passed on SQLite with Node and browsers available, and 1,092 passed on PostgreSQL 16 (runs dated 2026-10-22/23 in the previous evaluation report).

### Compose worker healthcheck regression

The only failing test at the start of this review was `test_compose_has_a_worker_without_the_api_http_healthcheck`. `docker-compose.yml` gave the worker a `curl` probe on port 5000, but the image has no `curl` and the worker serves no HTTP, so it could never become healthy. The fix makes the worker's healthcheck look for its own Redis heartbeat key (`qs:g:worker:<hostname>:*`, written every 2 s with a 30 s TTL by `observability/gauges.py`). A new test runs that exact command against an in-memory Redis and checks four cases: healthy with a heartbeat, unhealthy with none, unhealthy with another container's heartbeat, and unhealthy with only a web-process heartbeat. The command was also checked to exit non-zero when Redis is unreachable. Both tests fail against the old Compose file and pass against the new one. Not verified: the healthcheck inside a running container.

## 2. AI evaluation methodology

`evaluation/` runs the real pipeline on a **62-case benchmark** over the bundled retail database (5 tables, generated deterministically by `create_retail.py`).

| Part | Contents |
|---|---|
| Answerable cases (44) | Simple filtering 7, aggregation 8, grouping 6, joins 8, dates 8, multi-step 7. Each stores gold SQL, expected tables and columns, and whether row order matters |
| Should-decline cases (18) | Ambiguous 5, impossible (no such table/column) 6, adversarial or invalid 7 (injection, writes, prompt exfiltration, gibberish) |
| Difficulty | 20 easy, 29 medium, 13 hard |
| Grading | **Result sets are compared, not SQL text**: rows as a multiset, order only for ranking questions, columns matched by content, floats to 4 decimals |
| Hallucination | Detected independently of the app's validator (AST scope check against the catalog, plus SQLite `EXPLAIN`); the report states how many hallucinating queries the validator let through |

| Mode | Where the SQL comes from | What it can tell you |
|---|---|---|
| `validate-dataset` | Gold SQL | Whether the benchmark itself is sound |
| `oracle` | Gold SQL replayed | Whether the harness and deterministic layers are consistent; pipeline overhead excluding the LLM. **Says nothing about a model** |
| `synthetic` | Oracle plus 23 hand-written, labelled defects | Whether the metrics react to known failures. **The accuracy it prints describes the fixture, not a model** |
| `replay` | Raw LLM replies recorded from an earlier live run | Real model quality, re-scored offline |
| `live` | QuerySure's real provider chain | **The only mode that measures LLM accuracy, cost and latency. Not run yet** |

## 3. AI evaluation results

| Mode | Result (verified in this review) |
|---|---|
| `validate-dataset` | OK: 62 cases (44 answerable, 18 should-decline); all gold SQL validates, executes and is unambiguous |
| `oracle` | 62/62 correct; validity, execution and execution accuracy 100% of 44; 0% hallucination; 100% of 18 declines; "Low confidence" on correct answers 0%; latency excluding LLM **9.8 ms mean / 16.1 ms p95** on this Mac |
| `synthetic` | Validator let through **0 of 5** hallucinating queries; "answered instead of declining" 22.2% and execution accuracy 72.7% (both properties of the injected defects); "Low confidence" on correct answers 0% and on wrongly answered declines 50% |
| `live` / `replay` (62-case benchmark) | **Not run.** Live model results on the separate multi-format suite are in section 3.1 |

### 3.1 Multi-format evaluation (2026-10-24)

`python -m evaluation.multiformat` runs QuerySure's real pipeline on five files supplied by the owner (not generated), and compares the results with answers computed **independently with pandas from the raw files**, not by QuerySure's own SQL.

| File | Format | Cases | Runs incl. variants |
|---|---|---:|---:|
| Stats NZ business financial data, Mar 2026 quarter | CSV | 14 | 37 |
| Project management sample | Excel | 16 | 39 |
| European countries | JSON | 16 | 41 |
| Northwind (small) | SQLite | 15 | 34 |
| Chinook | SQLite `.db` | 12 | 29 |

Each case has an English question plus up to seven variants (Spanish, French, German, Hindi, informal, typo, incomplete): **180 tuned runs**. **32 held-out runs** (14 cases) were written after the fixes and never used to tune them, and **24 negative cases** are deliberately wrong SQL. Grading compares result sets: *exact* means the same rows, and *semantic* also tolerates extra columns and 2-decimal rounding. *Clean* means no false alarm, no verifier mismatch and no repair request on a correct answer. *Retrieval recall* means every table the correct SQL needs was in the model's prompt.

**Deterministic pipeline, candidate SQL supplied (no LLM).** This measures grounding, planning, validation, execution and verification, not a model:

| Metric | Original code | After fixes |
|---|---|---|
| Tuned runs exactly correct (of 180) | 94.44% (10 correct queries rejected: 8 Northwind joins, 2 `SELECT *`) | **100%** |
| Clean (no false alarm on a correct answer) | 75.56% | **100%** |
| False alarms on correct answers | 18.89% | **0%** |
| Retrieval recall | 74.44% (Northwind 8.8%, Chinook 48%) | **100%** |
| Held-out runs (of 32), first run after the first fix round | n/a | 100% correct, 93.75% clean; the 2 defects found were fixed; now 100% / 100% |
| Negative cases detected (of 20 detectable) | 17 | **20**; the 4 "hard" cases (plausible but wrong grain or filter) remain undetected |
| Ingestion fidelity (row counts and values vs raw file) | all match | all match; CSV `Period` stored as a number, same values |

**Live model.** Groq `openai/gpt-oss-120b` with fallback `gpt-oss-20b`; OpenAI and Anthropic keys failed (`rate_limited`, `auth`).

| Run | Runs | Refused by the provider | Answered | Exact (of answered) | Note |
|---|---:|---:|---:|---:|---|
| Live 1, tuned | 180 | 100 | 80 | 56.25% | Before the later fixes; $0.05 |
| Live 1, held-out | 32 | 9 | 23 | 52.2% | |
| Live 2, held-out | 32 | 30 | 2 | 1 of 2 | Groq daily quota exhausted |
| Live 2, tuned | stopped | — | — | — | Every call refused |

**No complete live measurement exists.** Groq's free tier allows 200,000 tokens per model per day and 8,000 tokens per minute. One Northwind question costs about 4,000 prompt tokens plus the output reservation, so a full run (about 250 calls) does not fit in a day.

**Replay of the model's recorded SQL** (`--replay`, no new LLM calls). The final SQL from live run 1 was re-checked by the original pipeline and by the current one. Repair rounds are not re-run, so this isolates what the deterministic layers change:

| | Tuned, 80 answered: original → now | Held-out, 23 answered: original → now |
|---|---|---|
| Exact | 56.25% → **57.5%** | 52.17% → **60.87%** |
| Semantic | 63.75% → **65.0%** | 60.87% → **69.57%** |
| Accepted by the validator | 87.5% → **100%** | 82.61% → **100%** |
| Clean | 76.25% → **97.5%** | 65.22% → **100%** |
| False alarms on correct answers | 8.75% → **0%** | 13.04% → **0%** |

**The 99% target is not met with a live model.** Measured live accuracy on answered questions is 57.5% (tuned) and 60.9% (held-out) exact, and 65% / 69.6% semantic. The remaining 35 wrong answers are the model's SQL, which the pipeline now accepts and runs:

| Cause | Runs | Example |
|---|---:|---|
| The Stats NZ CSV is a hierarchical statistical file: each industry appears under several `group` levels and variables. The model omits the Level 1 `group` or the `series_title_1` filter, or groups by `group` | 22 | csv-01, csv-06, csv-07, csv-08, csv-10 to csv-13, h-csv-01 |
| Expected ordering, or a column the question did not ask for | 6 | h-x-02 (list in name order), h-j-03 (expects the country name too), xlsx-05 hi |
| The model chose a different grain or presentation | 4 | xlsx-05 (unordered percentage), xlsx-04 (projects with 0 completed omitted) |
| Plain model errors | 3 | csv-02 `COUNT(DISTINCT data_value)`; xlsx-01 informal `LIMIT 10`; xlsx-13 a non-aggregated `end_date` (now flagged, which triggers a repair) |

The expectations were **not** changed after seeing these results. The root cause of each pipeline fix is listed in [CHANGELOG.md](../CHANGELOG.md) under *Accuracy and reliability*; each fix has a test in `tests/test_v108_accuracy_regressions.py`, and 41 of its 52 tests fail on the original code. `tests/test_v108_multiformat_eval.py` runs the tuned, held-out and negative suites when the source files are present (`tests/fixtures/user/` or `$QUERYSURE_EVAL_DATA`) and fails on any wrong or unclean run.

### 3.2 End-to-end check on the running Docker Compose stack (2026-10-24)

Run against the owner's existing stack: API on port 5001, PostgreSQL 16, Redis and the RQ worker. `api` and `worker` were rebuilt with `docker compose up -d --build --no-deps api worker`; `db` and `redis` were not recreated. Two temporary, pre-verified test users were created directly in the database so that no e-mail was sent, and were removed afterwards with the app's own *Delete account* endpoint. The SQL was written live by Groq `openai/gpt-oss-120b` (one answer by the `gpt-oss-20b` fallback).

| Area | What was done | Result |
|---|---|---|
| Health and persistence | `/api/health`, `/api/ready`, Compose health; row counts before and after the rebuild and after `docker compose restart db` | All four services healthy, including the worker's Redis-heartbeat check. Users, history, sessions and usage counts unchanged. Volume `querysure_querysure_pgdata` kept (same creation time) |
| Sign-in | E-mail/password form in the browser | Works |
| JSON upload (UI) | `european-countries.json` through the upload input | 43 rows, 7 columns. "¿Cuántos países son miembros de la UE?" gave 26 (independent count: 26). "Which EU member has the largest area?" gave France, 643,801 |
| Sample database (UI) | "Which region placed the most orders?" | North, 1,503: correct. **Found two defects, now fixed:** the verifier reported *Mismatch* (its reference query counted stores per region), and the findings said "North contributes 100.00%" (share of the single returned row; the real share is 1,503 of 3,000 = 50.10%). After the fix: *Not independently verified*, reliability 89/100, "North contributes 50.10% of total order_count" |
| Dataset switch (UI) | Sample answer on screen, then a JSON upload | Question box cleared, answer sheet hidden, results cleared, the pill shows the new dataset. **Found:** the hidden sheet kept the old question text; now cleared |
| Failed upload (UI) | A text file named `broken.xlsx` while JSON was active | Error shown, JSON stays active, Ask and Upload re-enabled; the next question ran on the JSON data. **Found:** the message was a generic "please try again"; it now shows the worker's reason ("Excel workbook is not a valid ZIP container.") |
| Excel, CSV, SQLite (API, same calls as the UI) | Upload, job poll, schema, semantic layer, retrieval, generate, validate, execute, reliability, insights | xlsx-01 and csv-09: exact match with the pandas answers (csv-09 also `verified_match`). ch-01: exact match. nw-04: same counts plus an extra `shipper_id` column (semantically correct, not exact). Shares in the findings use the full total: Marketing 8.70% (4/46), Rock 35.50% (826.65/2,328.60), United Package 39.28% (326/830) |
| History (UI) | List, open an entry, Delete then Cancel, Delete then Confirm; a run-time failure (`abs()` overflow) | Read-only snapshot ("has not been re-run"); no Reopen or Rerun; Cancel keeps the entry; Confirm removes it (API then 404); the failed run shows "Result not saved" |
| Isolation | User B against user A's data | B's history is empty; B gets 404 reading or deleting A's entry and 404 opening A's session; A's entry survives; no token gets 401 |
| Header layout | 375, 1,024 and 1,280 px | All four tabs visible and clickable; no horizontal scroll. **Found at 1,024 px:** the tabs were covered by the dataset pill; fixed |
| Cleanup | Both test accounts deleted through `/api/account/delete` | 0 rows remain for either ID in users, history, sessions, jobs, reports, usage and auth sessions; old tokens get 401. The owner's account and its counts are unchanged |

**Not verified end to end:** Excel and CSV uploads through the browser's file picker (the pane cannot open local files; those formats went through the same upload API instead), Google sign-in, e-mail delivery, the report download, and anything on Render. With no volume for uploads (deliberate, see `docker-compose.yml`), **uploaded dataset files are discarded whenever the `api` container is recreated**: the rebuilds above removed the files behind two of the owner's existing sessions. History snapshots, which are stored in PostgreSQL, were not affected.

### 3.3 Hierarchical statistics files (Stats NZ CSV), 2026-10-24

**Root cause of 22 of the 35 wrong live answers in section 3.1.** The Stats NZ file stores every figure as a row keyed by period, industry level (`group`: NZSIOC Level 1 with 14 industries, Level 2 with 26; both add up to the same totals, and 9 industries such as Mining appear at both levels with identical rows), variable (`series_title_1`) and version (`series_title_4`: unadjusted, seasonally adjusted, trend). `series_title_3` relabels `group` one-to-one. The model added levels or versions together, grouped by the level column, used it as the industry name, or wrote `suppressed <> 'Y'` on a column that is empty on 99.8% of rows (which removes every row).

**Fix (`modules/long_format.py`).** The structure is detected from the data alone, with no column names or dataset-specific rules:

- **Long-format table:** no row identifier, and a key of few-valued columns.
- **Hierarchy level:** a column whose values hold the same totals at different granularities; the top level is the one with the fewest entities.
- **Versions:** a column whose values give the same per-series totals over the periods they share.
- **Aliases:** columns that relabel another one-to-one.

On the five evaluation files only the Stats NZ table is detected. The prompt describes the structure. The planner fixes one level (from "top-level", "level 2", or the level that contains a named industry, otherwise the top level) and one version (named, otherwise the most complete). A variable is fixed only when the question names it with a word unique to one stored value. The semantic check flags SQL that adds levels, versions or variables together, groups by or names the level column, uses `<>`/`NOT IN` on a mostly-NULL column, or returns a bare ratio for a percentage question. When the missing filter has one right value, it is added deterministically and listed under the answer's fixes. The validator accepts a long-format table joined with itself on its key columns.

| Measurement | Before | After |
|---|---|---|
| Gold SQL, tuned and held-out (212 runs): correct / clean | 100% / 100% | 100% / 100% (no new false alarms) |
| Model SQL recorded in live run 1, CSV runs: wrong answers flagged | 2 of 17 | 15 of 17, and 0 of 24 correct answers flagged. Replayed, 23 of 41 recorded CSV runs are exactly correct (was 18; 24 semantically) because the deterministic filter repairs them |
| Negative cases (deliberately wrong SQL) | 20/20 detectable, 0/4 hard | 20/20 detectable, 1/4 hard ("Level 1 and Level 2 mixed"). Two are now corrected deterministically, and the corrected result matches the independent answer |
| Regression tests | n/a | `tests/test_v109_long_format.py` (16 tests): a generated statistics table whose level-2 industries roll up exactly into level-1 parents; every expected answer is computed in Python from the generating data, never by SQL |

**Live, Groq `openai/gpt-oss-120b`, CSV questions only:**

| Run | Answered | Exact | Semantic | Notes |
|---|---:|---:|---:|---|
| Live run 1 (section 3.1), tuned CSV, replayed through the pipeline before this fix | 36 | 41.7% (15) | 41.7% | Recorded SQL, no new LLM calls |
| Run 3 (first version of this fix), tuned CSV | 37 | **62.2%** (23) | 64.9% (24) | Top-level ranking csv-01: 6 of 7 exact (was 0 of 7) |
| Run 3, held-out CSV | 5 | **100%** (5) | 100% | |
| Run 4 (all checks, deterministic filters), tuned CSV | 30 (7 refused by the quota) | **63.3%** (19) | 66.7% (20) | 2 answers correct only after a repair round; 0 correct answers left with a warning |
| Run 4, held-out CSV | 0 (5 refused) | n/a | n/a | Groq daily quota exhausted |

The checks catch most of the remaining errors, but the model's repair round often does not fix them. Live accuracy on this file is about **63% exact on answered questions, far from 99%**. Run-to-run variation is large, and the samples are small (30 to 37 runs). The remaining wrong answers are mostly:

- **Questions in other languages:** "ventes" or "Umsatz" is not matched to the English variable values, so the sales filter is not enforced.
- **No period filter:** the model omitted it for "the March 2026 quarter".
- **Ambiguous question:** "number of industries" can mean one level or all names.
- **Median written with the wrong row index.**

### Findings that led to changes (v1.0.7, recorded earlier)

These before/after figures come from the v1.0.7 evaluation runs and the changelog. The "after" state is covered by the tests named.

| Finding | Before | After | Test |
|---|---|---|---|
| Verifier flagged correct gold queries as `mismatch` (of 44) | 3 | 0, while a deliberately wrong count is still caught | `test_v107_final.py::test_a_real_mismatch_is_still_caught` |
| "Low confidence" on correctly answered questions (oracle) | 81.8% | 0% (cutoff lowered from 0.15 to 0.05; tuned on this benchmark) | Oracle run above |
| Default Groq and Anthropic models | Retired by the providers, so generation failed with only a Groq key | `openai/gpt-oss-120b` → `openai/gpt-oss-20b`, `claude-haiku-4-5-20251001` | `test_v107_final.py::test_default_models_are_not_the_retired_ones` |
| SQL posted directly to `/api/execute-sql` | Ran without validation | Re-validated on every call | `test_sprint3_prompt_injection.py` (posts `DROP TABLE` straight to `/api/execute-sql`) |
| Known dependency vulnerabilities (`pip-audit`) | 14 | 0 (FastAPI 0.159, Starlette 1.9, PyJWT) | `pip-audit` above |
| Answer headline | "The output is suitable for business review." | States the result ("North has the highest revenue at 160,932.48 (48.0% of the total)…") | `test_v107_final.py::test_headline_states_what_the_result_shows` |

### Deterministic benchmarks (v1.0.5, recorded earlier)

`tests/test_v105_benchmark.py` checks the deterministic layers (grounding, planner, validators, verifier) without an LLM: 18 of 18 cases on a CSV hard fixture and 7 of 7 SQLite CTE/subquery/alias cases were recorded as passing, and the test passes in the suite run above. These are **not** end-to-end NL-to-SQL accuracy.

## 4. Security checks

| Check | Evidence | Status |
|---|---|---|
| Write, multi-statement, `ATTACH`, `PRAGMA` and `sqlite_master` queries rejected | `test_execution_guardrails.py`, `test_v105_sqlite_validator.py` | Pass in this review |
| Prompt injection from file contents cannot cause destructive SQL | `test_sprint3_prompt_injection.py` | Pass in this review |
| Another user's session, job, report or history returns 404 | `test_isolation.py`, `test_sprint2_jobs.py`, `test_sprint3_reports.py`, `test_four_format_e2e_guardrails.py` | Pass in this review |
| Production refuses unsafe configuration (CORS `*`, SQLite, no S3, `http://` base URL, short JWT secret) | `test_sprint5_docs.py`; also run by hand in this review | Pass |
| Logs contain no passwords, tokens or user content | `test_sprint4_logging.py`, `test_v106_gaps.py` | Pass in this review |
| XSS escaping of the real frontend | `test_xss_escaping.py` | **Skipped in this review** (needs Node.js) |
| Production container: 5 attack queries rejected with 422; second user 404; no token 401 | Previous evaluation report | Recorded earlier, on a stand-in base image; **not reproduced** |
| Release archive and git files contain no secrets, local databases, private data, IDE files, caches or macOS metadata | `scripts/package_release.py` re-reads the ZIP it built; `tests/test_release_packaging.py`; `--scan-git` on a fresh repository | Pass in this review (271 files in the ZIP, 270 tracked files; 7 local secret values checked, none found) |
| No credentials hardcoded in `docker-compose.yml` | `tests/test_release_packaging.py::test_compose_has_no_hardcoded_credentials` | Pass; the local stack still runs on the existing volume |

## 5. Load and failure testing (recorded, Sprint 4)

Recorded in Sprint 4 with `loadtests/` (raw data in `loadtests/results/*.json`) on a single-CPU sandbox with PostgreSQL 16, Redis, one RQ worker, two uvicorn processes, S3 via moto and a stub LLM. Not re-run in this review, and Sprint 5 added work to every authenticated request after these runs, so **they are not current capacity figures**.

| Finding | Recorded result |
|---|---|
| Thread/pool deadlock under burst load | 50/100/200 simultaneous users: 20/60/160 server errors before the fix. After admission control (`API_ADMISSION_LIMIT`), all 200 OK in the isolated burst; 13 deliberate `503 SERVER_BUSY` at 200 users in the mixed workload |
| First bottleneck after the fix | Background-job throughput with one worker (queue-wait p95 rose from 4 s to 60 s between 10 and 200 users); PostgreSQL stayed under 23 connections |
| Failure injection | Nine LLM failure cases; no canary secret leaked to responses, logs or ops views |
| Backup and restore | 14 MB database: dump under 1 s, verified restore 1.9 s, row counts matched, `alembic check` clean |

## 6. Known limitations

| Area | Limitation |
|---|---|
| AI accuracy | Live accuracy measured only on partial runs (about 60% exact on answered questions across formats, section 3.1; about 63% on the Stats NZ CSV after the section 3.3 fix); the 99% target is not met. The free Groq quota blocks a full run. The 62-case benchmark is one 5-table database with gold SQL written by the author, and the Low-confidence cutoff was tuned on it. Expected answers in the multi-format suite were written by the author from the raw files; no second reviewer checked them |
| Verification | Independent recalculation covers single-table plans only, so most multi-table answers (including "most orders per store region") are "not independently verified" |
| Tests not run here | PostgreSQL-only pytest tests and Node/browser tests. The Docker stack was exercised by hand (section 3.2), not by an automated test |
| Deployment | Not deployed in this review. `render.yaml` uses free plans for web, PostgreSQL (deleted after 30 days) and Key Value (not persistent) |
| Providers | Model IDs drift; defaults were checked against provider documentation, not a live call. The circuit breaker is per process |
| Security | Token in `localStorage`; account-existence disclosure on registration; no MFA; uploaded SQLite opened without `trusted_schema=OFF` |
| Billing | No real payment provider adapter; the Pro plan cannot be sold as shipped |
| Reliability | Load figures come from one CPU core; no soak test; real Sentry, alert webhook and S3 recovery never exercised. Large Excel/CSV ingestion on a 512 MB worker may exhaust memory near the 1,000,000-row cap |
| Product | No clarifying questions; per-question reports are not downloadable from the main UI |

## 7. Next steps

1. Re-run `python -m evaluation.multiformat --live --yes --delay 20` (and `--holdout`) on a provider tier whose daily quota covers about 250 calls; publish exact and semantic accuracy and cost per question.
2. Hierarchical statistics files (section 3.3): match variable names across languages ("ventes" -> "Sales"), model numeric periods such as `2026.03` ("March 2026 quarter"), and ask a clarifying question when a request could mean one level or all.
3. Run `python -m evaluation live --record-responses evaluation/results/run1_responses.json` on the 62-case benchmark.
4. Recalibrate the Low-confidence cutoff on those live results.
5. Run the PostgreSQL and Node/browser test jobs (CI).
   If uploads should survive container rebuilds locally, add a named volume for `/app/uploads`; this is a deliberate choice in `docker-compose.yml`, so it was not changed here.
6. Extend independent verification to two-table joins.
7. Move the token to an HttpOnly cookie with CSRF protection, or document the accepted risk.
8. Upgrade Render plans and connect a real payment adapter before accepting payments.
