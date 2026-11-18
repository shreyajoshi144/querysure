# QuerySure — Sprint History (Retrospective Reconstruction)

> **This is a retrospective reconstruction, not a set of historical Jira records.** QuerySure was not tracked in Jira, and the repository has no Git history. The seven sprints below were rebuilt from evidence inside the repository: "Sprint N" comments in code and tests, release-numbered test files (`test_v103_*` … `test_v107_*`), the Alembic migration chain and `CHANGELOG.md`. Story IDs such as `S3-02` are local labels for this document, **not Jira keys**. No story points, velocity, assignees or sprint dates are given, because none are recorded.

Related: [Product](01_PRODUCT.md) · [Architecture](02_ARCHITECTURE.md) · [Evaluation](03_EVALUATION.md)

## How to read this document

| Label | Meaning |
|---|---|
| **Verified** | Backed by code, a migration, a test or CI configuration in this repository |
| **Reconstructed** | An inference from verified evidence (sprint placement, goal wording, priority) that is not recorded anywhere |
| **Not done** | Known to be incomplete or never exercised |

**Evidenced dates only:** 2026-08-25 (initial schema migration), 2026-10-04 (Sprint 4 operations migration), 2026-10-23 (`CHANGELOG.md` v1.0.7 entry). Sprints 1–5 are named in the code; Sprints 6 and 7 group releases v1.0.3–v1.0.7 by theme and are reconstructed. Priorities (P0 = security, data loss or deploy blocker; P1 = core function; P2 = documentation and polish) were assigned retrospectively by that rule.

## Epics

| Epic | Scope | Sprints |
|---|---|---|
| **E1 NL-to-SQL pipeline** | Ingestion, profiling, retrieval, planner, generation, validator, sandbox, verifier, answer | Baseline, 6, 7 |
| **E2 Platform and deployment** | Configuration, migrations, Docker, Compose, Render, packaging | 1, 5, 6, 7 |
| **E3 Background work and concurrency** | Jobs, RQ worker, optimistic sessions, leases, admission control | 2, 3, 4 |
| **E4 AI control plane** | Structured output, prompt-injection defence, budgets, circuit breaker, provider fallback | 3, 4, 6, 7 |
| **E5 Observability and operations** | Logs, metrics, SLOs, alerts, health checks, backups, load tests | 4 |
| **E6 Accounts, plans and billing** | Authentication, revocable sessions, export/delete, entitlements, webhook billing | Baseline, 5 |
| **E7 Quality and evaluation** | Test suite, CI, benchmark harness, documentation | All |

## Order of work (verified from the migration chain)

Every Alembic migration names its parent, so this order is verified even where the sprint placement is not.

```mermaid
flowchart LR
    m1["7b6b5b36ed43<br/>initial schema<br/>2026-08-25"] --> m2["9f4c1a2e8b71<br/>email auth"] --> m3["a13f9c2d7e44<br/>Google identity"] --> m4["c4e8f2a1b7d0<br/>plans and usage"] --> m5["d6b7c8e9f012<br/>subscription state"] --> m6["f1a2b3c4d5e6<br/>token indexes"]
    m6 --> m7["a7b8c9d0e1f2<br/>jobs, session version<br/>Sprint 2"] --> m8["b1c2d3e4f5a6<br/>ai_invocations<br/>Sprint 3"] --> m9["c2d3e4f5a6b7<br/>drop duplicate uniques"] --> m10["d3e4f5a6b7c8<br/>dataset_bytes"] --> m11["e4f5a6b7c8d9<br/>reports"] --> m12["f5a6b7c8d9e0<br/>upload reservation"]
    m12 --> m13["5c1e0a4d9b21<br/>ops events<br/>Sprint 4, 2026-10-04"] --> m14["5a1e7c3b9d20<br/>billing, auth sessions<br/>Sprint 5"] --> m15["a9c1d2e3f4b5<br/>query history<br/>v1.0.5"]
```

The baseline before Sprint 1 (verified to exist; scope reconstructed) already had the core pipeline, e-mail and Google authentication, plans and usage tracking (migrations `7b6b5b36ed43` to `f1a2b3c4d5e6`).

---

## Sprint 1 — Production-safe baseline

**Sprint goal (reconstructed):** make the existing app impossible to deploy unsafely, and make the deployed image match what was tested. **Epics:** E2.

| ID | User story | Acceptance criteria | Evidence | Pri | Status |
|---|---|---|---|---|---|
| S1-01 | As an operator, I want all configuration read in one module so I can see every setting the app depends on | Only `config.py` reads `os.environ`; every variable is documented | `config.py` docstring; `test_sprint5_docs.py::test_every_environment_variable_is_documented` | P0 | Verified |
| S1-02 | As an operator, I want production to refuse unsafe settings so a misconfigured deploy fails loudly | Start-up fails with SQLite, no Redis, no `TRUSTED_PROXY_IPS`, or CORS `*` | `config.py` guards; `test_sprint5_docs.py::test_production_guards_refuse_unsafe_configuration` | P0 | Verified |
| S1-03 | As an operator, I want migrations applied on every deploy so production never runs a stale schema | `alembic upgrade head` runs before uvicorn; a failed migration fails the deploy | `render.yaml` `startCommand`; `Dockerfile` `CMD` | P0 | Verified |
| S1-04 | As a developer, I want `docker compose up` to run the built image, not the host source tree | No `.:/app` bind mount | `docker-compose.yml` comment | P1 | Verified |
| S1-05 | As an API client, I want malformed input to get a stable 422 error shape | Malformed JSON → 422 with a stable body | `test_sprint1_hardening.py::test_malformed_json_is_422_with_stable_error_shape` | P1 | Verified |
| S1-06 | As a user, I want storage, Redis-outage, XSS and row-limit defects fixed (`IMP-1` … `IMP-6`) | Each defect has a regression test | `test_storage.py`, `test_redis_resilience.py`, `test_xss_escaping.py`, `test_row_limit.py`, `test_sample_database_provisioning.py` | P0 | Verified; **sprint placement reconstructed** |

**Testing:** `test_sprint1_hardening.py` (security headers in production mode, API docs disabled in production, password change revokes the old token, report rate limit) plus the IMP regression files.
**Outcome:** a configuration layer that fails fast in production and a deploy path that always migrates first.
**Remaining work:** none recorded.

---

## Sprint 2 — Background jobs, concurrency and ingestion limits

**Sprint goal (reconstructed):** move slow and risky work off the request path and make concurrent requests safe. **Epics:** E3, E1.

| ID | User story | Acceptance criteria | Evidence | Pri | Status |
|---|---|---|---|---|---|
| S2-01 | As a user, I want uploads processed in the background so large files don't time out | Upload returns 202 with a job ID; the worker retries failures up to 3 times and stores a safe error | `jobs.py`, `worker.py`; `test_sprint2_jobs.py` | P1 | Verified |
| S2-02 | As a user, I want nobody else to see my jobs or sessions | Another user's job or session returns 404 | `test_sprint2_jobs.py::test_job_is_not_visible_to_another_user` | P0 | Verified |
| S2-03 | As an operator, I want hostile files rejected before they exhaust memory | Row, column, sheet and zip-bomb caps for CSV, JSON, XLSX and SQLite | `test_sprint2_ingestion_limits.py` | P0 | Verified |
| S2-04 | As a user, I want two tabs editing one session not to overwrite each other silently | Conflicting optimistic updates rejected; non-conflicting ones merged | Migration `a7b8c9d0e1f2`; `test_sprint2_concurrency.py` | P1 | Verified |
| S2-05 | As an operator, I want expensive routes limited and pool exhaustion to fail fast | Pool exhaustion → 503; expensive routes → 429 | `test_sprint2_concurrency.py` | P1 | Verified |

**Testing:** `test_sprint2_jobs.py`, `test_sprint2_ingestion_limits.py`, `test_sprint2_concurrency.py` (includes a PostgreSQL-only concurrent-writer test).
**Outcome:** uploads, reports and e-mail run on an RQ worker; sessions are owner-scoped and versioned.
**Remaining work:** under real burst load, pool exhaustion still stalled for the full 10 s pool timeout. Found and fixed in Sprint 4.

---

## Sprint 3 — AI control plane

**Sprint goal (reconstructed):** treat the LLM as an untrusted, metered dependency. **Epics:** E4, E3.

| ID | User story | Acceptance criteria | Evidence | Pri | Status |
|---|---|---|---|---|---|
| S3-01 | As the system, I want LLM output to be strict JSON with a fixed schema | Invalid JSON retried once, then a clean `GENERATION_FAILED`; unknown keys rejected | `test_sprint3_structured_generation.py` | P0 | Verified |
| S3-02 | As an operator, I want text inside uploaded files treated as data, never instructions | Hostile text appears only inside nonce-delimited blocks; an obeyed injection never executes | `test_sprint3_prompt_injection.py` | P0 | Verified |
| S3-03 | As an operator, I want AI budgets reserved before every provider call, plus a kill switch | Kill switch → 503 with no provider call; global cap atomic under concurrency; database fallback when Redis is down | `ai_guard.py`; `test_sprint3_ai_protection.py`, `test_final_budget.py` | P0 | Verified |
| S3-04 | As an operator, I want every AI call recorded with real token counts and cost, never the prompt | One `ai_invocations` row per call with no prompt or data | Migration `b1c2d3e4f5a6`; `test_sprint3_ai_invocations.py`, `test_sprint3_token_accounting.py` | P1 | Verified |
| S3-05 | As an operator, I want per-user session, dataset and storage quotas | Clear 403 over quota; concurrent uploads never exceed it | `quotas.py`; `test_sprint3_quotas.py`, `test_final_quota_atomicity.py` | P1 | Verified |
| S3-06 | As a user, I want reports stored durably and downloadable only by me | Other users get 404; tampered keys rejected | `report_storage.py`; `test_sprint3_reports.py` | P1 | Verified |

**Testing:** the files above plus `test_final_lease.py` (Redis leases released only by their owner). The "final hardening" of quotas, budgets and leases is placed here because its migration (`f5a6b7c8d9e0`) precedes Sprint 4's (reconstructed).
**Outcome:** AI spend is bounded and auditable; model output is parsed, never trusted.
**Remaining work:** the burst-load deadlock described under Sprint 2.

---

## Sprint 4 — Observability, reliability and operations

**Sprint goal:** make the system observable and prove its failure behaviour under load (wording from `tests/test_sprint4_*` and the operations guide). **Epics:** E5, E3, E4.

| ID | User story | Acceptance criteria | Evidence | Pri | Status |
|---|---|---|---|---|---|
| S4-01 | As an operator, I want PII-safe structured logs with request correlation | JSON lines; no passwords, tokens, prompts, SQL or rows; user IDs as keyed hashes | `observability/logging_setup.py`, `redaction.py`; `test_sprint4_logging.py` | P1 | Verified |
| S4-02 | As an operator, I want API, AI, queue, pool and Redis metrics | Per-minute aggregates shared through Redis | `observability/metrics.py`, `store.py`; `test_sprint4_metrics.py` | P1 | Verified |
| S4-03 | As an admin, I want an ops dashboard that shows aggregates only | Admin routes cannot reach customer content | `ops_api.py`; `test_sprint4_ops_privacy.py` | P1 | Verified |
| S4-04 | As a user, I want AI requests to survive provider failures | Timeouts, fallback, circuit breaker, reservation rollback, query-slot refund | `test_sprint4_reliability.py` | P0 | Verified |
| S4-05 | As an operator, I want readiness to separate critical from degraded dependencies | Database or Redis down → 503; worker, storage or AI problems → 200 `degraded` | `observability/health.py`; `test_sprint4_health.py` | P1 | Verified |
| S4-06 | As an operator, I want SLOs, alerts and incident tracking | Burn-rate alerts; one incident per alert key | Migration `5c1e0a4d9b21` (2026-10-04); `test_sprint4_alerts_slo.py` | P1 | Verified |
| S4-07 | As an operator, I want backup and restore verified, not just documented | Restore drill checks row counts and schema drift | `scripts/pg_backup.sh`, `pg_restore_verify.sh`, `s3_lifecycle.py`; `test_sprint4_backup_recovery.py` | P1 | Verified with moto (S3) only |
| S4-08 | As a user, I want bursts of traffic shed quickly instead of hanging | In-flight requests capped; fast `503 SERVER_BUSY` | `config.API_ADMISSION_LIMIT`; `test_sprint4_admission.py` | P0 | Verified |

**Testing:** eight `test_sprint4_*` files; `loadtests/` with raw results in `loadtests/results/*.json`.
**Outcome (recorded, not re-run):** the load test found a thread/pool deadlock (20/60/160 server errors at 50/100/200 simultaneous users); admission control removed it. See [Evaluation §5](03_EVALUATION.md#5-load-and-failure-testing-recorded-sprint-4).
**Remaining work:** a real Sentry project, real alert webhook and real S3 recovery were never exercised; capacity was measured on one CPU core only; no soak test longer than two minutes.

---

## Sprint 5 — Accounts, billing, packaging and documentation

**Sprint goal (reconstructed):** make accounts and money flows safe, make the container self-consistent, and make the documentation testable. **Epics:** E6, E2, E7.

| ID | User story | Acceptance criteria | Evidence | Pri | Status |
|---|---|---|---|---|---|
| S5-01 | As a user, I want sign-out to revoke my token, and "sign out everywhere" | Tokens bound to server sessions; password reset revokes all sessions | Migration `5a1e7c3b9d20`; `test_sprint5_auth_account.py` | P0 | Verified |
| S5-02 | As a user, I want to export my data and delete my account | Re-authentication required; deletion fails closed | `account_service.py`; `test_sprint5_auth_account.py` | P1 | Verified |
| S5-03 | As the business, I want checkout and webhooks that tolerate replay, reordering and bad signatures | Idempotent, order-safe, signature-verified processing | `billing_service.py`, `payment_provider.py`; `test_sprint5_billing.py` | P1 | Verified engine; **no real provider adapter** |
| S5-04 | As an operator, I want the image to contain every module the app imports | Packaging test fails if a module is missing | `Dockerfile` glob copy; `test_sprint5_packaging.py` | P0 | Verified |
| S5-05 | As a reviewer, I want documentation that fails CI if it drifts from the code | Every environment variable in `ENVIRONMENT.md`; every route in `API.md` | `test_sprint5_docs.py` | P2 | Verified |
| S5-06 | As a team, I want a real-browser end-to-end test of the main flow | Headless Chromium drives the real UI | `test_sprint5_e2e.py`, `e2e/e2e.mjs` | P1 | Verified (needs Node; skipped in this review) |

**Testing:** `test_sprint5_*.py`, `test_google_signin_integration.py`.
**Outcome:** revocable sessions, account export and deletion, an idempotent billing engine, a self-checking container and tested documentation.
**Remaining work:** no payment provider is connected (see [BILLING.md](BILLING.md)). The Sprint 5 closure test counts are unknown; that report was removed in v1.0.7.

---

## Sprint 6 (reconstructed) — Deploy blockers and answer reliability (v1.0.3–v1.0.5)

**Sprint goal (reconstructed):** unblock the Render/PostgreSQL deploy, survive provider outages, and stop plausible-but-wrong answers. **Epics:** E1, E2, E4.

| ID | User story | Acceptance criteria | Evidence | Pri | Status |
|---|---|---|---|---|---|
| S6-01 | As an operator, I want the Render blueprint to deploy without manual fixes | Worker and cron not on the free plan; Redis provisioned; no committed secrets; migrations first | `render.yaml`; `test_v103_render_blockers.py`, `test_render_config.py` | P0 | Verified |
| S6-02 | As a user, I want my question answered when one LLM provider is down | Cross-provider fallback with a shared attempt budget; sanitised diagnostics | `test_llm_fallback.py` | P0 | Verified |
| S6-03 | As a user, I want Excel, CSV and SQLite handled as reliably as JSON | Format-specific regression cases pass | `test_v104_format_reliability.py`, `test_four_format_e2e_guardrails.py` | P1 | Verified |
| S6-04 | As a user, I want filter values matched to the exact values in my data | Question words grounded to stored values; literals bound as parameters | `modules/value_grounding.py`, `sql_params.py`; `test_v105_csv_pipeline.py` | P1 | Verified |
| S6-05 | As a user, I want SQL checked against a plan and an independent recalculation | Planner, SQLite compile check, per-scope join lineage, verifier | `modules/query_planner.py`, `sqlite_compile_check.py`, `result_verifier.py`; `test_v105_sqlite_validator.py`, `test_v105_benchmark.py` | P1 | Verified |
| S6-06 | As a user, I want to search, reopen and rerun past questions (Reopen/Rerun removed after Sprint 7, see R-07) | Owner-scoped history; rerun re-validates | Migration `a9c1d2e3f4b5`; `history_service.py`; `test_v105_history.py` | P1 | Verified |

**Testing:** the files above; deterministic benchmarks recorded 18/18 CSV and 7/7 SQLite cases (not end-to-end LLM accuracy).
**Outcome:** a deployable blueprint, provider fallback, and a deterministic plan-and-verify layer around the LLM.
**Remaining work:** live-LLM accuracy unmeasured; joins not inferable from names; independent verification limited to single-table plans.

---

## Sprint 7 (reconstructed) — Release hardening and AI evaluation (v1.0.6–v1.0.7)

**Sprint goal (reconstructed):** fix what the evaluation exposed, clear known vulnerabilities, and document the system honestly. **Epics:** E7, E1, E4, E2.

| ID | User story | Acceptance criteria | Evidence | Pri | Status |
|---|---|---|---|---|---|
| S7-01 | As a user, I want the reliability score never to imply "correct" without independent verification | Score capped when unverified or mismatched | `test_v106_gaps.py::test_high_reliability_requires_independent_verification` | P1 | Verified |
| S7-02 | As a Free user, I want the daily limit enforced before any LLM spend | The 11th question is blocked before generation and the limit resets daily | `test_v106_gaps.py::test_exhausted_quota_blocks_generation_before_any_llm_spend` | P1 | Verified |
| S7-03 | As an evaluator, I want a benchmark that grades by result set and detects hallucinated identifiers | 62-case dataset validates; oracle, synthetic, replay and live modes | `evaluation/`; `validate-dataset` and `oracle` re-run in this review | P1 | Verified harness; **live run not done** |
| S7-04 | As a user, I want correct answers not flagged as mismatches or "Low confidence" | No false `mismatch` on the 44 gold queries; a real mismatch still caught | `test_v107_final.py::test_a_real_mismatch_is_still_caught` | P1 | Verified |
| S7-05 | As an operator, I want defaults that still work after providers retire models | Defaults are not retired model IDs | `test_v107_final.py::test_default_models_are_not_the_retired_ones` | P0 | Verified |
| S7-06 | As an operator, I want no known-vulnerable dependencies, enforced in CI | `pip-audit` clean; CI step | `.github/workflows/ci.yml`; `pip-audit` re-run in this review | P0 | Verified |
| S7-07 | As a new developer, I want `python start.py` to work on the first run | `.env.example` works for a first local run | `test_v107_final.py::test_env_example_works_for_a_first_local_run` | P1 | Verified |
| S7-08 | As a reviewer, I want documentation that explains the design and states its limits | README and product, architecture and evaluation documents | `README.md`, `docs/` | P2 | Verified |

**Testing:** `test_v106_gaps.py`, `test_v107_final.py`, `test_evaluation_product_ui.py`; evaluation harness runs.
**Outcome:** v1.0.7 (`CHANGELOG.md`, 2026-10-23).
**Remaining work:** see below.

---

## Release finalisation (after Sprint 7)

Defects found during the final review and fixed before packaging. These are not part of a sprint.

| ID | Item | Acceptance criteria | Evidence | Status |
|---|---|---|---|---|
| R-01 | Compose worker healthcheck used `curl` on an HTTP port the worker does not serve | Healthcheck uses no curl and no HTTP; healthy only while this container's worker heartbeat exists in Redis | `docker-compose.yml`; `test_v103_render_blockers.py` (two tests) | Fixed; container start not verified |
| R-02 | Compose defaulted `ANTHROPIC_FALLBACK_MODEL` to the retired `claude-3-5-haiku-20241022` | Default matches `config.py` (`claude-haiku-4-5-20251001`) | `docker-compose.yml` | Fixed |
| R-03 | README linked to documents and images that did not exist | Every local link resolves | README link check | Fixed |
| R-04 | Overlapping documentation | Five core documents plus the reference docs that code and tests depend on | `docs/` | Done |
| R-05 | No evaluation on the owner's real files (CSV, Excel, JSON, SQLite) or in other languages | 180 tuned and 32 held-out runs in 8 language/style variants, answers computed independently with pandas; negative cases | `evaluation/multiformat/`; `test_v108_multiformat_eval.py` | Done |
| R-06 | Correct SQL rejected or flagged, and the wrong tables shown to the model (Northwind retrieval recall 8.8%) | On supplied correct SQL: 100% accepted and clean, retrieval recall 100%; held-out set passes; detectable wrong SQL still flagged (20/20) | `test_v108_accuracy_regressions.py` (40 tests, 30 fail on the original code) | Fixed offline; live accuracy about 60% on answered questions, target of 99% not met |
| R-07 | History offered Reopen/Rerun, and switching datasets kept the old answer on screen | History is read-only snapshots ("Result not saved" when none), with confirmed delete; a new dataset clears the workspace; a failed upload leaves the current dataset active | `static/js/app.js`; `test_v108_accuracy_regressions.py` (UI checks) | Fixed; checked in the browser on the Compose stack (R-08) |
| R-08 | End-to-end check on the running Compose stack | Health, persistence across rebuild and database restart, all five formats, dataset switch, failed upload, History, isolation; temporary accounts removed | [03_EVALUATION.md](03_EVALUATION.md) section 3.2; `test_v108_accuracy_regressions.py` | Done; found and fixed a verifier false *Mismatch*, wrong share-of-total findings, a hidden header tab bar and a generic upload error |
| R-09 | Stats NZ hierarchical CSV: levels, versions and variables added together | Structure detected from the data; filters fixed from the question; mixing flagged or repaired deterministically; no false alarms on gold SQL | `modules/long_format.py`; `test_v109_long_format.py`; [03_EVALUATION.md](03_EVALUATION.md) section 3.3 | Fixed offline; live about 63% exact on answered CSV questions, 99% not met |
| R-10 | Release ZIP built by hand carried secrets and private data; credentials in `docker-compose.yml` | Packaging script with its own exclusions and a post-build scan; Compose reads credentials from `.env` | `scripts/package_release.py`; `test_release_packaging.py` | Done |

**Verification in this review:** 1,171 passed, 0 failed, 14 skipped (Python 3.12, SQLite); Ruff clean; `pip-audit` clean; `docker compose config` valid. PostgreSQL-only pytest tests were skipped; the running Compose stack was checked by hand (R-08). Details: [03_EVALUATION.md](03_EVALUATION.md).

## AI Operations Platform — Sprint 0 (baseline) and Sprint 1 (workspaces)

The first two sprints of the evolution towards an AI operations platform. Sprint 0 measures the starting point; Sprint 1
adds the tenancy layer every later capability depends on.

| ID | Item | Acceptance criteria | Evidence | Status |
|---|---|---|---|---|
| S0-01 | Baseline | Tag `v1-querysure`; tests and offline evaluations recorded on a clean checkout of the tag | [baseline/README.md](baseline/README.md) | Done |
| S0-02 | Capability map | Every item of both target scopes classified working / partial / missing / broken, citing the code | [CAPABILITY_MAP.md](CAPABILITY_MAP.md) | Done |
| S0-03 | Cross-user isolation regression | Another user cannot reach a user's sessions, dataset preview, retrieval, execution, history, jobs, reports or answer trace | `test_s1_workspaces.py::test_cross_user_isolation_of_sessions_history_reports_jobs_and_retrieval` | Done |
| S1-01 | Workspaces and roles | Personal workspace per user; team workspaces with owner / admin / analyst / viewer; one authorization check; 404 for non-members, 403 for low roles | `workspaces.py`, `workspace_api.py`; role-matrix tests | Done |
| S1-02 | Dataset catalog | Uploads catalogued in the chosen workspace (analyst+); entry removed with its file; delete by creator or admin, storage first; datasets follow their creator when membership ends | `datasets` table; `test_s1_workspaces.py` | Done. Querying another member's file is **not** included: storage stays owner-scoped |
| S1-03 | Audit trail | Append-only; login, upload, query, report, dataset delete, workspace and member changes; no SQL, questions, data, e-mail or storage refs; failure never fails the request | `audit.py`; audit tests | Done |
| S1-04 | Migration | Backfill of personal workspaces and catalog; `alembic check` clean; reversible; on SQLite and PostgreSQL 16 | `b8d2e4f6a1c3`; `test_s1_migration.py`; manual run on PostgreSQL 16 and from the Docker image | Done |
| S1-05 | UI | Workspace view: switcher, catalog, members, audit trail with paging, create / rename / delete / leave; hostile names render as text | `static/js/app.js`; `test_s1_workspace_ui.py`; checked in the browser on the running app | Done |
| S1-06 | Account privacy | Export lists memberships and catalogued datasets; deletion removes owned team workspaces and re-homes other members' datasets | `account_service.py`; tests | Done |

**Verification (Sprint 1):** Ruff clean. SQLite: 1,266 passed, 8 skipped, 2 failed — the 2 are the browser e2e test, whose bundled Linux Chromium cannot start on macOS (`spawn ENOEXEC`); the same test passes (both asset variants) when pointed at a local Chrome. PostgreSQL 16: 1,262 passed, 13 skipped, 0 failed (e2e deselected). Migration upgrade, `alembic check`, downgrade and re-upgrade verified on SQLite, PostgreSQL and from the Docker image. Docker smoke test: healthy, UID 10001. Live app: 24 API checks across a server restart, plus the Workspace UI exercised in the browser as owner and viewer.

## AI Operations Platform — Sprint 2 (AI provider layer)

| ID | Item | Acceptance criteria | Evidence | Status |
|---|---|---|---|---|
| S2-01 | Extract the provider layer | Transports, fallback chain, usage accounting in `ai/`; no behaviour change | 375 existing provider/fallback/injection/accounting/evaluation tests pass unmodified; offline multi-format (tuned, held-out, negatives) output identical to the Sprint 0 baseline; oracle benchmark identical except timing | Done |
| S2-02 | Per-task routing | `sql_generation` and `sql_repair` routable via `AI_TASK_ROUTES`; bad config stops startup; repairs run as their own task | `ai/router.py`; `test_s2_ai_layer.py`; live app run with `sql_generation=openai` made no Groq call | Done |
| S2-03 | Gemini and xAI | Documented OpenAI-compatible endpoints, strict JSON Schema, no default models, capabilities listed per provider | `ai/providers.py`; stubbed-client tests | Done in code; **not run against the live Gemini or xAI APIs** (no keys) |
| S2-04 | AI call trace | One `ai_calls` row per attempt incl. failures, retries and skips; trace id on the response and the invocation summary; owner-only read; no prompt/SQL/data/keys; write failure never breaks a request; export and deletion | `ai/trace.py`, migration `c3f5a7b9d1e2`, `GET /api/ai/traces/{id}`; live app run through real SDKs against a local mock (4 Groq 500s with retries, then OpenAI) | Done |
| S2-05 | Side-by-side model evaluation | Same cases, pipeline and scoring per `provider[:model]`; accuracy over answered runs, p50 latency, tokens, cost | `evaluation.multiformat --compare` | Done in code; **no live comparison numbers yet** (needs provider credits) |
| S2-06 | UI | Generate SQL inspector lists every model call behind an answer | `static/js/app.js`; checked in the browser | Done |

## AI Operations Platform — Sprint 3 (connectors, layered ingestion, workspace-owned datasets)

| ID | Item | Acceptance criteria | Evidence | Status |
|---|---|---|---|---|
| S3-01 | Connector framework + file connectors | Interface, registry, CSV / Excel / JSON / Parquet / SQLite; UI lists only the registry | `connectors/`; `test_s3_ingestion.py` | Done (file sources, full refresh only) |
| S3-02 | Layered runs | Raw, validated, curated; run counts; one active run per dataset (database-enforced); atomic swap; identical file skipped; failed or interrupted run keeps the previous version | `ingest.py`, migration `d4a6b8c0e2f4` | Done |
| S3-03 | Validation, quarantine, lineage, schema changes | Reason codes, hidden `_qs_*` tables blocked at schema, validator and authorizer, schema diff audited | tests | Done |
| S3-04 | Workspace-owned datasets | Viewer opens and queries, analyst re-imports, revocation immediate, stale sessions reopen, quota once per dataset | `workspaces.session_db_path`, `dataset_api.py` | Done. Behaviour change: dataset lifetime is independent of sessions |
| S3-05 | UI | Open, Re-import, Runs, quarantine sample | `static/js/app.js` | Done |
| S3-06 | Encrypted connection credentials | | | **Not in this sprint** (file connectors need none); Sprint 4 |

## AI Operations Platform — Sprint 4 (database connectors, incremental sync)

| ID | Item | Acceptance criteria | Evidence | Status |
|---|---|---|---|---|
| S4-01 | Encrypted connections | Create, test, edit, disable, delete; password encrypted, bound to its connection, never in responses, logs or audit; redirect of a stored secret blocked; workspace roles | `secrets_store.py`, `connection_api.py`; `test_s4_connections.py` | Done |
| S4-02 | PostgreSQL and MySQL connectors | Schema discovery, read-only keyset batches, error codes, network guard | `connectors/`; tests against real PostgreSQL 16 and MySQL 8.4 | Done |
| S4-03 | Batch import and backfill | Bounded memory, progress, resume after a transient failure, time limit, unstorable values quarantined | `ingest_db.py` | Done |
| S4-04 | Incremental sync | Watermark + overlap, primary-key upserts, idempotent re-run, checkpoint with the swap, fallback with a recorded reason | tests | Done. Deletions not detected; no scheduler yet |
| S4-05 | Reliability | One active run per dataset, lost job → interrupted, failed run keeps the previous version and checkpoint | tests | Done |
| S4-06 | UI | Connections, import, sync | `static/js/app.js` | Done |

## AI Operations Platform — Sprint 5 (REST, S3, scheduler)

| ID | Item | Acceptance criteria | Evidence | Status |
|---|---|---|---|---|
| S5-01 | REST connector | Config validation, five pagination styles, auth methods, retries with `Retry-After`, redirects and oversize or non-JSON answers refused, validated-address connection, incremental by `updated_since` | `connectors/rest.py`, `httpclient.py`; `test_s5_sources.py` against a real local HTTP server | Done. No OAuth |
| S5-02 | S3 connector | Explicit credentials only, endpoint guard, caps, new / changed / removed object detection, unchanged run skipped, schema-change fallback | `connectors/s3_source.py`; moto tests; live run against a local S3-compatible server | Done |
| S5-03 | Shared pipeline | One swap / checkpoint / quarantine path for database, REST and S3 sources | `ingest_db.publish`, `ingest_records.py` | Done |
| S5-04 | Scheduler | Atomic claim, no doubled run, one catch-up run after downtime, self-disable with reason, heartbeat, compose service | `scheduler.py`; tests incl. a two-thread race | Done. Needs an always-on process |
| S5-05 | Cleanup | Workspace deletion removes connections and secrets; dataset deletion removes checkpoint and schedule | tests | Done |
| S5-06 | UI | REST / S3 connection forms, resources import, schedule control | `static/js/app.js` | Done |

## AI Operations Platform — Sprint 6 (data quality)

| ID | Item | Acceptance criteria | Evidence | Status |
|---|---|---|---|---|
| S6-01 | Profiling | Per-table and per-column counts after every successful import; no text values stored | `quality.py`; `tests/test_s6_quality.py` | Done |
| S6-02 | Findings | Drop, surge, outlier, empty column, null spike, type / schema change, numeric shift, reject and duplicate rates, configurable | tests on numbers and through the API | Done |
| S6-03 | Safety | Report failure never fails an import; viewers read, non-members get 404; removed with the dataset | tests | Done |
| S6-04 | UI | Status on the dataset row; findings in the Runs panel | `static/js/app.js` | Done |

## AI Operations Platform — Sprint 7 (knowledge hub)

| ID | Item | Acceptance criteria | Evidence | Status |
|---|---|---|---|---|
| S7-01 | Ingestion | txt / md / pdf / docx, caps, duplicate refusal, text-only storage | `knowledge.py`; tests | Done |
| S7-02 | Retrieval | Reuses `rank_texts`; recall measured; nothing returned when nothing is relevant | `test_retrieval_recall_on_a_fixed_question_set` | Done (small fixture) |
| S7-03 | Cited answers | Untrusted-passage prompt, citation validation, withheld ungrounded answers, shared budget and trace | `knowledge_api.py`; tests | Done |
| S7-04 | Isolation and deletion | Roles, 404 for non-members, delete removes passages, workspace / account cleanup | tests | Done |
| S7-05 | UI | Knowledge card: upload, list, delete, ask, search | `static/js/app.js` | Done |

## Open backlog

| ID | Item | Type |
|---|---|---|
| B-01 | Run the live LLM benchmark and publish accuracy, hallucination rate and cost | Not done |
| B-02 | Run the PostgreSQL and Node/browser suites and start the Compose stack; confirm the worker reports healthy | Not done |
| B-03 | Connect a real payment provider adapter and test it against the provider's sandbox | Not done |
| B-04 | Upgrade Render free plans (PostgreSQL expiry, non-persistent Key Value) before real users | Not done |
| B-05 | Exercise real Sentry, alert webhook and S3 recovery; measure capacity on multi-core hardware | Not done |
| B-06 | Clarifying questions for ambiguous asks; two-table independent verification | Not done |
| B-07 | Token storage hardening (HttpOnly cookie with CSRF protection) and `trusted_schema=OFF` for uploaded SQLite | Not done |

## AI Operations Platform — Sprint 8 (assistant conversations)

| ID | Item | Acceptance criteria | Evidence | Status |
|---|---|---|---|---|
| S8-01 | Persisted private chats | Create / list / read / rename / delete; migration `c9e1a3b5d7f2` | `conversation_api.py`; `tests/test_s8_assistant.py` | Done |
| S8-02 | Follow-up context | Last turns in a `<conversation>` block; short follow-ups retrieved with the previous question | `assistant.py`; tests | Done |
| S8-03 | Same pipeline, one model call path | `knowledge_api.generate_answer` shared with Sprint 7; budget, keys, provider chain, trace unchanged | tests | Done |
| S8-04 | Honest limits | No passage: no model call; data questions redirected to Ask-data; uncited answers withheld | tests | Done |
| S8-05 | Privacy and cleanup | Owner-only (404 otherwise), membership re-checked, workspace / account deletion removes chats | tests | Done |
| S8-06 | UI | Assistant card: history, new chat, send, cited sources, delete | `static/js/app.js` | Done |

## AI Operations Platform — Sprint 9 (controlled agent)

| ID | Item | Acceptance criteria | Evidence | Status |
|---|---|---|---|---|
| S9-01 | Tool registry and plan validation | Six typed tools; unknown tools, bad arguments, foreign ids, read-after-write and over-long plans rejected | `agent.py`; `test_invalid_plans_are_rejected_not_repaired` | Done |
| S9-02 | Approval gates | Write steps wait; approval runs the stored arguments once; reject skips the rest; approvals expire | tests | Done |
| S9-03 | Injection resistance | One planning call; tool output never returned to the model; names fenced | `test_injection_in_documents_and_names_cannot_add_steps` | Done |
| S9-04 | Roles and privacy | analyst+ starts; tool role approves; runs visible to creator and admin+ only | tests | Done |
| S9-05 | Persistence, audit, cleanup | Runs/steps stored (migration `d1f3b5a7c9e4`), writes audited, workspace/user deletion removes them | tests | Done |
| S9-06 | UI | Agent card: goal, runs, step results, approve / reject / cancel | `static/js/app.js` | Done |

## AI Operations Platform — Sprint 10 (investigations and structured reports)

| ID | Item | Acceptance criteria | Evidence | Status |
|---|---|---|---|---|
| S10-01 | Computed facts with SQL | Trend, comparison, breakdown; each fact keeps its SQL; deterministic | `investigation.py`; `test_trend_separates_facts_from_hypotheses`, `test_results_are_deterministic_for_the_same_inputs` | Done |
| S10-02 | Facts vs hypotheses vs recommendations vs limitations | Separate sections; hypotheses cite fact ids and are labelled unverified | tests | Done |
| S10-03 | Safe parameters | Roles checked against the schema; injection-like identifiers refused; nothing stored on refusal | `test_bad_parameters_are_refused_with_a_code` | Done |
| S10-04 | Untrusted documents | Excerpts shown as context only, escaped in HTML, never change a figure | `test_documents_are_untrusted_context_and_never_change_the_figures` | Done |
| S10-05 | Exports and access | JSON/HTML via existing report storage; creator-only download; viewer read, analyst run, admin/creator delete; cleanup with dataset | tests | Done |
| S10-06 | UI | Investigations card: run, list, read, download, delete | `static/js/app.js` | Done |

## AI Operations Platform — Sprint 11 (evaluation and governance)

| ID | Item | Acceptance criteria | Evidence | Status |
|---|---|---|---|---|
| S11-01 | Retrieval evaluation and vector-index decision | Fixed corpus, class-level recall/MRR/false positives, thresholds fixed beforehand, intervals reported | `evaluation/platform/rag.py`; `docs/baseline/platform_eval.txt` | Done (100 questions; verdict: not justified, paraphrase recall@3 0.72) |
| S11-02 | Plan / tool-selection evaluation | Oracle, replay and live modes; overreach and id accuracy scored; live refuses without `--yes` | `agent_eval.py`; tests | Done (live not run: no funded key) |
| S11-03 | Safety-adversarial suite | 36 cases against real controls; a broken control makes cases fail | `safety.py`; `test_the_safety_suite_detects_a_broken_control` | Done |
| S11-04 | AI usage governance report | Per task / provider / day from `ai_calls`, admin-only, workspace-scoped, no prompt text | `governance.py`; `test_ai_usage_report_aggregates_real_calls_and_is_admin_only` | Done |
| S11-05 | UI | Admin-only AI usage card | `static/js/app.js` | Done |

## AI Operations Platform — Sprint 12 (dashboard, hardening and launch)

| ID | Item | Acceptance criteria | Evidence | Status |
|---|---|---|---|---|
| S12-01 | Workspace overview | Real counts, role-aware, scoped to the workspace; UI card | `overview.py`; `test_overview_summarises_the_workspace_and_respects_roles` | Done |
| S12-02 | Authentication matrix | Every `/api` route rejects anonymous calls unless on a reviewed list | `test_every_non_public_api_route_rejects_anonymous_requests` | Done |
| S12-03 | Measured latency and limits | Heavy paths timed; unsafe defaults replaced by enforced limits | `docs/baseline/performance.txt`; `test_index_and_table_size_limits_protect_latency` | Done |
| S12-04 | Launch checklist and CI gate | Documented gates and smoke test; safety suite in CI | `docs/LAUNCH_CHECKLIST.md`; `.github/workflows/ci.yml` | Done (checklist not yet executed on a real deployment) |

## AI Operations Platform — Sprint 13 (retrieval scale, live-quality tooling, deployment rehearsal)

| ID | Item | Acceptance criteria | Evidence | Status |
|---|---|---|---|---|
| S13-01 | Cached inverted index | Same scores as scoring every passage; follows uploads and deletes; never crosses workspaces; bounded memory | `tests/test_s13_retrieval_index.py`; `docs/baseline/performance.txt` | Done |
| S13-02 | Raised, measured cap | Cap raised only to what measurements support; memory stated; small plans lowered | 25,000 default; `render.yaml` 5,000 | Done |
| S13-03 | Answer-quality suite | Retrieval -> real prompt -> real validator; oracle / replay / live; off-topic hallucination counted | `evaluation/platform/answers.py`; `test_answer_suite_scores_replayed_replies_honestly` | Done (live run not executed) |
| S13-04 | Live quality run | One command, explicit confirmation, Groq only, replies recorded | `scripts/live_quality.sh` | Tooling done; **no live numbers** (no funded key available here) |
| S13-05 | Deployment rehearsal | Production-mode stack, smoke test, backup, verified restore, restart; exits non-zero on failure | `scripts/launch_rehearsal.sh`, `scripts/launch_smoke.py` | Done on one machine; not a cloud deployment |
| S13-06 | Documentation for outside readers | Reviewer guide, measured results, limits | `README.md`, `docs/LAUNCH_CHECKLIST.md` | Done |
