# Changelog

Notable product, reliability, security, and engineering changes for QuerySure are recorded here. Detailed architecture, evaluation results, configuration, and operating procedures live in `docs/`.

## Unreleased

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
