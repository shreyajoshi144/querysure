# QuerySure operations guide

Runbook for operating QuerySure: observability, readiness, AI reliability, SLOs and alerts (sections 1-7), billing and session support (8), LLM fallback, deployment (9) and backup/recovery (10). Measured load-test figures are summarised in [03_EVALUATION.md](03_EVALUATION.md#5-load-and-failure-testing-recorded-sprint-4).

## 1. Observability at a glance
| Concern | Where | Notes |
|---|---|---|
| Structured logs | `observability/logging_setup.py` | JSON lines on **stderr**; `LOG_FORMAT=text` for dev. Every line is PII-scrubbed. |
| Correlation | `observability/context.py` | `request_id` (accepts a caller `X-Request-ID` of 8-64 safe chars, else generated; echoed in the response), `user_ref`, `session_ref`, `job_id`. Refs are a truncated HMAC (`LOG_HASH_KEY`, defaults to a key derived from `JWT_SECRET_KEY`): stable for correlation, not reversible. |
| Metrics | `observability/store.py`, `metrics.py` | Per-minute aggregate buckets (counters + histograms), shared through Redis by web workers **and** the RQ worker; in-memory fallback if Redis is down. No per-user/per-request series. |
| Gauges | `observability/gauges.py` | DB-pool utilisation, breaker states, worker heartbeat (`qs:g:*`, short TTL: a dead process disappears). |
| Errors | `observability/errors.py` | Exception **type + fingerprint + code location** aggregated; messages are never stored. Optional Sentry via `SENTRY_DSN` (strict `before_send`, `pip install sentry-sdk`, not a hard dependency). |
| Events / incidents | `observability/events.py`, tables `ops_events`, `ops_incidents` | Allow-listed scalar detail only. Retention `OPS_EVENT_RETENTION_DAYS` (30). |
| Readiness | `GET /api/ready` | See section 3. `GET /api/health` stays a pure liveness probe. |
| Admin dashboard | `/ops` (page), `/api/admin/ops/*` (API) | Aggregates only. See section 2. |
| Prometheus | `GET /api/admin/ops/metrics` | `X-Ops-Token: $OPS_METRICS_TOKEN` or an admin bearer. Window-scoped counters + histogram buckets (cumulative), plus pool/queue/worker gauges. |

### What is logged / never logged
Logged: method, **route template** (never the raw path or query string), status, duration, ids as pseudonymous refs, provider names, token counts, cost, error type and `path:function:line` frames.
Never logged: prompts, questions, SQL, dataset names or contents, result rows, report text, e-mail addresses, tokens/keys, request/response bodies, exception messages (opt-in `LOG_EXCEPTION_MESSAGES=1`, still scrubbed, debugging only).
uvicorn's access log is filtered to drop client address and query string (verification/reset tokens travel in query strings).

## 2. Admin / ops dashboard (privacy contract)
* Access: verified account whose e-mail is in `ADMIN_EMAILS` (comma list; **empty by default = nobody**). Everyone else gets 403.
* Data: only the aggregate metric store, process gauges, queue/Redis/DB-pool counters, `ops_events`/`ops_incidents`, and error fingerprints. `ops_api.py` imports no user-content model, no storage, no job payloads (enforced by an AST test).
* Admin status grants **no** access to user-facing routes: sessions, datasets, SQL, results and reports stay owner-scoped.
* The page `/ops` is a static shell (no data, no inline script, `textContent` only); it calls the API with the signed-in user's bearer token.

## 3. Readiness: critical vs degraded
* **Critical -> 503 `not_ready`**: database (`SELECT 1` within `READINESS_DB_TIMEOUT_MS`), Redis (when configured / in production).
* **Degraded -> 200 `degraded`**: no live worker while jobs wait, queue backlog (`ALERT_QUEUE_DEPTH`, `ALERT_QUEUE_OLDEST_SECONDS`), object storage unreachable, all AI providers' circuits open / kill switch / global spend cap reached, DB-pool pressure (`ALERT_DB_POOL_UTILIZATION_PCT`), metrics store lost Redis.
* Why: draining the only web instance because a non-critical dependency is down turns a partial outage into a total one; those conditions alert instead.
* Public response: component names only. Non-critical probes are cached `READINESS_CACHE_SECONDS`.
* The database probe is bounded to `READINESS_DB_TIMEOUT_MS` **in total** (including connection-pool checkout) and uses at most one helper thread, so an exhausted pool yields a fast `503 not_ready` instead of a 10 s hang. Recorded during Sprint 4 (not re-run): first probe ~2 s, then ~35 ms.
* Alert tuning: keep `CIRCUIT_COOLDOWN_SECONDS` >= 2x `ALERT_EVAL_INTERVAL_SECONDS`, otherwise a circuit sampled in `half_open` can resolve its alert while the provider is still failing.

## 4. AI reliability
* Per-call SDK timeout `AI_PROVIDER_TIMEOUT_SECONDS` (25); one overall deadline per generate-sql request `AI_REQUEST_DEADLINE_SECONDS` (60) across fallbacks, JSON retry and repair rounds; no provider call starts with < `AI_MIN_CALL_BUDGET_SECONDS` left; per-attempt timeout is capped so the SDK's one retry still fits.
* Provider fallback chain + per-provider circuit breaker (Sprint 3), now with transition events (`circuit_opened` / `circuit_closed`) and failure-reason metrics (`timeout`, `rate_limited`, `auth`, `connection`, `server_error`, ... derived from exception type/status, never the message).
* Spend: per-user and global daily caps reserve budget **before** the provider call (Redis, DB fallback). If neither can reserve the request **fails closed** (Sprint 3 behaviour, kept). Provider failure refunds the reservation down to tokens actually spent; a failing settle can no longer turn a paid-for answer into a 500 (the counter stays high = fail-safe).
* Query slots: `/api/execute-sql` reserves a daily slot and refunds it in a `finally` unless the caller received a result (executor crash, failed query, session write conflict). A failing refund is logged, counted, evented, and never masks the original error.
* Background jobs: jobs stuck `queued/running/retrying` longer than `JOB_STALE_SECONDS` (1500, > the 15 min RQ timeout) are failed with `JOB_STALE` and release their upload quota; a reaped job is never resurrected if a worker later picks it up. Runs on the elected maintenance leader.

## 5. SLOs and alerts
Defaults (env-configurable, see `config.py` Sprint 4 block): availability 99.5 %, interactive API p95 1.5 s / p99 5 s (AI/upload/report routes excluded), AI success 95 %, AI p95 30 s, queue wait p95 30 s, job success 98 % (a user's invalid file is `rejected`, not a failure). Objectives with fewer than `SLO_MIN_SAMPLES` observations report `no_data`.
Percentiles are histogram-bucket estimates (bounds 5 ms ... 60 s); accuracy is the bucket width.
Alerts (evaluated by one elected process every `ALERT_EVAL_INTERVAL_SECONDS`; enable with `ALERTS_ENABLED=1`, on by default in production): database/Redis/storage unavailable, worker down, queue backlog, DB pool saturated, circuit open (per provider), all providers down, kill switch on, global budget high/exhausted, availability burn rate (short **and** long window >= `SLO_BURN_RATE_THRESHOLD`), any other SLO breach. One incident per alert key while open (partial unique index), resolved after `ALERT_RESOLVE_AFTER_SECONDS` of quiet. Delivery: `ALERT_WEBHOOK_URL` (https in production; JSON without user data), always also a log line + incident row.

### Runbook (first response)
| Alert | Look at | Action |
|---|---|---|
| `database_unavailable` | `/api/ready`, provider status page | Fail over / restore (section 10). Web stays 503 until DB answers. |
| `redis_unavailable` | `/api/ready` | App degrades to per-process limits and DB budget ledger; jobs cannot enqueue (503). Restore Redis. |
| `worker_down` / `queue_backlog` | `/ops` Queue panel | Restart/scale `python worker.py`; stale jobs auto-fail after 25 min and release quota. |
| `ai_circuit_open:<p>` | `/ops` AI providers | Provider outage; fallback chain is serving. Nothing to do unless `ai_all_providers_down`. |
| `ai_budget_exhausted` | `/ops` AI panel | Raise `GLOBAL_DAILY_AI_BUDGET_USD` after review, or wait for 00:00 UTC. |
| `db_pool_saturated` | `/ops` Database | Slow queries or too few connections: raise `DB_POOL_SIZE`/`DB_MAX_OVERFLOW` within Postgres `max_connections` (see section 6). |
| `slo_availability_burn` | `/ops` Routes, top errors | Find the 5xx route and fingerprint; roll back if a deploy correlates. |

## 6. Capacity notes
Each web process holds up to `DB_POOL_SIZE + DB_MAX_OVERFLOW` (5+5) Postgres connections; the RQ worker holds more per active job. Required Postgres `max_connections` >= (web processes x 10) + (workers x 10) + headroom. See `loadtests/README.md` and [03_EVALUATION.md](03_EVALUATION.md#5-load-and-failure-testing-recorded-sprint-4) for measured saturation.

## 7. Environment variables added in Sprint 4
`LOG_FORMAT`, `LOG_LEVEL`, `LOG_HASH_KEY`, `LOG_EXCEPTION_MESSAGES`, `METRICS_ENABLED`, `METRICS_RETENTION_SECONDS`, `METRICS_FLUSH_INTERVAL_SECONDS`, `GAUGE_TTL_SECONDS`, `ADMIN_EMAILS`, `OPS_METRICS_TOKEN`, `SENTRY_DSN`, `SENTRY_ENVIRONMENT`, `AI_PROVIDER_TIMEOUT_SECONDS`, `AI_REQUEST_DEADLINE_SECONDS`, `AI_MIN_CALL_BUDGET_SECONDS`, `LLM_PROVIDER_ORDER`, `LLM_TRANSIENT_RETRIES`, `LLM_RETRY_BACKOFF_SECONDS`, `LLM_MAX_ATTEMPTS_PER_REQUEST`, `JOB_STALE_SECONDS`, `WORKER_HEARTBEAT_*`, `READINESS_CACHE_SECONDS`, `READINESS_DB_TIMEOUT_MS`, `SLO_*`, `ALERTS_ENABLED`, `ALERT_*`, `OPS_EVENT_RETENTION_DAYS`.

## 8. Sprint 5 additions

**Scheduled maintenance.** `python scripts/maintenance.py` (Render cron `querysure-maintenance`, every 30 min) prints one JSON line
`{"downgraded_subscriptions", "expired_checkouts", "purged_sessions"}`; exit 1 + `{"error": ...}` on failure. If it stops running, paid accounts whose
renewal event never arrived keep Pro. Alert on cron failures at the platform level.

**Billing runbook.**
* Health: structured log events `billing_webhook` (field `outcome`) and `billing_webhook_failed`; error counter
  `qs_errors_window{component="billing",type="bad_signature|invalid_event|webhook_processing_failed"}` (a windowed counter; see section 1). A burst of `bad_signature` after a deploy usually means
  the provider and `PAYMENT_WEBHOOK_SECRETS` disagree.
* Stuck events: `SELECT provider, event_id, event_type, status, outcome, attempts, received_at FROM billing_events WHERE status IN ('failed','deferred') ORDER BY received_at;`
  They are retried when the provider redelivers; use the provider's "resend". `deferred` older than `BILLING_UNRESOLVED_RETRY_HOURS` become `ignored`.
* User says "I paid but I'm still Free": check `checkout_sessions` (status `pending`?), `billing_events` for that period, the provider's delivery log; the plan changes
  only on a verified `subscription.updated` with `status=active`. Do not edit `users.plan` by hand without also fixing the subscription row (maintenance would revert it).
* Rotate webhook secret: add the new secret to `PAYMENT_WEBHOOK_SECRETS` (comma-separated), deploy, switch it in the provider, remove the old one.
* Billing off switch: unset `PAYMENT_PROVIDER` and deploy. Checkout 503s, webhooks 404, existing plans are untouched.

**Sessions.** Revoke one account everywhere: the user's "Sign out everywhere", or `UPDATE auth_sessions SET revoked_at = now() WHERE user_id = '...' AND revoked_at IS NULL`
plus `UPDATE users SET auth_version = auth_version + 1 WHERE id = '...'`. Revoke everyone: rotate `JWT_SECRET_KEY`.

**Account deletion support.** The user self-serves (Settings -> Delete account). Common refusals: `SUBSCRIPTION_ACTIVE` (cancel in the provider portal first),
`JOBS_IN_PROGRESS` (wait), `DELETE_INCOMPLETE` (storage error; nothing deleted; retry). After a restore see [section 10](#10-backup-restore-and-recovery).

**Capacity.** Sprint 5 adds one indexed primary-key lookup (`auth_sessions`) to every authenticated request and a few small tables. **It has not been load-tested;**
the Sprint 4 figures in [03_EVALUATION.md](03_EVALUATION.md#5-load-and-failure-testing-recorded-sprint-4) predate it and must not be quoted as current capacity.

**New variables.** `PAYMENT_*`, `BILLING_*`, `AUTH_*`: see [ENVIRONMENT.md](ENVIRONMENT.md).


## LLM provider fallback (v1.0.3)

* Chain: `LLM_PROVIDER_ORDER` over every provider that has a key and a non-empty model id (`GROQ_MODEL`, `GROQ_FALLBACK_MODEL`, `OPENAI_FALLBACK_MODEL`, `ANTHROPIC_FALLBACK_MODEL`). Model ids are not assumed valid: a retired/unknown model yields `model_not_found` and the chain moves on. Check each id against your provider's model list.
* Every call logs one `llm_attempt` line (`provider`, `model`, `attempt`, `latency_ms`, `outcome`, `error_code`). No prompt, SQL, key or exception text.
* A request that succeeded after failures writes an `llm_fallback` ops event; a request where every provider failed writes `llm_all_providers_failed` (critical).
* Admin view: `/ops` page (cards "LLM fallback chain", "LLM failure reasons", "Fallback history", "LLM settings"), backed by `GET /api/admin/ops/providers`. Numbers come only from the real metrics store and events table; empty means nothing happened yet.
* Admin setup: set `ADMIN_EMAILS` to your address on the web service, sign up with that address, verify the e-mail (unverified accounts are refused), then open `/ops`.

## 9. Deployment

Variables are listed in [ENVIRONMENT.md](ENVIRONMENT.md). In `ENVIRONMENT=production` the process **refuses to start** if a
required setting is missing (that file lists every guard).

### Render (blueprint: `render.yaml`)

Defines `querysure-api` (web, free), `querysure-worker` (background worker, **paid `starter`**: Render has no free worker plan),
`querysure-maintenance` (cron, `starter`), `querysure-db` (Postgres) and `querysure-redis` (Key Value, `noeviction`, private
network only). The worker is required: it sends every verification / password-reset e-mail and ingests uploads. It is also where
SMTP runs, so the web service never needs outbound mail ports.

#### Exact steps

1. **Repository.** Unzip, `git init`, commit, push to a *private* GitHub/GitLab repo (`.gitignore` already excludes `.env`, `*.db`,
   `uploads/`, `reports/`, `dump.rdb`, `node_modules/`). Render reads `render.yaml` from the repo root.
2. **S3 bucket** (private): AWS S3 or Cloudflare R2. Create an access key limited to that bucket (get/put/delete/list).
   AWS: `S3_REGION=<region>`; R2: `S3_ENDPOINT_URL=https://<account>.r2.cloudflarestorage.com`, `S3_REGION=auto`.
3. **SMTP account** (Gmail app password, SES, Postmark ...): host, port 587, username, password, and a verified `EMAIL_FROM`.
4. **Google OAuth client (optional; skip it and Google sign-in is simply hidden)**: Google Cloud Console -> APIs & Services -> Credentials -> *OAuth client ID* -> *Web application*.
   Add **Authorized JavaScript origins** = your exact app origin (`https://<service>.onrender.com`, plus your custom domain).
   No redirect URI and no client secret are used (Google Identity Services ID-token flow). Copy only the Client ID.
5. **Pick the public URL first.** Service names are global on `onrender.com`; if `querysure-api` is taken Render adds a suffix.
   Rename the service in `render.yaml` to something unique so the URL is predictable: `APP_BASE_URL` and
   `CORS_ALLOWED_ORIGINS` must equal it exactly (`https://...`, no trailing slash, no path).
6. **Render dashboard -> New -> Blueprint** -> select the repo -> Apply. Fill every prompted `sync: false` variable:
   `APP_BASE_URL`, `CORS_ALLOWED_ORIGINS`, `TRUSTED_PROXY_IPS` (see below), `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`,
   `SMTP_PASSWORD`, `EMAIL_FROM`, `GOOGLE_CLIENT_ID` (optional), `S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `S3_REGION`,
   `S3_ENDPOINT_URL` (non-AWS only), `GROQ_API_KEY` (models default to `openai/gpt-oss-120b` / `openai/gpt-oss-20b`; override with `GROQ_MODEL` / `GROQ_FALLBACK_MODEL`) (and/or `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` if you add them),
   `ADMIN_EMAILS` (optional), `ALERT_WEBHOOK_URL` (optional, https). Leave every `PAYMENT_*` variable blank for Free-only.
   `DATABASE_URL`, `REDIS_URL` and `JWT_SECRET_KEY` are wired automatically (the worker/cron reuse the web service's JWT secret).
   The prompts repeat per service: enter the same values for web, worker and cron (they all import the same config).
7. **Deploy order is handled by Render.** The web start command runs `alembic upgrade head` *once, before* uvicorn starts, so
   multiple uvicorn workers never race on schema creation; a failed migration fails the deploy instead of serving a stale schema.
8. **Smoke test** (below). Then, in Google Cloud Console, confirm the origin you added is the one actually serving the app.

#### `TRUSTED_PROXY_IPS`

Uvicorn only believes `X-Forwarded-For` from these peers; the rate limiter keys on the resulting client IP. Too narrow: every
visitor shares the load balancer's IP and the 5/min login limit becomes a global limit. Too wide (`*`): a client can spoof its IP
and evade limits. Never use `*`. Render's load balancer reaches your service over the private network, so start with
`10.0.0.0/8`, then **verify**: from one network (e.g. phone data) send 6 wrong logins until you get HTTP 429, then immediately
try one login from a different network. If the second network is also rate-limited, the CIDR does not cover Render's proxy:
widen it to the range your service logs show for peer addresses.

#### Free-plan caveats

* Free Postgres is deleted after 30 days; use a paid database plan before real users arrive (change `plan:` under `databases:`).
* Free Key Value is not persistent: a restart empties queued jobs (they are reaped as `JOB_STALE` and the user retries) and resets
  rate-limit counters. Use a paid Key Value plan if that is unacceptable.
* The free web service sleeps when idle; the first request after sleep is slow.

### Docker

`docker build .` produces a non-root image (UID 10001). It copies every top-level module (glob), `observability/`, `scripts/`,
`modules/`, `migrations/` and `static/` (with built assets). In production the container runs `alembic upgrade head` before
starting; `HEALTHCHECK` probes `/api/health`.

`docker-compose.yml` is a **development** stack (Postgres, Redis, API, worker; `ENVIRONMENT: development`, wildcard CORS). It is not a
production recipe. The `worker` service is required for e-mail and report jobs (dev e-mail is written inside it:
`docker compose exec worker sh -c 'cat .dev_mail/*.eml'`). The worker serves no HTTP, so Compose overrides the image
`HEALTHCHECK` for it: the worker is healthy while its Redis heartbeat key `qs:g:worker:<hostname>:<pid>` (30 s TTL, written by
`observability/gauges.py`) exists. In development the API creates the schema with `create_all()` under a
Postgres advisory lock, so several uvicorn workers can start at once; production always uses Alembic.

### Migrations

* Add-only for Sprint 5 (`5a1e7c3b9d20`): two columns on `subscriptions` and four tables; safe against a live database; the
  previous app version ignores them. Pre-existing subscribers are unchanged (verified on Postgres with seeded data).
* Check drift: `alembic check` must say "No new upgrade operations detected".
* Roll back application code only with the schema in place; the downgrade drops the Sprint 5 tables (billing ledger, payments,
  auth sessions), so take a backup first.

### First-deploy consequences

* With `AUTH_REQUIRE_SESSION_ID` on (production default) every existing token lacks `sid` and is refused: **all users sign in once**.
* Subscriptions already in the database keep working; entitlement now also depends on `current_period_end + BILLING_GRACE_HOURS`
  when a period end is stored, and `maintenance.py` will downgrade lapsed ones.

### Smoke test after deploy

`/api/health` (liveness), `/api/ready` (database + Redis), load `/`, register + verify + sign in, sample dataset query,
report download, `POST /api/billing/checkout` (expects `503 BILLING_NOT_CONFIGURED` if billing is off), and an admin visit to `/ops`.

### Frontend build

`python scripts/build_static.py` writes `static/dist/` (`rjsmin`/`rcssmin`, whitespace and comments only). Without it the server
serves the sources with `?v=<content hash>`. Static responses carry `Cache-Control` (`immutable` for hashed URLs, `no-cache` otherwise).

## 10. Backup, restore and recovery

Everything here is executable and was exercised against a real Postgres 16 (see "Verified" below).

### Postgres
**Backups.** Managed Postgres: enable provider automated backups + point-in-time recovery (PITR) and keep >= 7 days; that is the primary control (RPO ~ minutes with WAL archiving). Add the logical dump below as an independent, portable second copy (daily; keep 14-30 days; store off the database host, encrypted).
```
DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/querysure scripts/pg_backup.sh /var/backups/querysure
```
`pg_dump --format=custom`, writes a `.sha256`, then proves the archive is readable (`pg_restore --list`). Non-zero exit = do not trust it.

**Restore drill (run monthly, and after any migration that rewrites data).** Never restore over production first:
```
DATABASE_URL=<source> RESTORE_ADMIN_URL=postgresql://admin:pw@host:5432/postgres scripts/pg_restore_verify.sh <dump>
```
Restores into a throw-away database, then checks: checksum, restore without errors (`--exit-on-error`), every public table has the **same row count** as the source, `alembic_version` matches, `alembic check` reports no schema drift. Scratch DB is dropped on exit. Use `REQUIRE_SOURCE_MATCH=0` when verifying an old dump against a source that has moved on (integrity + schema only).

**Real recovery.** (1) Stop web + worker (or set `AI_KILL_SWITCH=1` and scale web to 0). (2) Create an empty database, `pg_restore --no-owner --no-privileges --dbname=<new> <dump>`. (3) `alembic upgrade head` (no-op if the dump is current; applies newer migrations if it is older). (4) Point `DATABASE_URL` at it, start web, check `/api/ready`, then start workers. (5) Redis holds no source-of-truth data: counters/limits rebuild; in-flight jobs older than `JOB_STALE_SECONDS` are auto-failed and their quota released, so users simply retry uploads.
RPO = time since the last backup (daily dump) or ~minutes with PITR. RTO is dominated by dump size; measured below.

### S3 (datasets, reports, upload staging)
Layout: `staging/<user>/<uuid>[.ext]` (upload staging), `<S3_PREFIX>/<user>/<uuid>.db` (datasets), `<S3_PREFIX>/reports/<user>/<uuid>` (reports).
```
S3_BUCKET=... python scripts/s3_lifecycle.py plan|apply|verify
```
* **Versioning enabled** -> an overwritten or deleted object is recoverable. Recover an overwrite: copy the old `VersionId` onto the key (`copy_object` with `CopySource.VersionId`). Recover a delete: delete the **delete marker** version. Both are exercised in `tests/test_sprint4_backup_recovery.py`.
* **Lifecycle**: `staging/` expires after 1 day (backstop for crashed jobs; normal flow deletes right after ingestion); incomplete multipart uploads abort after 7 days; noncurrent versions of datasets/reports expire after 30 days (so recovery window = 30 days, and deleted-user data physically leaves after that). **Current user data is never auto-expired** (verified by `verify`).
* Also enable: bucket default encryption (SSE-S3/KMS), Block Public Access, and for disaster recovery cross-region replication of the bucket (requires versioning; not configured by the script because it needs an IAM role and destination bucket).
* Datasets are re-derivable only from user uploads: without versioning/replication an S3 loss is permanent data loss. Reports are regenerable but cost AI spend.
* **Account deletion vs backups.** A restored dump resurrects accounts deleted after it was taken. The app logs an `account_deleted` event
  carrying `user_ref` (a keyed hash of the user id, `LOG_HASH_KEY`) for every deletion. Collect those refs from your log store for the period since
  the dump (one per line in a file) and run `python scripts/reapply_deletions.py refs.txt --dry-run`, then without `--dry-run`. It deletes exactly the
  restored users whose ref matches, through the normal fail-closed path, and exits 1 if any could not be deleted (live subscription, running
  jobs, storage error). **Keep `LOG_HASH_KEY` stable** and retain these log events at least as long as your backup retention; if the key is lost the
  refs cannot be matched. Deleted users' objects leave S3 immediately; only noncurrent object versions linger for the 30-day lifecycle window.
* **Billing after a restore.** The `billing_events` ledger is restored with the database, so replays of already-processed events stay no-ops; events
  the provider delivered *after* the dump are lost from the ledger and will be re-processed if the provider redelivers (safe: processing is idempotent
  and order-safe). Ask the provider to resend events since the dump timestamp, then run `python scripts/maintenance.py`.
* **Sessions after a restore.** `auth_sessions` is restored too; tokens revoked after the dump become valid again until they expire (max
  `ACCESS_TOKEN_EXPIRE_MINUTES`). If that matters (e.g. after a compromise), run "sign out everywhere" for affected accounts or rotate `JWT_SECRET_KEY`,
  which invalidates every token.

### Recorded verification (Sprint 4 sandbox, Postgres 16.13; not re-run)
Measured sizes/times are recorded in [03_EVALUATION.md](03_EVALUATION.md#5-load-and-failure-testing-recorded-sprint-4). Negative checks: a corrupted archive is rejected by the checksum step; a source that changed after the dump is reported as `MISMATCH table=... source=301 restored=300` and exits 1.
