# Changelog

Notable product, reliability, security, and engineering changes for QuerySure are recorded here. Detailed architecture, evaluation results, configuration, and operating procedures live in `docs/`.

## Unreleased

### Retrieval scale, quality measurement and deployment rehearsal (Sprint 13)
- **Search no longer rescans every passage per question.** `modules/retrieval_engine.TextIndex` (TF-IDF with an inverted index; scores identical to scoring every text, proven by a test) is cached per workspace in `knowledge.py` and rebuilt when the workspace's document set changes (also when another worker changed it). Questions after the first take under 2 ms at 25,000 passages; the one-off build is about 4 s. `KNOWLEDGE_SEARCH_MAX_CHUNKS` default 10,000 -> 25,000; new `KNOWLEDGE_INDEX_CACHE_WORKSPACES` (4). Memory is stated in `docs/baseline/performance.txt`; `render.yaml` sets 5,000 for the 512 MB free plan.
- **Production-mode rehearsal:** `scripts/launch_rehearsal.sh` builds the image and runs the compose services with `ENVIRONMENT=production` (production guards, `alembic upgrade head` on boot, MinIO storage) in an isolated project, runs `scripts/launch_smoke.py` against the API, takes a backup, restores it into a scratch database and verifies it, restarts the services and removes everything it created. Last run passed (see `docs/LAUNCH_CHECKLIST.md` section 7).
- **Live quality tooling:** `python -m evaluation.platform --suite answers` scores cited answers (retrieval -> real prompt -> real validator) in oracle / replay / live modes; `scripts/live_quality.sh` runs the SQL benchmark, agent plans and answers against Groq after an explicit confirmation and writes `docs/baseline/live_quality.txt`. The live run has not been executed in this repository (no provider key was available), so no live number is claimed.
- README and docs rewritten for outside readers (reviewer guide, measured results, honest limits).

### Dashboard, hardening and launch (Sprint 12)
- Workspace **Overview** card (`GET /api/workspaces/{id}/overview`): what needs attention (datasets with quality warnings/errors or not ready, failed schedules, agent approvals waiting), recent imports, counts, and AI usage for admins.
- **Hardening gates:** a test calls every `/api` route anonymously and fails unless it is on a reviewed public list; CI runs the 36-case safety suite. Measured latency (`docs/baseline/performance.txt`, `python -m evaluation.platform.perf`) found two risks, now limited: search scored only the first 30,000 passages and took 4.8 s there (workspaces now hold at most 10,000 passages, uploads beyond that get 409 `INDEX_FULL`), and an investigation scans a table several times (tables over 500,000 rows are refused).
- `docs/LAUNCH_CHECKLIST.md`: build gates, production configuration, processes, smoke test, backup rehearsal and the known limits to disclose.

### Evaluation and governance (Sprint 11)
- `python -m evaluation.platform` evaluates the AI features against the real code, offline and free: retrieval (8 documents, 100 questions: 24 keyword / 60 paraphrase / 16 off-topic, recall@k, MRR, false positives, 95% intervals and a vector-index decision rule fixed before measuring), agent plan and tool selection (oracle / replay / live modes; live needs `--yes` and a key), and a 36-case safety-adversarial suite that plays a compromised model or hostile input against each control (plan validation, approval gates, prompt fences, answer validation, SQL identifiers, workspace isolation). Results: `docs/baseline/platform_eval.txt`.
- Tests gate the suites (and prove the safety suite can fail). `GET /api/workspaces/{id}/ai-usage` and an admin-only AI usage card report calls, failures, tokens, estimated cost and latency per task/provider/day from `ai_calls`, plus AI actions from the audit log.

### Investigations and structured reports (Sprint 10)
- Trend, period comparison and segment breakdown of a dataset (migration `e2a4c6d8f0b5`). Every figure is computed by read-only SQL through the existing query engine and keeps its SQL; hypotheses are rule-based, cite the facts they rest on and are labelled unverified; recommendations and limitations are separate sections; related document excerpts are shown as untrusted context and never feed a figure. No model is involved, and the same inputs give the same facts.
- JSON and HTML exports reuse `report_storage` and the owner-only report download; HTML escapes all data. Investigations and exports are deleted with their dataset, workspace or user. Investigations card in the Workspace view.

### Controlled agent with approvals (Sprint 9)
- A goal becomes a one-shot plan over a registry of six typed tools: `list_datasets`, `list_documents`, `search_documents`, `dataset_quality` (read) and `sync_dataset`, `delete_document` (write). The plan is validated (known tools, schema-checked arguments, ids inside the workspace, reads before writes, step cap) and a bad plan is rejected, not repaired.
- Tool output is never fed back to the model, so text retrieved from documents cannot add steps. Write steps stop as `awaiting_approval`; a person with the tool's role approves the exact stored arguments, approvals expire, roles are re-checked at execution, and every write is audited. Persisted runs and steps (migration `d1f3b5a7c9e4`), shared model call/budget/trace (task `agent_plan`), Agent card in the Workspace view.

### Assistant conversations (Sprint 8)
- Private, persisted chats per user and workspace (`conversations`, `conversation_messages`; migration `c9e1a3b5d7f2`). Follow-up questions use the last turns as context and are retrieved together with the previous question when short.
- Answers reuse the Sprint 7 pipeline (`knowledge.search`, untrusted-passage prompt, validated citations, shared AI budget, provider chain, `ai_calls` trace) through one shared `knowledge_api.generate_answer`; nothing is generated when no passage is relevant. Table-data questions are recognised by wording and redirected to Ask-data (no model call, no invented figures).
- UI: Assistant card in the Workspace view (history list, new chat, delete). Deleting a chat, workspace or user removes the messages. Settings `CONVERSATIONS_MAX_PER_USER_WORKSPACE`, `CONVERSATION_MAX_MESSAGES`, `CONVERSATION_HISTORY_TURNS`.

### Knowledge hub: documents, retrieval and cited answers (Sprint 7)
- Workspaces can hold `.txt`, `.md`, `.pdf` (text layer) and `.docx` documents. They are chunked and stored as text only (the upload is not kept); deleting a document, its workspace or its uploader's account removes the passages.
- Retrieval reuses the schema retrieval's TF-IDF/cosine (`rank_texts`); no vector database. Recall on a fixed 14-question set is measured in `tests/test_s7_knowledge.py`.
- `POST .../knowledge/ask` answers only from retrieved passages, treated as untrusted data, and returns numbered sources. Replies are validated: invented citation numbers are dropped and an answer citing nothing valid is withheld. With no relevant passage no model is called. New AI task `knowledge_answer` (routable with `AI_TASK_ROUTES`), same budget, key rules and `ai_calls` trace as SQL generation.
- New dependency `pypdf`; migration `b8f0a2c4d6e1`; a Knowledge card in the Workspace view. Text-to-SQL is unchanged.

### Data quality profiling and anomaly findings (Sprint 6)
- Every successful import (file, database, REST, S3) now profiles the new dataset version and stores a **quality report**: per column the non-null and empty counts, distinct count and numeric / date ranges (no text values are stored), compared with the previous version and the recent row-count history.
- Deterministic findings: empty table, row-count drop / surge / statistical outlier (history of 4+ versions, 3 sigma), empty column, empty-share spike, type change, column or table added or removed, large shift of a numeric average, high reject rate, high duplicate rate. Thresholds are `QUALITY_*` settings. No model is involved.
- `GET /api/datasets/{id}/quality`; the dataset list carries the latest status; the Workspace Runs panel shows the findings. Migration `a7e9f1b3c5d8`. A failure in profiling never fails or rolls back an import.

### REST and S3 sources, incremental detection and a scheduler (Sprint 5)
- Added a configurable **REST API** source: a connection defines a base URL, an auth method (none, bearer, header, basic) and resources with a path, where the records are, pagination (none, page, offset, cursor, next URL), a primary key and an optional `updated_since` parameter. Requests are GET-only, bounded in pages, size and time, and retried with bounded backoff (honouring `Retry-After`). The server connects to the validated address, verifies TLS against the host name, never follows redirects and refuses a next-page URL on another server. Incremental sync sends the stored watermark (with an overlap) and upserts by primary key; new fields become reported columns.
- Added an **Amazon S3 / S3-compatible** source: a resource is a prefix and a format (CSV, JSON, Parquet, Excel). Incremental sync compares each object's ETag, LastModified and size, downloads only new or changed objects, replaces their rows, removes the rows of objects that disappeared, and records a run with no change as skipped without creating a version. A changed column set falls back to a full refresh. Credentials are always the connection's own; the server's AWS identity and default credential chain are never used.
- Added an interval **scheduler** (`python scheduler.py`, a compose `scheduler` service, or a development thread). A due schedule is claimed atomically so several schedulers fire it once, an active run is skipped, and after downtime it fires once. A schedule disables itself with a recorded reason after repeated failures or when its owner loses access or the connection is disabled or deleted. It needs an always-on process, so it is not available on Render's free plan.
- Deleting a workspace now deletes its saved connections (and their encrypted secrets) and detaches their datasets; deleting a dataset removes its checkpoint and schedule.
- New: `PUT/GET/DELETE /api/datasets/{id}/schedule`; connection types `rest` and `s3`; the Workspace view gains REST/S3 connection forms and an automatic-sync control. Not included: webhooks, change data capture, real-time sync, REST write-back or OAuth flows.

### Database connections, batch and incremental sync (Sprint 4)
- Added PostgreSQL and MySQL / MariaDB connectors. A workspace admin saves a connection (host, port, database, user, TLS mode, password); analysts test it, browse its tables and import a table as a dataset. Connections are read-only and time-limited, and a table without a primary key can still be imported (full refresh).
- Saved passwords are encrypted (Fernet, bound to their connection, rotatable with `CONNECTIONS_ENCRYPTION_KEY`) and are never returned, logged or audited. Changing a connection's host, port or user requires entering the password again. Without the key, connections cannot be created in production; the rest of the app is unaffected.
- Outbound connections are checked: every address a host resolves to must be public unless `CONNECTOR_ALLOW_PRIVATE_HOSTS` is on (off in production). Identifiers come only from the discovered catalog; driver errors are reduced to codes.
- Batch import reads in keyset-paginated batches (bounded memory) and resumes after a transient failure from the last key. Incremental sync re-reads rows past a stored watermark (integer or timestamp, with a configurable overlap) and upserts them by primary key, recording inserted, updated and skipped rows; re-running a window duplicates nothing. The checkpoint is saved in the same transaction as the version swap, so a failed run never advances it. A first import is the backfill; an incremental request that cannot run (no key, no watermark, schema changed) becomes a full refresh and the run records why.
- Values that cannot be stored (for example text over 1,000,000 characters) are quarantined with a reason code. Source schema changes are detected, reported and audited.
- New endpoints under `/api/connections` plus `POST /api/datasets/{id}/sync` and `GET /api/datasets/{id}/source`; the Workspace view gains Database connections, Import, Sync and Full refresh. Not detected: deletions in the source (use a full refresh) and rows whose watermark is NULL. Syncs run on request; scheduling is Sprint 5.
- CI runs the connector tests against real PostgreSQL 16 and MySQL 8.4 services.

### Connectors, layered ingestion and workspace-owned datasets (Sprint 3)
- Added a connector framework (`connectors/`) with CSV, Excel, JSON, **Parquet** (new, via pyarrow) and SQLite connectors. `GET /api/connectors` lists exactly what is registered; all are full-refresh file connectors that need no credentials.
- Uploads now go through a layered pipeline: the original file is kept (Raw, the last 2 per dataset), deterministic row checks run (Validated), and the typed tables are written to one SQLite file per version (Curated). Rejected rows are recorded in a hidden `_qs_quarantine` table with a reason code (`malformed_row`, `empty_row`, `missing_key`); exact duplicate rows are counted and kept. A CSV with a malformed line is now imported with that line quarantined instead of being refused whole.
- Every import is an `ingestion_runs` record: records read / inserted / updated / skipped / rejected, duplicates, reasons, per-table counts, schema changes and a source fingerprint. Only one run per dataset can be active (enforced by the database); a job lost in a restart marks its run interrupted. A new version is built aside and swapped in one transaction, so a failed run leaves the previous version queryable, and re-importing an identical file records a skipped run. Column additions, removals and type changes are detected, reported and audited, never coerced.
- Datasets are now workspace-owned: viewers can open and query a teammate's dataset, analysts can re-import it, and removing a member ends their access at once, including in an open session. **Behaviour change:** a dataset now lives until it is deleted; replacing or flushing a session's dataset only unlinks it, and quota counts each dataset once against its creator (deleting a session no longer frees storage; deleting the dataset does). Pre-Sprint-3 uploads keep their old behaviour.
- New endpoints: `POST /api/datasets`, `POST /api/datasets/{id}/runs`, `GET .../runs`, `GET .../runs/{id}`, `GET .../quarantine`, `POST .../open`. The Workspace view gains Open, Re-import and Runs. `.parquet` is accepted at `/api/connect/upload`; `MAX_UPLOAD_PARQUET_MB` caps it separately (4 MB on the Render demo).
- Internal `_qs_*` tables are hidden from the schema engine, validator, retrieval and previews and are denied by the SQL execution authorizer; SQLite uploads that already use a `_qs_` table name are refused.

### AI provider layer (Sprint 2)
- Moved provider transports, the fallback chain (circuit breakers, bounded retries, request deadline, attempt budget) and token/cost accounting out of `modules/sql_generator.py` into an `ai/` package. Behaviour is unchanged: the 375 existing provider, fallback, injection and evaluation tests pass without modification, and the offline evaluations give the same scores.
- Added per-task routing. SQL generation and SQL repair are separate tasks, and `AI_TASK_ROUTES` can pin each to an ordered `provider[:model]` list. Unknown task or provider names stop startup.
- Added Google Gemini and xAI Grok through their documented OpenAI-compatible endpoints, using strict JSON-Schema structured output. Both are off unless a key and a model id are set; there are no default models. They are tested against a stubbed client only; no live call was made without keys.
- Added an `ai_calls` trace: one row per provider attempt (task, provider, model, outcome, error code, retry index, latency, tokens, cost), grouped by a `trace_id` that `/api/generate-sql` returns and `ai_invocations` stores. Owners can read a trace at `GET /api/ai/traces/{trace_id}`, and the Generate SQL inspector lists the calls. Prompts, SQL, data and keys are never stored. A trace write failure never fails a request. Account export and deletion include these rows.
- Added side-by-side model evaluation: `python -m evaluation.multiformat --live --yes --compare groq --compare openai:MODEL` runs the same cases, pipeline and scoring for each route and tabulates accuracy over answered runs, median latency, tokens and cost.
- The admin providers view now shows each task's chain and how each provider is asked for structured output.

### Workspaces, roles, dataset catalog and audit trail (Sprint 1)
- Every account now has a personal workspace, and users can create team workspaces with `owner`, `admin`, `analyst` and `viewer` roles. One server-side check (`workspaces.require_role`) authorizes every workspace route; non-members get 404 and insufficient roles get 403.
- Uploaded datasets are recorded in a workspace dataset catalog (`datasets` table), linked from the session that uses them. A catalog entry is removed together with its stored file. Uploads can target a team workspace where the uploader is analyst or above.
- Members can see a team workspace's catalog; each stored file is still readable only by the account that uploaded it. Shared querying of another member's dataset is not part of this release.
- A dataset's creator, or a workspace admin, can delete it: the file is removed first, and every session using it is emptied. When a member leaves or is removed, or a team workspace is deleted, their datasets move to their personal workspace instead of being lost.
- Added an append-only audit trail (`audit_log`) for sign-in, upload, query execution, report generation, dataset deletion, and workspace and member changes. Details are allow-listed scalars only; SQL, questions, data values, e-mail addresses, storage references and uploaded file names are never recorded; datasets appear by ID only. An audit failure never fails the audited action.
- Account export includes workspace memberships and catalogued datasets. Account deletion removes owned team workspaces, moving other members' datasets to their personal workspaces.
- Migration `b8d2e4f6a1c3` creates the tables and backfills a personal workspace for every existing user and a catalog entry for every existing uploaded dataset. It is reversible.
- New Workspace view in the app: workspace switcher, dataset catalog, members (add, change role, remove, leave), audit trail with paging, and create, rename or delete workspace.
- Added `docs/CAPABILITY_MAP.md` (each target capability classified against the code) and `docs/baseline/` (pre-Sprint-1 test and evaluation results).

### Render Free Demo Mode
- Added an automatic execution path for supported uploads and reports when no background worker is available. Jobs run in the web process in this mode, so they are **not durable across restarts, redeploys, or crashes**. Interrupted jobs return a retry message rather than remaining queued indefinitely.
- Set the Render demo web process to one worker to reduce memory pressure. Docker Compose now passes `WEB_CONCURRENCY` through to the application.
- Added configurable upload limits: `MAX_UPLOAD_MB` defaults to 10 MB, and `MAX_UPLOAD_XLSX_MB` can set a lower limit for Excel. The Render demo configuration caps Excel uploads at 2 MB.
- Improved job-state handling so a late result cannot overwrite a reported failure, and fixed upload-size checks that incorrectly counted multipart form overhead.
- Updated browser job polling to wait for the server's final job state and display elapsed time.

### Authentication and Email Delivery
- Added HTTPS email-provider options (`brevo` and `resend`) for deployments where outbound SMTP is unavailable. Configure the provider and API key through environment variables.
- Added bounded email-request timeouts, reduced retries for permanent failures, and timing logs for signup and email dispatch. Logs avoid email addresses, verification links, and tokens.
- Added automatic email-delivery behavior that can use a worker when available or send from the web process in a workerless demo configuration. Verification tokens, expiration, and one-time use remain enforced.

### Query Accuracy and Validation
- Improved schema retrieval with CamelCase splitting and plural-aware matching; larger schemas prioritize tables connected to the requested query.
- Improved query planning for date columns, counts, ranking, aggregate thresholds, units, top-level questions, and other common natural-language patterns.
- Tightened semantic checks for ambiguous joins, aggregation, ranking direction, short filter values, hierarchical and long-format data, and unsupported percentage calculations.
- Improved result verification to avoid certain false mismatch warnings and to calculate shares against the full filtered result when a top-N limit is used.
- Added multi-format evaluation tooling for CSV, Excel, JSON, and SQLite datasets, including holdout and negative cases. Evaluation distinguishes provider errors from incorrect answers.
- On the recorded hierarchical CSV cases, semantic checks flagged 21 of 23 known-wrong SQL answers and none of 18 recorded-correct answers. This is a dataset-specific result, not a general accuracy guarantee.

### Workspace and Reliability
- Improved dataset switching and upload-state handling so stale answers are cleared, Ask is locked during uploads, failed uploads preserve the active dataset, and late preview responses for an older dataset are discarded.
- Improved history deletion feedback and clarified that history entries are snapshots rather than guaranteed rerunnable sessions.
- Fixed the Docker Compose worker health check and aligned the Anthropic fallback-model default with application configuration.
- Added regression coverage for workerless jobs, deployment configuration, query validation, and format-specific behavior.

### Security, Packaging, and Documentation
- Removed credentials from Docker Compose configuration; database passwords and JWT secrets are provided through environment variables.
- Added safer release packaging checks to exclude `.env` files, local databases, IDE metadata, caches, and other private artifacts from release archives.
- Updated the README and consolidated product, architecture, evaluation, environment, API, billing, and operations documentation.

### Known Demo Limitations
- Workerless jobs run in-process and can be interrupted by a service restart or deployment.
- The Render Free profile is CPU- and memory-constrained. CSV imports can take several minutes, and Excel imports are especially slow; the demo therefore uses a lower Excel upload limit.
- Free hosting and database retention are provider-controlled and are not suitable as a guarantee of durable production storage.
- AI answer quality varies by provider, dataset, and question. Do not infer a universal accuracy rate from tuned benchmark cases.

## v1.0.7 — 2026-10-23

### Added and Improved
- Updated default LLM models and fallback configuration; model IDs remain configurable.
- Added or expanded authentication, billing, reporting, query-history, observability, operational, and evaluation components.
- Improved answer wording, truncation reporting, low-confidence calibration, and report-failure handling.
- Improved fresh local setup, including sample-database generation and optional Google sign-in configuration.

### Render free demo mode
- **Uploads and reports no longer wait for a worker that does not exist.** In production every upload and PDF report went to
  the RQ queue, and only the paid `querysure-worker` consumes it; on Render's free plan they stayed `queued` until the 25-minute
  stale reaper failed them. With `JOB_EXECUTION=auto` (default) they now run in the web process right after the `202` response
  whenever no worker heartbeat is visible, one heavy job at a time per process (`IN_PROCESS_JOB_CONCURRENCY=1`). These
  in-process jobs are **not durable**: a restart loses them. Their rows carry a heartbeat, and with no worker alive a job whose
  heartbeat is older than `JOB_ORPHAN_SECONDS` (90 s) is failed as `JOB_INTERRUPTED` with a retry message and its upload quota
  released (on the next status poll and by the maintenance reaper), instead of staying `queued`. The same applies to a job
  queued with `JOB_EXECUTION=queue` when no worker runs. A late worker never resurrects such a job.
- **Configurable upload limit, default 10 MB** (`MAX_UPLOAD_MB`), below the existing per-format ceilings (50/50/100 MB).
- **One web process on Render free.** `render.yaml` sets `WEB_CONCURRENCY=1`, `MAX_UPLOAD_MB=10`, `JOB_EXECUTION=auto` on the web
  service; Compose now passes `WEB_CONCURRENCY`, `MAX_UPLOAD_MB` and `JOB_EXECUTION` from `.env` to the API container (the image's
  start command already read `WEB_CONCURRENCY`; Compose never set it, so it was always 2).
- **A late result never overwrites a reported failure.** A job failed as `JOB_INTERRUPTED`/`JOB_STALE` while its thread was
  still working (found in a 0.1-CPU test: a 10 MB workbook parsed for over 25 minutes) now discards its result instead of
  switching the user's dataset or marking itself succeeded; its stored objects are deleted.
- **Excel limit** `MAX_UPLOAD_XLSX_MB` (default = `MAX_UPLOAD_MB`; `render.yaml` sets 2): at 0.1 CPU a 1 MB workbook took 345 s.
- **The browser no longer gives up after 3 minutes.** `pollJob` waited 180 polls; a 10 MB CSV took 305 s at 0.1 CPU and the UI
  said "timed out" although the job succeeded. It now waits up to 30 minutes (the server ends every job before then) and
  shows the elapsed time.
- **Files just under the limit were refused.** The fast `Content-Length` check compared the whole multipart body to the file
  limit; it now allows 64 KiB of framing (the streamed byte count stays exact).
- Measured under Render-free limits (512 MB, 0.1 CPU, one process, no worker): see OPERATIONS.md "Render free demo mode".
- Tests: `tests/test_render_free_demo.py` (27). Docs: OPERATIONS.md "Render free demo mode", ENVIRONMENT.md, 02_ARCHITECTURE.md.

### Fixed
- Reduced false `mismatch` verdicts on the 44-query gold benchmark from 3 to 0 while retaining checks for real mismatches.
- Fixed several result-verification edge cases involving aggregates, thresholds, date ranges, counted entities, and duration questions.
- Improved test coverage for routes and added dependency vulnerability scanning to CI.

### Removed
- Removed obsolete internal sprint and audit notes, superseded reliability reports, and the generated `retail.db` file from the shipped repository contents.

### Documentation
- Added the portfolio documentation set for the product case study, architecture, and evaluation, with Markdown and PDF versions.
