# Capability map (Sprint 0 baseline, updated through Sprint 13)

Every item in the two target scopes (Enterprise AI Operations, Connectors & Ingestion), classified against the code at
tag `v1-querysure` (commit `a47f924`). Status was decided by reading the named files, not from documentation.

- **Working**: implemented and covered by tests.
- **Partial**: some of the requirement exists; the gap is stated.
- **Missing**: no implementation.
- **Broken**: implemented but failing. No item was found broken.

Sprint 1 changes are marked **(S1)**, Sprint 2 changes **(S2)**, Sprint 3 changes **(S3)**, Sprint 4 changes **(S4)**, Sprint 5 changes **(S5)**, and so on up to **(S13)**. Sprint numbers here are AI Operations Platform sprints.

## Foundation: tenancy, authorization, audit

| Capability | Status | Evidence / gap |
|---|---|---|
| Authentication (password, Google, revocable sessions) | Working | `auth.py`, `api.py` auth routes; `tests/test_auth*.py`, `test_sprint5_auth_account.py` |
| Per-user data isolation | Working | Every data route filters by `PipelineSession.user_id`; storage keys re-validated per user (`storage._parse_remote`); `tests/test_isolation.py`, **(S1)** `tests/test_s1_workspaces.py::test_cross_user_isolation_of_sessions_history_reports_jobs_and_retrieval` |
| Workspaces | **Working (S1)** | `workspaces.py`, `workspace_api.py`; a personal workspace for every user plus team workspaces |
| RBAC (owner / admin / analyst / viewer) | **Working (S1)** | `workspaces.require_role` is the single check; role-matrix tests in `test_s1_workspaces.py` |
| Dataset as a first-class entity | **Working (S3)** | Workspace-owned, versioned datasets (`datasets`, `ingestion_runs`); lifetime independent of sessions; viewer+ opens and queries, analyst+ re-imports; membership re-checked on every request. Pre-Sprint-3 uploads keep the legacy per-session behaviour |
| Audit trail | **Working (S1)** | `audit.py` (append-only, allow-listed details), recorded for login, upload, query, report, dataset delete, workspace and member changes |
| Platform ops admin | Working | `ops_api.py`: aggregate metrics only, gated by `ADMIN_EMAILS` |

## 1. Enterprise AI assistant

| Capability | Status | Evidence / gap |
|---|---|---|
| Natural-language understanding for data questions | Working | `modules/query_planner.py`, `modules/sql_generator.py` |
| Intent classification (data / knowledge / investigation / report / action) | Partial **(S8)** | The assistant checks deterministically whether a question is about documents or data and redirects data questions to Query; no model-based intent routing |
| Structured request interpretation | Partial | Deterministic query plan (measure, grouping, filters, dates, limit) in `query_planner.py`; data questions only |
| Context-aware follow-ups, conversation history | Partial **(S8)** | Knowledge assistant: private conversations with follow-up context (`assistant.py`). Query (data questions) still answers one question at a time; `query_history` is a log, not context |
| Workspace-aware context | Partial **(S7, S9)** | Knowledge search is per workspace; the agent's planning prompt lists the workspace's datasets and documents. Query uses only the selected dataset |
| Structured outputs | Working | `GenerationOutput` pydantic model; Anthropic tool-forced `emit_sql` (`sql_generator.py`) |
| Multi-provider support | **Working (S2)** | `ai/providers.py`: Groq, OpenAI, Anthropic, Gemini, xAI. Gemini and xAI are verified against stubs only (no live keys) |
| Model selection by task | **Working (S2)** | `ai/router.py`, `AI_TASK_ROUTES`; tasks `sql_generation`, `sql_repair` |
| Traceable tool execution | **Working (S2, S9)** | Every provider attempt traced in `ai_calls` (`ai/trace.py`); agent runs and each step's arguments, status and result stored (`agent_steps`) |

## 2. Data intelligence and text-to-SQL

| Capability | Status | Evidence / gap |
|---|---|---|
| Schema-aware SQL generation | Working | `schema_engine.py`, `sql_generator.py` |
| Business metric and semantic context | Working | `semantic_layer.py` (column meanings, table metrics, glossary) |
| Intent and query decomposition | Working | `query_planner.py` (plan used as a contract and for an independent reference calculation) |
| Schema and relationship retrieval | Working | `retrieval_engine.py` (TF-IDF); multi-format eval retrieval recall 100% (`docs/baseline/multiformat.txt`) |
| Dialect-aware SQL | Partial | SQLite only. All datasets are converted to SQLite, so this is by design until live database querying exists |
| SQL security and validation | Working | `sql_validator.py` (sqlglot AST), `sqlite_compile_check.py`, `sql_params.py`, `sql_intent_checker.py` |
| Controlled execution | Working | `query_result_engine.py`: read-only, time and row limits; `tests/test_execution_guardrails.py` |
| Result verification | Working | `result_verifier.py` (independent reference query) |
| Bounded query repair | Working | At most 3 candidates (2 LLM repairs) in `sql_generator.py` |
| Ambiguity detection and clarification | Partial | Ambiguous values detected in `value_grounding.py` and `query_planner.py`, but the user is never asked a clarifying question |
| Cross-dataset reasoning | Missing | One SQLite file per session |
| Evidence-grounded explanations | Working | Trust trace, validation issues and verification status shown with each answer |
| Honest reliability reporting | Working | `data_reliability_agent.py`; evaluation separates provider errors from wrong answers |

## 3. Knowledge hub and RAG

| Capability | Status | Evidence / gap |
|---|---|---|
| TXT / MD / PDF / DOCX ingestion, chunking, search, cited answers, retrieval eval, injection defence | Working **(S7)** | `knowledge.py`, `knowledge_api.py`; retrieval reuses `modules/retrieval_engine` (**S13:** cached inverted `TextIndex`, 25,000-passage cap); `tests/test_s7_knowledge.py` |
| Conversation history, follow-ups, private chats per user, deterministic intent check (knowledge vs data question) | Working **(S8)** | `assistant.py`, `conversation_api.py`; `tests/test_s8_assistant.py`. Data questions are redirected to the Query page, not answered by chat |
| HTML ingestion, OCR for scanned PDFs, FTS5 / vector search, per-document permissions, re-indexing, document versions | Missing | Not built; no vector store (see ARCHITECTURE: measured recall) |

## 4. Data quality and anomaly intelligence

| Capability | Status | Evidence / gap |
|---|---|---|
| Data profiling | **Working (S6)** | `quality.py` profiles every table and column after each import; status on the dataset row, findings in the Runs panel |
| Missingness and distribution analysis | Partial | Completeness and uniqueness scores on query results (`data_reliability_agent.py`); not run on whole datasets |
| Unusual value detection | Partial | IQR / z-score outliers on result rows (`business_insight_agent._outlier_detection`) |
| Schema-change explanations | Partial **(S3, S4)** | Added, removed and type-changed columns detected and audited per run; no AI-written explanation |
| Statistical anomaly detection on datasets | Partial **(S6)** | `quality.py`: deterministic checks against the previous version and a row-count history; no per-value outlier detection |
| AI explanations of validation failures | Missing | |
| Deterministic ingestion validation | Working | `ingestion.py` (ambiguous JSON rejected, Excel header detection, formula cached-value check), `data_normalizer.py` |
| Quarantine and summaries | Partial **(S3)** | Row-level quarantine with reason codes for file imports; AI-written summaries are not built (**S6** findings are deterministic, no model) |

## 5. Investigations

| Capability | Status | Evidence / gap |
|---|---|---|
| Multi-step investigations (trends, period comparison, segment breakdown, document retrieval, labelling facts vs hypotheses) | **Working (S10)** | `investigation.py`, `investigation_api.py`; deterministic SQL facts, rule-based hypotheses labelled unverified, document excerpts as untrusted context; `tests/test_s10_investigations.py` |
| Root-cause proof, forecasting, multi-table joins, free-form investigation questions | Missing | Findings are descriptive; hypotheses are not verified |

## 6. Controlled agentic AI

| Capability | Status | Evidence / gap |
|---|---|---|
| Planner, tool registry with schemas, approval gates, persisted runs/steps, execution trace | **Working (S9)** | `agent.py`, `agent_api.py`; 6 tools; one-shot plan, no replanning from tool output; `tests/test_s9_agent.py` |
| Autonomous loops, tool output fed back to the model, retries of failed steps, running SQL or shell as a tool, scheduled agents | Missing | Deliberately not built: it would let retrieved text steer actions |

## 7. Reports

| Capability | Status | Evidence / gap |
|---|---|---|
| Question, sources, SQL, results, reliability, insights | Working | `report_generator.py` (JSON / HTML / PDF / CSV), stored durably (`report_storage.py`) |
| Time range and filters, limitations, recommendations separated from facts | **Working (S10) for investigations** | Facts / hypotheses / recommendations / limitations are separate sections in JSON and HTML exports; the per-query report is unchanged |

## 8. Multi-provider AI infrastructure

| Capability | Status | Evidence / gap |
|---|---|---|
| Provider adapters | **Working (S2)** | `ai/` package, shared by any task |
| Timeouts, retries, circuit breaker, fallback | Working | `ai_guard.py`, `_run_provider_chain`; `tests/test_llm_fallback.py`, `test_sprint3_ai_protection.py` |
| Token and cost tracking | Working | `AIInvocation`, `UsageDaily`, daily budget (`test_sprint3_token_accounting.py`) |
| Latency and error monitoring | Working | `observability/metrics.py`, `slo.py`, `alerts.py` |
| Gemini, xAI; per-task model config | **Working (S2)** | Off unless key and model are set |
| Comparable model evaluation | **Working (S2)** | `python -m evaluation.multiformat --live --yes --compare ...`; needs provider credits to produce numbers |
| General tool calling for agents | **Working (S9)**, closed set by design | Six typed tools in `agent.TOOLS`; no open-ended tool calling, SQL or shell |

## 9. Safety, governance and evaluation

| Capability | Status | Evidence / gap |
|---|---|---|
| Prompt-injection defences for data | Working | `untrusted_block` with a per-request nonce (`sql_generator.py`); `tests/test_sprint3_prompt_injection.py` |
| Workspace-aware authorization | **Working (S1)** | `workspaces.require_role` |
| Sensitive-data protection in logs | Working | `observability/redaction.py`; ops privacy tests |
| Audit trails | **Working (S1)** | `audit.py` |
| Text-to-SQL evaluation | Working | `evaluation/` (oracle and replay modes), `evaluation/multiformat` (180 tuned + 32 held-out cases + 24 negatives) |
| RAG retrieval, tool-selection / plan, safety-adversarial evaluation | **Working (S11)** | `evaluation/platform/` (`python -m evaluation.platform`); numbers in `docs/baseline/platform_eval.txt`; `tests/test_s11_platform_eval.py` gates them. Live plan quality is not measured yet (needs a funded key) |
| End-to-end answer quality of the knowledge assistant with a real model (faithfulness, helpfulness) | Missing | Citations are validated for existence, not factual support |
| Workspace dashboard of what needs attention (datasets, quality, schedules, approvals, imports, AI) | **Working (S12)** | `overview.py`, `GET /api/workspaces/{id}/overview`, Overview card |
| Route authentication matrix, measured latency of heavy paths, launch checklist | **Working (S12)** | `tests/test_s12_hardening.py`, `evaluation/platform/perf.py`, `docs/baseline/performance.txt`, `docs/LAUNCH_CHECKLIST.md` |
| Per-workspace AI usage, cost, failure and latency report | **Working (S11)** | `governance.py`, `GET /api/workspaces/{id}/ai-usage`, admin-only UI card |

## 10. AI observability

| Capability | Status | Evidence / gap |
|---|---|---|
| Request id, provider, model, latency, tokens, cost, retries, validation outcome, error category | Working | `AIInvocation` rows; request-id middleware |
| Task type, tool calls, multi-step trace ids | **Working (S2)** | `ai_calls`; trace id joins all attempts of one operation |

## Connectors and ingestion

| Capability | Status | Evidence / gap |
|---|---|---|
| File ingestion: CSV, Excel, JSON, SQLite | Working | `modules/ingestion.py`; format fidelity in `docs/baseline/multiformat.txt` |
| Parquet | **Working (S3)** | `connectors/files.py` (pyarrow; size and decoded-size capped) |
| Connector interface | **Working (S3)** | `connectors/base.py`, `registry.py`; five file connectors, full refresh |
| Credential storage, connection lifecycle (create, test, edit, disable, delete) | **Working (S4)** | `secrets_store.py` (Fernet), `connection_api.py`; workspace-owned, admin+ manages |
| PostgreSQL, MySQL source connectors | **Working (S4)** | `connectors/postgres.py`, `mysql.py`; tested against real servers |
| REST API source | **Working (S5)** | `connectors/rest.py`; tested against a real local HTTP server. No OAuth flows |
| S3 source | **Working (S5)** | `connectors/s3_source.py`; tested with moto (in-memory S3) and a local S3-compatible server. QuerySure's own dataset storage (`storage.py`) is separate |
| Batch ingestion as jobs | **Working (S3)** | Persisted job + `ingestion_runs`, batched validation and writing, progress counters. File sources are parsed whole (capped by the upload limits); pagination and backfill apply to live sources (Sprint 4) |
| Incremental ingestion, watermarks, checkpoints, upserts | **Working (S4, S5)** | PostgreSQL / MySQL / REST: integer or timestamp watermark, overlap window, primary-key upserts, checkpoint saved with the swap; deletions are not detected. S3: object ETag / LastModified detection, replace-by-object, vanished objects removed. No CDC or real-time sync |
| Scheduled syncs | **Working (S5)**, requires an always-on process | `scheduler.py`; not available on Render's free plan |
| Retry, resume, no overlapping runs, restart recovery | **Working (S3)** for file imports | One active run per dataset (partial unique index); orphaned jobs mark their run interrupted; a failed run leaves the previous version intact. Resuming mid-file is not possible (a retry re-imports) |
| Schema-change detection | **Working (S3, S4)** | Added / removed / type-changed columns and tables per run, audited; a changed source schema forces a full refresh; incompatible changes are reported, not blocked |
| Raw / Validated / Curated layers | **Working (S3)** | Raw kept per run (last 2), row checks before writing, curated file per version |
| Lineage and ingestion history | **Working (S3)** | `_qs_lineage` per table, `ingestion_runs` history, API and UI |
| Workspace ownership of datasets | **Working (S3)** | Objects under `ws/<workspace>/<dataset>/`, access through the catalog |

## Baseline numbers (tag `v1-querysure`, macOS, Python 3.12.15)

See `docs/baseline/README.md`.
