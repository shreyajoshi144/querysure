# Environment variables

Every variable the code reads (`config.py` is the only module allowed to read the environment, plus a few tunables in
`observability/`). **Defaults are the real runtime values** of the `config` module in a clean `development` environment;
secrets show `(unset)`. `tests/test_sprint5_docs.py` fails if a variable is read by the code but missing here.

AWS credentials for the `s3` backend (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`) are read by boto3, not by QuerySure.

## Production start-up guards

With `ENVIRONMENT=production` the process **refuses to start** unless: `DATABASE_URL` is Postgres; `REDIS_URL` is set; `JWT_SECRET_KEY` is set and at least 16 characters
(a warning is printed below 32; use `secrets.token_urlsafe(64)`); `CORS_ALLOWED_ORIGINS` is explicit (not `*`); `TRUSTED_PROXY_IPS` is explicit; e-mail is configured (`smtp`: `SMTP_HOST`, `SMTP_USERNAME`,
`SMTP_PASSWORD`, `EMAIL_FROM`; `brevo`/`resend`: `EMAIL_API_KEY`, `EMAIL_FROM`) and `APP_BASE_URL` is https; **object storage is configured** (`S3_BUCKET`, with the `s3` backend);
and, if `PAYMENT_PROVIDER` is set, `PAYMENT_WEBHOOK_SECRETS` is set and `PAYMENT_CHECKOUT_URL` / `PAYMENT_PORTAL_URL` are https. The worker and
the maintenance cron import the same config, so they need the same variables.

## Core & deployment

| Variable | Default | Purpose |
|---|---|---|
| `ENVIRONMENT` | `'development'` | `development` or `production`. `production` turns on every startup guard below (config.py refuses to start when one fails). |
| `HOST` | `—` | Bind address for `python api.py` / `start.py` (not used by uvicorn when started with `--host`). |
| `PORT` | `—` | Listen port for `start.py`. |
| `WEB_CONCURRENCY` | `2` | Uvicorn worker processes per instance (read by the Docker `CMD`, the Render start command and Compose). Pool sizing must satisfy workers x (DB_POOL_SIZE + DB_MAX_OVERFLOW) < Postgres connection limit. `render.yaml` sets `1` for the free plan (512 MB): each idle process holds about 135 MB. |
| `THREADPOOL_MAX_WORKERS` | `20` | Size of the sync-endpoint thread pool per process. |
| `APP_BASE_URL` | `'http://localhost:5000'` | Public https origin of the app; used in e-mail links and the billing return URL. Required in production. |
| `CORS_ALLOWED_ORIGINS` | `['*']` | Comma-separated allowed origins. `*` is refused in production. |
| `TRUSTED_PROXY_IPS` | `''` | Proxy IPs/CIDRs whose `X-Forwarded-For` is trusted for rate-limit keys. Never `*` in production. |
| `PRODUCT_CONTACT_EMAIL` | `''` | Contact address shown for the Business plan and in the site pages. |
| `ADMIN_EMAILS` | `[]` | Comma-separated e-mails granted the admin (operations) role. Admins see aggregate operations data only, never customer content. |
| `CSP_REPORT_ONLY` | `False` | `1` sends the CSP as report-only (diagnosis only); production sets `0`. |

## Database, cache & storage

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `(unset)` | SQLAlchemy URL. Production requires Postgres (SQLite is refused). |
| `DB_POOL_SIZE` | `5` | Connections kept per process. |
| `DB_MAX_OVERFLOW` | `5` | Extra burst connections per process. |
| `DB_POOL_TIMEOUT` | `10` | Seconds to wait for a pooled connection before failing. |
| `DB_POOL_RECYCLE` | `1800` | Seconds after which a pooled connection is recycled. |
| `REDIS_URL` | `(unset)` | Redis for rate limiting, quota/budget counters, job queue (RQ) and metrics. Required in production. |
| `STORAGE_BACKEND` | `'local'` | `local` (default) or `s3` for uploaded datasets and reports. |
| `S3_BUCKET` | `''` | Bucket for the `s3` backend. |
| `S3_PREFIX` | `'datasets'` | Key prefix inside the bucket. |
| `S3_REGION` | `None` | S3 region. |
| `S3_ENDPOINT_URL` | `None` | Custom endpoint for S3-compatible stores. |
| `STORAGE_CACHE_MAX_MB` | `2048` | Size cap of the local download cache used with remote storage. |

### Docker Compose only

Read by `docker-compose.yml` for variable substitution, not by `config.py`. Compose stops with a message when a required one is missing; nothing has a password default.

| Variable | Default | Meaning |
|---|---|---|
| `POSTGRES_USER` | `querysure` | Database role created by the `db` service and used in the API/worker `DATABASE_URL`. |
| `POSTGRES_PASSWORD` | **required** | Its password. Letters, digits, `-` and `_` only (it is placed in a URL). An existing volume keeps the password it was created with. |
| `POSTGRES_DB` | `querysure` | Database name. |

`JWT_SECRET_KEY` (below) is also required by Compose.

## Authentication & sessions

| Variable | Default | Purpose |
|---|---|---|
| `JWT_SECRET_KEY` | `(unset)` | HMAC key for access tokens. Required in production (min 16 characters enforced, 32+ recommended). |
| `JWT_ALGORITHM` | `'HS256'` | JWT signing algorithm (HS256). |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `1440` | Absolute lifetime of an access token / session. |
| `AUTH_REQUIRE_SESSION_ID` | `False` | `1` refuses tokens without a server-side session id (pre-Sprint-5 tokens). Default: on in production. |
| `AUTH_MAX_ACTIVE_SESSIONS` | `20` | Live sessions per user; the oldest are revoked beyond this. |
| `AUTH_EMAIL_COOLDOWN_SECONDS` | `0` | Minimum seconds between two verification/reset e-mails for one account. Default 60 in production, 0 elsewhere. |
| `GOOGLE_CLIENT_ID` | `''` | Google Identity Services client id. Optional: when unset, Google sign-in is hidden and email sign-in still works. |
| `EMAIL_PROVIDER` | `'smtp'` | `smtp`, `brevo` or `resend` (HTTPS e-mail APIs, port 443: use one of these where outbound SMTP is blocked, as on Render's free instances), or `dev` (writes `.eml` files; not allowed in production). |
| `EMAIL_API_KEY` | `''` | API key for `brevo` or `resend`. Required in production with those providers. |
| `EMAIL_TIMEOUT_SECONDS` | `10` | Bound (1-30 s) for each e-mail network call: the HTTPS request, or each SMTP step. |
| `AUTH_EMAIL_DELIVERY` | `'auto'` | `queue` (background worker sends), `background` (the web process sends right after the response; for deployments without a worker), or `auto` (queue when a worker heartbeat is live, otherwise background). Sign-up never waits for the mail server. |
| `SMTP_HOST` | `''` | SMTP server (production). |
| `SMTP_PORT` | `587` | SMTP port. |
| `SMTP_USERNAME` | `''` | SMTP user. |
| `SMTP_PASSWORD` | `(unset)` | SMTP password. |
| `EMAIL_FROM` | `''` | Sender address. |
| `DEV_EMAIL_DIR` | `—` | Directory for `dev` provider `.eml` files. |

## Billing (Sprint 5)

| Variable | Default | Purpose |
|---|---|---|
| `PAYMENT_PROVIDER` | `''` | Name of a registered payment adapter (`generic` ships). Empty = billing disabled. |
| `PAYMENT_WEBHOOK_SECRETS` | `(unset)` | Comma-separated webhook signing secrets (16+ chars each; several allow rotation). Required in production when a provider is set. |
| `PAYMENT_CHECKOUT_URL` | `''` | Generic adapter: hosted checkout page URL (https in production). |
| `PAYMENT_CHECKOUT_SIGNING_SECRET` | `(unset)` | Generic adapter: secret used to sign the checkout link. |
| `PAYMENT_PORTAL_URL` | `''` | Customer-portal URL shown as 'Manage billing' (https in production). |
| `BILLING_PLAN_MAP_JSON` | `''` | JSON map provider price id -> QuerySure plan key. |
| `BILLING_WEBHOOK_TOLERANCE_SECONDS` | `300` | Replay window for webhook timestamps. |
| `BILLING_WEBHOOK_MAX_BYTES` | `65536` | Maximum webhook body size. |
| `BILLING_GRACE_HOURS` | `72` | Paid access survives this long past `current_period_end`. |
| `BILLING_CHECKOUT_TTL_MINUTES` | `30` | Lifetime of a pending checkout session. |
| `BILLING_UNRESOLVED_RETRY_HOURS` | `24` | How long an unlinkable event is answered 503 (retry) before being ignored. |

## Plans, quotas & upload limits

| Variable | Default | Purpose |
|---|---|---|
| `QUOTA_LIMITS_JSON` | `—` | JSON override of per-plan resource quotas (storage, sessions). |
| `QUOTA_EMPTY_SESSION_RECLAIM_SECONDS` | `3600` | Age after which an empty session is reclaimed against the quota. |
| `MAX_INGEST_ROWS` | `1000000` | Maximum rows ingested from an uploaded file. |
| `MAX_INGEST_COLUMNS` | `200` | Maximum columns. |
| `MAX_INGEST_SHEETS` | `20` | Maximum sheets in a workbook. |
| `MAX_XLSX_UNCOMPRESSED_MB` | `512` | Zip-bomb guard: maximum uncompressed workbook size. |
| `MAX_UPLOAD_MB` | `10` | Largest upload in MB, any format (each format also keeps its own ceiling: CSV/JSON/Excel 50 MB, SQLite 100 MB). The upload job holds the dataset in memory: a 10 MB file peaks at about 135-360 MB in one process, see [OPERATIONS.md](OPERATIONS.md#render-free-demo-mode). |
| `MAX_UPLOAD_XLSX_MB` | `10` (= `MAX_UPLOAD_MB`) | Lower limit for Excel (`.xlsx`/`.xlsm`) only; the lower of the two applies. Excel parsing is much slower than CSV: on 0.1 CPU a 1 MB workbook took about 6 minutes. `render.yaml` sets `2`. |
| `MAX_UPLOAD_PARQUET_MB` | `10` (= `MAX_UPLOAD_MB`) | Lower limit for Parquet (`.parquet`) only. Columnar compression means a small file can decode to far more memory; the decoded size is also capped at 64 MB. Measured: a 5.8 MB file (330,000 rows, 5 columns) peaked at 223 MB in the import process. |
| `CONNECTIONS_ENCRYPTION_KEY` | `(unset)` | Fernet key that encrypts saved database passwords (several comma-separated keys allow rotation: the first encrypts, all decrypt). Required in production to create or use connections (the app still starts without it; connection routes answer `503 CONNECTIONS_NOT_CONFIGURED`). In development a key is derived from `JWT_SECRET_KEY`. Generate one with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Losing the key makes saved passwords unreadable; they must be entered again. |
| `CONNECTOR_ALLOW_PRIVATE_HOSTS` | `true` in development, `false` in production | When false, connections to private, loopback, link-local or otherwise non-public addresses are refused (every address the host resolves to is checked). |
| `CONNECTION_TIMEOUT_SECONDS` | `10` | Connect timeout for source databases (1-60). |
| `CONNECTION_QUERY_TIMEOUT_SECONDS` | `60` | Statement / read timeout for source queries (1-600). |
| `CONNECTION_SYNC_MAX_SECONDS` | `900` | A sync running longer than this is stopped (30-3600). |
| `CONNECTION_FETCH_BATCH_ROWS` | `5000` | Rows per keyset batch read from a source (100-50000). |
| `INCREMENTAL_OVERLAP_SECONDS` | `300` | Timestamp watermarks are re-read this far back so late commits are not missed; upserts by primary key make the re-read idempotent (0-86400). |
| `SOURCE_MAX_PAGES` | `500` | REST pages read in one run (1-5000); more stops the run with `TOO_MANY_PAGES`. |
| `SOURCE_MAX_RESPONSE_MB` | `5` | Largest single REST response body accepted (1-100). |
| `S3_SOURCE_MAX_OBJECTS` | `200` | Matching objects per S3 resource (1-5000); more is refused (`TOO_MANY_OBJECTS`): narrow the prefix. |
| `S3_SOURCE_MAX_OBJECT_MB` | `= MAX_UPLOAD_MB` | Largest single S3 object read (the same ceiling as an upload). |
| `S3_SOURCE_MAX_TOTAL_MB` | `50` | Total object bytes read in one S3 run. |
| `SCHEDULER_ENABLED` | `false` | Run the scheduler as a thread inside the API process (development convenience). Production runs `python scheduler.py` (compose `scheduler` service) instead; either needs an always-on host, so scheduling is not available on Render's free plan. |
| `SCHEDULER_TICK_SECONDS` | `30` | How often the scheduler looks for due schedules (1-600). |
| `SCHEDULER_MIN_INTERVAL_MINUTES` | `5` | Shortest allowed schedule interval (1-1440). |
| `SCHEDULER_MAX_CONSECUTIVE_FAILURES` | `5` | A schedule disables itself after this many failed runs in a row (1-100). |
| `KNOWLEDGE_MAX_FILE_MB` | `10` | Largest knowledge document (1-50 MB). |
| `KNOWLEDGE_MAX_DOCS_PER_WORKSPACE` | `200` | Documents per workspace. |
| `KNOWLEDGE_MAX_CHUNKS_PER_DOC` | `3000` | Passages per document (a larger document is refused). |
| `KNOWLEDGE_MAX_PDF_PAGES` | `500` | PDF page cap. |
| `KNOWLEDGE_CHUNK_CHARS` / `KNOWLEDGE_CHUNK_OVERLAP` | `900` / `120` | Passage size and overlap in characters. |
| `KNOWLEDGE_TOP_K` | `6` | Passages sent to the model per question (1-20). |
| `KNOWLEDGE_MIN_SCORE` | `0.08` | Cosine floor: below it a passage is not relevant, and with no relevant passage no model is called. |
| `KNOWLEDGE_SEARCH_MAX_CHUNKS` | `25000` | Passages one workspace may hold (an upload that would exceed it gets 409 `INDEX_FULL`); search covers all of them. The one-off index build is about 4 s at the default; see `docs/baseline/performance.txt`. |
| `KNOWLEDGE_INDEX_CACHE_WORKSPACES` | `4` | Workspaces whose search index stays in memory per process (least recently used leave first). Raise it only after checking memory: a 25,000-passage index held about 285 MB in the benchmark (the build peaks near 520 MB), so on a 512 MB instance lower `KNOWLEDGE_SEARCH_MAX_CHUNKS` to about 5,000. |
| `CONVERSATIONS_MAX_PER_USER_WORKSPACE` | `100` | Assistant conversations one user may keep per workspace. |
| `CONVERSATION_MAX_MESSAGES` | `200` | Messages per conversation; beyond it the chat is full (409). |
| `INVESTIGATION_MAX_ROWS` | `5000` | Rows an investigation query may return. |
| `INVESTIGATION_QUERY_TIMEOUT_SECONDS` | `15` | Timeout of each investigation query. |
| `INVESTIGATION_MAX_TABLE_ROWS` | `500000` | Tables with more rows are refused (422 `TABLE_TOO_LARGE`): a run scans the table several times. |
| `INVESTIGATIONS_KEEP` | `50` | Investigations kept per dataset; older ones (and exports) are deleted. |
| `AGENT_ENABLED` | `true` | Kill switch for the controlled agent (503 when off). |
| `AGENT_MAX_STEPS` | `6` | Most steps a plan may have. |
| `AGENT_GOAL_MAX_CHARS` | `1000` | Longest goal accepted. |
| `AGENT_APPROVAL_TTL_HOURS` | `24` | A waiting approval expires after this many hours. |
| `AGENT_RUNS_KEEP` | `100` | Runs kept per user and workspace; older ones are deleted. |
| `CONVERSATION_HISTORY_TURNS` | `6` | Previous messages sent to the model as follow-up context (0 disables). |
| `QUALITY_ENABLED` | `true` | Profile every new dataset version and store data-quality findings (Sprint 6). A failure never fails an import. |
| `QUALITY_ROW_DROP_PCT` | `50` | Warn when a table loses at least this share of its rows versus the previous version (1-100). |
| `QUALITY_NULL_SPIKE_POINTS` | `20` | Warn when a column's empty share rises by this many percentage points (1-100). |
| `QUALITY_REJECT_WARN_PCT` / `QUALITY_REJECT_ERROR_PCT` | `5` / `20` | Reject-rate thresholds for a warning and an error (0 disables). |
| `QUALITY_DUPLICATE_WARN_PCT` | `5` | Warn when this share of rows are exact duplicates (0 disables). |
| `QUALITY_NUMERIC_SHIFT_PCT` | `50` | Warn when a numeric column's average moves this much versus the previous version. |
| `QUALITY_DISTINCT_MAX_ROWS` | `2000000` | Skip distinct counts on tables larger than this. |
| `QUALITY_REPORTS_KEEP` | `30` | Reports kept per dataset; older ones are trimmed (5-500). |
| `SCHEDULER_HEARTBEAT_FILE` | `/tmp/qs-scheduler.alive` | File the scheduler touches every tick; the compose healthcheck reads its age. |
| `MAX_XLSX_COMPRESSION_RATIO` | `100.0` | Zip-bomb guard: maximum compression ratio. |
| `CONCURRENCY_GENERATE_SQL` | `2` | Concurrent SQL-generation requests per process. |
| `CONCURRENCY_UPLOAD` | `1` | Concurrent uploads per process. |
| `CONCURRENCY_EXECUTE_SQL` | `1` | Concurrent SQL executions per process. |
| `API_ADMISSION_LIMIT` | `16` | Global concurrent-request admission limit per process. |
| `API_ADMISSION_WAIT_SECONDS` | `8.0` | How long a request waits for admission before a 503. |
| `JOB_STALE_SECONDS` | `1500` | Age after which a running job is considered stale. |
| `JOB_EXECUTION` | `'auto'` | Where upload and report jobs run: `queue` (the RQ worker), `in_process` (the web process, right after the response; for deployments without a worker), or `auto` (queue while a worker heartbeat is live, otherwise in-process; blank = auto). In-process jobs are **not durable**: a restart loses them and they are failed as `JOB_INTERRUPTED`. Any other value refuses to start. |
| `IN_PROCESS_JOB_CONCURRENCY` | `1` | Heavy in-process jobs (uploads, reports) one web process runs at once; the rest wait their turn (bounds peak memory). |
| `JOB_ORPHAN_SECONDS` | `90` | With no worker alive, a queued/running job whose heartbeat is older than this is failed as `JOB_INTERRUPTED` (on the next status poll, or by the maintenance reaper) and releases its upload quota. Minimum 30; must be at least 3 x `JOB_HEARTBEAT_SECONDS`. |
| `JOB_HEARTBEAT_SECONDS` | `15` | How often a web process refreshes the heartbeat (`jobs.updated_at`) of its unfinished in-process jobs. |

## LLM providers & cost control

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | `(unset)` | Groq key (server-side provider). |
| `OPENAI_API_KEY` | `(unset)` | OpenAI key (fallback provider). |
| `ANTHROPIC_API_KEY` | `(unset)` | Anthropic key (fallback provider). |
| `GROQ_MODEL` | `'openai/gpt-oss-120b'` | Primary Groq model. The previous Llama defaults were shut down by Groq on 2026-08-16. |
| `GROQ_FALLBACK_MODEL` | `'openai/gpt-oss-20b'` | Groq fallback model. |
| `GROQ_REASONING_EFFORT` | `'low'` | Sent as `reasoning_effort` to Groq `openai/gpt-oss-*` models so hidden reasoning does not consume the output budget. Empty = not sent. |
| `LOW_RETRIEVAL_CONFIDENCE` | `0.05` | Mean retrieval score below which an answer's SQL confidence is shown as Low. Calibrated on the 62-case benchmark. |
| `LLM_MAX_OUTPUT_TOKENS` | `4000` | Output-token cap per Groq call (256-16000). OpenAI and Anthropic calls stay capped at 1500. |
| `OPENAI_FALLBACK_MODEL` | `'gpt-4o-mini'` | OpenAI fallback model (OpenAI lists a 2027-01-14 shutdown for its snapshot). |
| `LLM_PROVIDER_ORDER` | `'groq,openai,anthropic'` | Order the configured providers are tried in. Providers without a key are skipped; unknown names ignored. Known: `groq`, `openai`, `anthropic`, `gemini`, `xai`; any not listed are tried after the listed ones. |
| `GEMINI_API_KEY` | `(unset)` | Google Gemini key, used through Google's OpenAI-compatible endpoint (documented by Google as beta) with JSON-Schema structured output. Ignored unless `GEMINI_MODEL` is also set. |
| `GEMINI_MODEL` | `(unset)` | Gemini model id. No default: QuerySure does not assume which models exist. |
| `XAI_API_KEY` | `(unset)` | xAI Grok key, used through `https://api.x.ai/v1` with JSON-Schema structured output. Ignored unless `XAI_MODEL` is also set. |
| `XAI_MODEL` | `(unset)` | xAI model id. No default. |
| `AI_TASK_ROUTES` | `(unset)` | Per-task routing: `task=provider[:model],...` separated by `;`, for tasks `sql_generation` and `sql_repair`, e.g. `sql_repair=openai:gpt-4.1-mini,groq`. A routed task tries only the listed providers in order (those without a key are skipped); unrouted tasks use `LLM_PROVIDER_ORDER`. Unknown task or provider names stop startup. |
| `LLM_TRANSIENT_RETRIES` | `1` | Extra same-model retries (0-3) for timeouts, connection errors and 5xx. Auth, invalid model, 400, 429 and malformed replies fail over immediately. |
| `LLM_RETRY_BACKOFF_SECONDS` | `0.5` | Base of the exponential backoff between those retries (0-5 s), capped by the request deadline. |
| `LLM_MAX_ATTEMPTS_PER_REQUEST` | `8` | Hard cap on provider calls per question, shared by fallbacks, the JSON retry and SQL repair rounds. |
| `ANTHROPIC_FALLBACK_MODEL` | `'claude-haiku-4-5-20251001'` | Anthropic fallback model. |
| `AI_KILL_SWITCH` | `False` | `1` disables all LLM calls immediately. |
| `AI_PROVIDER_TIMEOUT_SECONDS` | `25.0` | Per-call provider timeout. |
| `AI_REQUEST_DEADLINE_SECONDS` | `60.0` | Overall deadline for one request's AI work. |
| `AI_MIN_CALL_BUDGET_SECONDS` | `3.0` | Minimum remaining deadline required to start another provider call. |
| `CIRCUIT_FAILURE_THRESHOLD` | `5` | Consecutive failures that open a provider circuit. |
| `CIRCUIT_COOLDOWN_SECONDS` | `30.0` | Seconds a circuit stays open. |
| `DAILY_LLM_BUDGET_USD` | `2.0` | Per-user daily LLM spend cap before plan multipliers. |
| `GLOBAL_DAILY_AI_BUDGET_USD` | `25.0` | Global daily LLM spend cap. |
| `PLAN_BUDGET_MULTIPLIERS_JSON` | `—` | JSON override of per-plan budget multipliers. |
| `LLM_UNLIMITED_PLAN_MULTIPLIER` | `20.0` | Budget multiplier applied to plans with no query limit. |
| `LLM_PRICES_JSON` | `—` | JSON override of per-model token prices used for cost estimates. |
| `LLM_DEFAULT_PRICE_IN` | `'<<not an attribute>>'` | Fallback input-token price for unknown models (set via env; unset uses the built-in default). |
| `LLM_DEFAULT_PRICE_OUT` | `'<<not an attribute>>'` | Fallback output-token price for unknown models. |

## Observability, SLOs & alerts

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | `'INFO'` | Log level. |
| `LOG_FORMAT` | `'json'` | `json` or text. |
| `LOG_HASH_KEY` | `(unset)` | Key for pseudonymous user references in logs; keep it stable (needed to re-apply deletions after a restore). |
| `LOG_EXCEPTION_MESSAGES` | `False` | `1` includes exception messages in logs (off by default; messages can contain data). |
| `SENTRY_DSN` | `(unset)` | Enables Sentry error reporting when set. |
| `SENTRY_ENVIRONMENT` | `'development'` | Sentry environment tag. |
| `METRICS_ENABLED` | `True` | Collect operational metrics. |
| `METRICS_FLUSH_INTERVAL_SECONDS` | `1.0` | How often process metrics are flushed to Redis. |
| `METRICS_RETENTION_SECONDS` | `21600` | Metric retention. |
| `GAUGE_TTL_SECONDS` | `30` | Lifetime of gauge values. |
| `OPS_METRICS_TOKEN` | `(unset)` | Token for the Prometheus endpoint (`X-Ops-Token`); admins may also read it. |
| `OPS_EVENT_RETENTION_DAYS` | `30` | Retention of operational events/incidents. |
| `READINESS_CACHE_SECONDS` | `5.0` | Cache time of the readiness result. |
| `READINESS_DB_TIMEOUT_MS` | `2000` | Database probe timeout for readiness. |
| `WORKER_HEARTBEAT_INTERVAL_SECONDS` | `10` | Worker heartbeat period. |
| `WORKER_HEARTBEAT_TTL_SECONDS` | `30` | Heartbeat expiry before a worker counts as gone. |
| `ALERTS_ENABLED` | `False` | Enable the alert evaluator. |
| `ALERT_EVAL_INTERVAL_SECONDS` | `30` | Alert evaluation period. |
| `ALERT_WEBHOOK_URL` | `''` | Webhook that receives alert notifications. |
| `ALERT_DB_POOL_UTILIZATION_PCT` | `90.0` | DB pool utilisation alert threshold. |
| `ALERT_GLOBAL_BUDGET_PCT` | `80.0` | Global AI-budget consumption alert threshold. |
| `ALERT_QUEUE_DEPTH` | `100` | Queue depth alert threshold. |
| `ALERT_QUEUE_OLDEST_SECONDS` | `300` | Oldest-queued-job age alert threshold. |
| `ALERT_RESOLVE_AFTER_SECONDS` | `120` | Quiet period before an alert auto-resolves. |
| `SLO_WINDOW_MINUTES` | `30` | SLO evaluation window. |
| `SLO_MIN_SAMPLES` | `20` | Minimum samples before an SLO is evaluated. |
| `SLO_BURN_RATE_THRESHOLD` | `14.4` | Error-budget burn-rate alert threshold. |
| `SLO_AVAILABILITY_TARGET_PCT` | `99.5` | Availability target. |
| `SLO_API_P95_MS` | `1500.0` | API p95 latency target. |
| `SLO_API_P99_MS` | `5000.0` | API p99 latency target. |
| `SLO_AI_P95_MS` | `30000.0` | AI-call p95 latency target. |
| `SLO_AI_SUCCESS_TARGET_PCT` | `95.0` | AI success-rate target. |
| `SLO_JOB_SUCCESS_TARGET_PCT` | `98.0` | Job success-rate target. |
| `SLO_QUEUE_WAIT_P95_MS` | `30000.0` | Queue-wait p95 target. |
