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

### Fixed
- Reduced false `mismatch` verdicts on the 44-query gold benchmark from 3 to 0 while retaining checks for real mismatches.
- Fixed several result-verification edge cases involving aggregates, thresholds, date ranges, counted entities, and duration questions.
- Improved test coverage for routes and added dependency vulnerability scanning to CI.

### Removed
- Removed obsolete internal sprint and audit notes, superseded reliability reports, and the generated `retail.db` file from the shipped repository contents.

### Documentation
- Added the portfolio documentation set for the product case study, architecture, and evaluation, with Markdown and PDF versions.
