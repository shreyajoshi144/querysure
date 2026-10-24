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
