# Product requirements document (PRD)

| | |
|---|---|
| **Product** | QuerySure |
| **Version covered** | v1.0.7 (Text-to-SQL MVP, 2026-10-23) and the AI Operations Platform (merged 2026-11-18, PR #2) |
| **Owner** | Shreya Joshi |
| **Status** | Requirements delivered as listed below. The open items are in the [product backlog](04_PRODUCT_BACKLOG.md) |

## 1. Problem

Small teams keep their numbers in CSV and Excel exports or in databases they can't query without SQL. "Chat with your data" tools remove the wait but fail **invisibly**:

- A filter value is spelled differently from the data (`'Completed'` vs `'completed'`), so the result is empty and reads as "no sales".
- A date filter is silently dropped, so the total is real but for the wrong period.
- A rate is averaged instead of recalculated, so the figure is slightly off and nobody notices.

**Problem statement:** people who don't write SQL need answers they can *check*, not answers they have to *trust*.

## 2. Users

| Persona | Situation | Need | Primary? |
|---|---|---|---|
| Operations / business analyst | Small company, data in exports, no BI team | A table or chart in seconds, from their own data | Yes |
| Founder / manager | Must decide if a figure can go in a report | One-line answer plus "verified / not verified" | |
| Reviewer | Signs off an AI-produced number | The exact SQL, every check and a saved snapshot | |
| Workspace admin | Shares data with a team | Roles, approvals, audit trail, AI cost visibility | |

## 3. Goals and non-goals

**Goals**
1. The model has one job per feature (writing SQL, writing a cited answer, proposing a plan). Everything around it is deterministic and tested.
2. Every answer shows its evidence, and is called "verified" only after an independent recalculation agrees.
3. Nothing the AI does can change or leak data without a person's approval.
4. Quality is measured with a method that can fail, and results are published with their limits.

**Non-goals:** replacing a BI tool, writing back to source data, autonomous agents, training or fine-tuning models.

## 4. User journey

Overview → **Connect** (upload or connect a source) → **Explore** (schema, preview, quality) → **Investigate** (agent with approvals) → **Analyze** (trend, comparison, breakdown) → **Query** (plain-English question → checked answer) → **Knowledge** (documents → cited answers).

## 5. Functional requirements

Priority: **P0** must have for release, **P1** should have, **P2** could have. Status: **Done** means implemented, tested and in `main`.

| ID | Requirement | Pri | Status | Evidence |
|---|---|---|---|---|
| FR-01 | Register, verify e-mail, sign in, sign out everywhere, reset password, export and delete account | P0 | Done | `auth.py`, `account_service.py`; `tests/test_auth*.py`, `test_sprint5_auth_account.py` |
| FR-02 | Upload CSV, JSON, Excel, SQLite, Parquet, with size, row and zip-bomb limits | P0 | Done | `modules/ingestion.py`, `connectors/files.py`; ingestion tests |
| FR-03 | Ask a question in plain English and get an answer sentence, table or chart, SQL, validation findings and trace | P0 | Done | 8-stage pipeline in `modules/`; `tests/test_v105_benchmark.py` |
| FR-04 | SQL must be a single read-only `SELECT`/`WITH` over real tables and columns | P0 | Done | `sql_validator.py` (sqlglot syntax tree), SQLite authorizer; `test_execution_guardrails.py` |
| FR-05 | Recalculate single-table results independently; label everything else "not independently verified" | P0 | Done (single-table only) | `result_verifier.py`; `test_v107_final.py` |
| FR-06 | Save every answer as a read-only snapshot in History; search and delete | P1 | Done | `query_history`; `test_v108_accuracy_regressions.py` |
| FR-07 | Team workspaces with roles owner > admin > analyst > viewer; non-members get 404 | P0 | Done | `workspaces.require_role`; `test_s1_workspaces.py` |
| FR-08 | Append-only audit trail with no user data in it | P0 | Done | `audit.py`; audit tests |
| FR-09 | Connect PostgreSQL, MySQL, REST APIs and S3 with encrypted credentials; batch and incremental sync; schedules | P1 | Done | `connectors/`, `secrets_store.py`, `scheduler.py`; `test_s4_connections.py`, `test_s5_sources.py` |
| FR-10 | Versioned datasets: a failed import keeps the previous version | P0 | Done | `ingest.py`; `test_s3_ingestion.py` |
| FR-11 | Data-quality report after every import (drops, surges, null spikes, schema changes) | P1 | Done | `quality.py`; `test_s6_quality.py` |
| FR-12 | Upload documents (txt, md, pdf, docx) and get answers that cite passages; withhold answers without a valid citation | P0 | Done | `knowledge.py`, `knowledge_api.py`; Sprint 7 tests |
| FR-13 | Private assistant conversations with follow-up context | P1 | Done | `assistant.py`; `test_s8_assistant.py` |
| FR-14 | Agent: one validated plan over 6 tools; write steps wait for approval by an authorised person; approvals expire after 24 h | P0 | Done | `agent.py`; `test_invalid_plans_are_rejected_not_repaired`, `test_injection_in_documents_and_names_cannot_add_steps` |
| FR-15 | Investigations: trend, comparison, breakdown; facts keep their SQL; hypotheses labelled unverified; JSON/HTML export | P1 | Done | `investigation.py`; Sprint 10 tests |
| FR-16 | AI provider fallback (Groq → OpenAI → Anthropic), per-task routing, call trace | P0 | Done | `ai/`; `test_s2_ai_layer.py`, `test_llm_fallback.py` |
| FR-17 | AI budgets per user and global, reserved before each call; kill switch | P0 | Done | `DAILY_LLM_BUDGET_USD` (default $2), `GLOBAL_DAILY_AI_BUDGET_USD` (default $25), `AI_KILL_SWITCH`; `test_sprint3_token_accounting.py` |
| FR-18 | Admin views: AI usage per task/provider/day, workspace overview of what needs attention | P1 | Done | `governance.py`, `overview.py`; Sprint 11–12 tests |
| FR-19 | Plans and quotas (Free: 10 questions/day) | P1 | Done | `quotas.py`; quota tests |
| FR-20 | Ask a clarifying question when a request is ambiguous | P2 | **Not done** | Backlog B-06 |
| FR-21 | Take payments through a real provider | P2 | **Not done** | Backlog B-03 (webhook engine exists, no live adapter) |

## 6. Non-functional requirements

| ID | Area | Requirement | Measured result |
|---|---|---|---|
| NFR-01 | Security | SQL can never write; four independent layers (syntax-tree allow-list, read-only connection, SQLite authorizer, time/row/size caps) | 36/36 safety cases blocked |
| NFR-02 | Security | Every `/api` route rejects anonymous calls unless on a reviewed public list | Enforced by `test_every_non_public_api_route_rejects_anonymous_requests` |
| NFR-03 | Privacy | Logs, audit and traces hold no passwords, tokens, prompts or data rows | `test_sprint4_logging.py`, `test_v106_gaps.py` |
| NFR-04 | Reliability | Production refuses to start with unsafe settings (SQLite, no Redis, CORS `*`, no S3, short JWT secret) | `test_sprint5_docs.py` |
| NFR-05 | Reliability | A provider outage falls back to the next model; a timeout on every call | Fallback and circuit-breaker tests |
| NFR-06 | Performance | Knowledge search stays fast as documents grow | Under 2 ms per question at 25,000 passages (was 4.8 s at 30,000) |
| NFR-07 | Cost | AI spend is capped | $2 per user per day, $25 global per day, reserved before each call |
| NFR-08 | Operability | Migrations run before the app takes traffic; backup and restore rehearsed | Rehearsal passed 2026-11-07 |
| NFR-09 | Maintainability | CI on every push: lint, tests on SQLite and PostgreSQL, migrations, dependency audit, Docker build | 6 CI jobs in `.github/workflows/ci.yml` |

## 7. Out of scope

| Item | Why | Trade-off accepted |
|---|---|---|
| Vector database / embeddings | A pass/fail rule was fixed before measuring; keyword retrieval met it | Reworded questions: recall@3 0.72 (about 28% miss) |
| Autonomous agent loop | Feeding tool output back to the model lets a document steer actions | Less flexible agent |
| Writing back to data | A validator bug must never become data loss | No "fix my data" features |
| LLM-written answer sentence | Rules over result rows can't invent a number | Less fluent wording |
| MFA, live payments, clarifying questions | Not needed to prove the core promise | Listed in the backlog |

## 8. Success metrics

No real users yet, so these are targets with the instrument that would measure each.

| Goal | Metric | Target | Current |
|---|---|---|---|
| Answers are right | Exact-match accuracy with a live model | ≥ 70% to release; 99% long-term goal | 57.5% (tuned) and 60.9% (held-out) exact on answered questions across 5 file formats; about 63% on the Stats NZ CSV after the hierarchy fix. Partial runs (free quota) |
| Answers are safe | Writes or validator escapes reaching data | 0 | 0 (36/36 blocked) |
| Users come back | 7-day return rate; answers exported or reopened in History | To be measured | Instrumented (`query_history`, `usage_daily`); no users yet |
| Cost stays bounded | AI cost per answered question | < $0.01 | Live run 1: $0.05 for 180 tuned runs |

## 9. Release criteria

All P0 requirements done; CI green; [launch checklist](../LAUNCH_CHECKLIST.md) gates pass; known limits published in the README.
