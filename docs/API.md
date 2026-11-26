# API reference

Base path `/api`. Generated from the application's route table, then annotated. All request/response bodies are JSON unless
noted. Errors are `{"success": false, "error": "...", "error_code": "..."}` (some older routes return `{"detail": ...}`).
Authentication is **`Authorization: Bearer <access token>`** only: the API sets and reads no cookies, so there is no ambient
credential for CSRF to ride on.

**Auth column:** `public` = no token; `user` = valid, unrevoked access token; `admin` = user whose e-mail is in `ADMIN_EMAILS`.

## Conventions

* **Tokens.** `POST /api/auth/login` (or Google sign-in / change-password) returns `{"access_token", "token_type": "bearer"}`.
  The token is a JWT carrying `sub`, `exp`, `ver` (account auth version) and `sid` (server-side session id). It is honoured only
  while its `auth_sessions` row exists, is not revoked and has not expired, and `ver` matches the account.
* **Sign-out.** Use `POST /api/auth/sign-out` (revokes this token) or `POST /api/auth/logout-all` (revokes every token).
  `POST /api/auth/logout` is the legacy client-side sign-out and does **not** revoke anything.
* **Rate limits.** Per-route limits are enforced (e.g. login 5/min, export 3/hour, delete 5/hour, checkout 10/min, webhook
  300/min). Exceeding one returns `429` with `error_code: RATE_LIMITED` and `Retry-After`.
* **Daily query quota.** `POST /api/execute-sql` returns `429` with `code: QUERY_LIMIT_REACHED`, `plan`, `daily_limit`,
  `used_today`, `remaining` when the plan's daily limit is used up. This is distinct from rate limiting; it resets at 00:00 UTC.
* **Request id.** Every response carries `X-Request-ID`; quote it when reporting a problem.

### Authentication & sessions

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/auth/change-password` | user |  |
| POST | `/api/auth/forgot-password` | public |  |
| POST | `/api/auth/google` | public |  |
| GET | `/api/auth/google/config` | public |  |
| POST | `/api/auth/google/link` | user |  |
| POST | `/api/auth/login` | public |  |
| POST | `/api/auth/logout` | user | Legacy client-side sign-out (kept for API compatibility): the token is NOT revoked server-side. |
| POST | `/api/auth/logout-all` | user | Sign out every device, this one included (all tokens of the account are revoked). |
| GET | `/api/auth/me` | user |  |
| GET | `/api/auth/methods` | user | Sign-in methods on the account: `google_linked`, `has_password`. |
| POST | `/api/auth/register` | public |  |
| POST | `/api/auth/resend-verification` | public |  |
| POST | `/api/auth/reset-password` | public |  |
| POST | `/api/auth/sign-out` | user | Revoke THIS token server-side: it stops working immediately, not merely when the client forgets it. |
| POST | `/api/auth/verify-email` | public |  |

### Account (privacy)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/account/delete` | user | Permanently delete the account. Requires re-authentication (password, or a fresh Google credential for accounts |
| GET | `/api/account/export` | user | Download a JSON copy of the caller's own data (no credentials, storage keys or other users' data). |
| GET | `/api/account/usage` | user |  |

### Billing

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/billing` | user | Return the authenticated user's read-only billing state. |
| POST | `/api/billing/checkout` | user | Start a hosted checkout. This NEVER changes the plan: the plan changes only when the provider's signed webhook says |
| POST | `/api/billing/webhook/{provider_name}` | public | Provider -> QuerySure. Authenticated ONLY by the provider's signature over the raw body (no bearer token, no cookies, |

### Admin / operations

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/admin/ops/alerts/evaluate` | admin | Run one alert reconciliation now (operator action, e.g. after fixing something). Names only in the response. |
| GET | `/api/admin/ops/errors` | admin |  |
| GET | `/api/admin/ops/providers` | admin | LLM fallback chain (provider + model, no keys), breaker states, per-provider metrics, fallback history, AI incidents. |
| GET | `/api/admin/ops/events` | admin |  |
| GET | `/api/admin/ops/incidents` | admin |  |
| GET | `/api/admin/ops/metrics` | ops token or admin | Prometheus text format. Auth: ``X-Ops-Token`` == OPS_METRICS_TOKEN, or an admin bearer token. |
| GET | `/api/admin/ops/slo` | admin |  |
| GET | `/api/admin/ops/summary` | admin |  |

### Workspaces, datasets & audit (Sprint 1)

Roles rank `viewer < analyst < admin < owner`. A non-member always gets `404 WORKSPACE_NOT_FOUND` (existence is not
revealed); an insufficient role gets `403 FORBIDDEN`. Responses never contain storage references.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/workspaces` | user | The caller's workspaces with their role, member and dataset counts, plus `personal_workspace_id`. |
| POST | `/api/workspaces` | user | Create a team workspace (`{"name"}`); the caller becomes owner. At most 10 owned team workspaces. |
| GET | `/api/workspaces/{workspace_id}` | user | One workspace (viewer+). |
| PATCH | `/api/workspaces/{workspace_id}` | user | Rename (`{"name"}`, admin+). |
| DELETE | `/api/workspaces/{workspace_id}` | user | Delete a team workspace (owner). Datasets move to their uploaders' personal workspaces; members and the workspace's audit trail are removed. |
| GET | `/api/workspaces/{workspace_id}/members` | user | Members with e-mail and role (viewer+). |
| POST | `/api/workspaces/{workspace_id}/members` | user | Add an existing verified account (`{"email", "role"}`, admin+; only the owner can grant `admin`). No e-mail is sent. |
| PATCH | `/api/workspaces/{workspace_id}/members/{user_id}` | user | Change a role. The owner manages admins; admins manage analysts and viewers; nobody changes their own or the owner's role. |
| DELETE | `/api/workspaces/{workspace_id}/members/{user_id}` | user | Remove a member (same rules) or leave (any non-owner, with your own id). Their datasets here move to their personal workspace. |
| GET | `/api/workspaces/{workspace_id}/datasets` | user | Dataset catalog (viewer+): name, format, tables, rows, bytes, creator, `can_delete`. |
| DELETE | `/api/datasets/{dataset_id}` | user | Delete a dataset: its creator (analyst+) or an admin. Removes the stored file first (`503 DELETE_INCOMPLETE` and no change if storage refuses), then empties every session using it. |
| GET | `/api/workspaces/{workspace_id}/audit` | user | Audit trail, newest first (admin+). `limit` ≤ 100; pass `next_cursor` as `before` for the next page. |

### Connectors, imports and runs (Sprint 3)

Datasets are workspace-owned: viewer+ can open and query, analyst+ can create and re-import, the creator or an admin can delete.
A non-member gets `404 DATASET_NOT_FOUND`. Imports run as background jobs (poll `GET /api/jobs/{job_id}`).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/connectors` | user | The registered connectors and what they support. All are file connectors: full refresh only, no credentials. |
| POST | `/api/datasets` | user | Multipart `file` (+ optional `workspace_id`, analyst+): import a file as a new dataset without a session. `202` + `job_id`. |
| POST | `/api/datasets/{dataset_id}/runs` | user | Multipart `file` (analyst+): import a new version. `409 RUN_IN_PROGRESS` while another run is active; an identical file records a `skipped` run. |
| GET | `/api/datasets/{dataset_id}/runs` | user | Run history (viewer+): status, records read / inserted / updated / skipped / rejected, duplicates kept, reject reasons, per-table counts, schema changes. `error_detail` is analyst+ only. |
| GET | `/api/datasets/{dataset_id}/runs/{run_id}` | user | One run (viewer+). |
| GET | `/api/datasets/{dataset_id}/quarantine` | user | Reason codes and row positions rejected by the current version (analyst+, first 50). Row values are not stored. |
| POST | `/api/datasets/{dataset_id}/open` | user | Make the dataset the dataset of one of the caller's sessions (`{"session_id"}`, viewer+). Same response shape as the upload job result. |

### Database connections (Sprint 4)

admin+ creates, edits, enables / disables and deletes connections; analyst+ lists, tests, browses and syncs. Responses never contain a password
(`has_password` only). Errors are codes (`AUTH_FAILED`, `CONNECT_FAILED`, `HOST_NOT_ALLOWED`, `TLS_FAILED`, `DATABASE_NOT_FOUND`, `QUERY_TIMEOUT`, ...).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/connections` | user | Connections of a workspace (`?workspace_id`, analyst+) and whether encryption is configured. |
| POST | `/api/connections` | user | Create (`{workspace_id, name, connector: postgres|mysql, config: {host, port, database, user, ssl_mode}, password}`, admin+). `503 CONNECTIONS_NOT_CONFIGURED` without an encryption key. |
| GET | `/api/connections/{connection_id}` | user | One connection (analyst+). |
| PATCH | `/api/connections/{connection_id}` | user | Rename or edit (admin+). Changing host, port or user needs `password` again (`422 PASSWORD_REQUIRED`). |
| DELETE | `/api/connections/{connection_id}` | user | Delete it and its secret (admin+); its datasets keep their data and stop syncing. |
| POST | `/api/connections/{connection_id}/disable` | user | Disable (admin+). |
| POST | `/api/connections/{connection_id}/enable` | user | Enable (admin+). |
| POST | `/api/connections/{connection_id}/test` | user | Connect and report `{ok, error_code, message}` (analyst+). |
| GET | `/api/connections/{connection_id}/schema` | user | Tables, primary keys and columns, with which tables are incremental-ready (analyst+). |
| POST | `/api/connections/{connection_id}/datasets` | user | Import one table as a dataset (`{table, mode: full_refresh|incremental, watermark_column, name}`, analyst+). Incremental needs a primary key and an integer or timestamp watermark; its first run is the backfill. `202` + `job_id`. |
| POST | `/api/datasets/{dataset_id}/sync` | user | Sync a connection dataset (`{mode}`, analyst+). `409 RUN_IN_PROGRESS` while a run is active. The job result reports `mode`, `fallback_reason` and `records_inserted / updated / skipped / rejected`. |
| GET | `/api/datasets/{dataset_id}/source` | user | Source summary and checkpoint (viewer+). No host, user or secret. |

### REST and S3 sources, schedules (Sprint 5)

`POST /api/connections` also accepts `connector: rest` and `connector: s3` (the `config` carries `base_url`, `auth` and `resources` for REST;
`bucket`, `access_key_id`, optional `region` / `endpoint_url` and `resources` for S3; the token or secret access key goes in `password`).
A resource is what you import (`table` in the import call). Changing where a connection points or who it signs in as needs the password again.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/datasets/{dataset_id}/schedule` | user | The dataset's sync schedule or `null` (viewer+), the minimum interval and whether an in-process scheduler is enabled. |
| PUT | `/api/datasets/{dataset_id}/schedule` | user | Sync every `interval_minutes` (`{interval_minutes, mode, enabled}`, analyst+, connection datasets only). The first run is one interval away; runs act as the caller. |
| DELETE | `/api/datasets/{dataset_id}/schedule` | user | Remove the schedule (analyst+). |

### Knowledge hub (Sprint 7)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/workspaces/{workspace_id}/knowledge` | user | Documents of the workspace and the upload limits (viewer+). |
| POST | `/api/workspaces/{workspace_id}/knowledge` | user | Upload a `.txt`, `.md`, `.pdf` or `.docx` file (multipart `file`, analyst+). Indexed synchronously; the same bytes twice give 409. |
| DELETE | `/api/knowledge/{document_id}` | user | Delete a document and its passages (admin, or the uploader at analyst+). |
| GET | `/api/workspaces/{workspace_id}/knowledge/search` | user | Best-matching passages for `q` (viewer+, no model). |
| POST | `/api/workspaces/{workspace_id}/knowledge/ask` | user | Cited answer from retrieved passages (`{question, document_ids?}`, viewer+). No relevant passage: `answer: null`, no model call. An answer citing nothing valid is withheld. Uses the shared AI budget. |

### Dashboard (Sprint 12)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/workspaces/{workspace_id}/overview` | user | One read-only summary (viewer+): dataset status and quality counts with the ones needing attention, recent imports, sync schedules (incl. last failed), document and passage counts, your conversation count, agent steps awaiting approval (yours; all for admin+), recent investigations, and 7-day AI usage (admin+ only). |

### Governance (Sprint 11)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/workspaces/{workspace_id}/ai-usage` | user | AI usage report of the workspace for the last `days` (default 30, max 365): calls, failures, tokens, estimated cost, p50/p95 latency per task, per provider and per day, top error categories, and counts of AI actions from the audit log (admin+; no prompt or answer text). |

### Investigations (Sprint 10)

Facts are computed by read-only SQL on the dataset; hypotheses are rule-based and labelled `unverified`; recommendations and limitations are separate sections; document excerpts are untrusted context. Same dataset version and parameters give the same facts.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/datasets/{dataset_id}/investigation-options` | user | Tables and guessed column roles (date / number / category / text), viewer+. |
| POST | `/api/datasets/{dataset_id}/investigations` | user | Run `trend`, `comparison` or `breakdown` (`{kind, table, measure?, date_column?, dimension?, agg?, split_at?}`, analyst+). Stores the report and JSON/HTML exports; 422 with a code for bad parameters. |
| GET | `/api/workspaces/{workspace_id}/investigations` | user | Investigations of the workspace (`dataset_id` filter), viewer+. |
| GET | `/api/investigations/{investigation_id}` | user | The full structured report (viewer+). Exports download through `GET /api/reports/{id}/download` (creator only). |
| DELETE | `/api/investigations/{investigation_id}` | user | Delete it and its exports (creator at analyst+, or admin). |

### Controlled agent (Sprint 9)

A run is a goal, a validated plan of registered tools, and its outcome. Read steps run at once; write steps wait for approval. The creator and workspace admin+ can read a run (others get 404).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/agent/tools` | user | The tool registry: name, risk (`read`/`write`), minimum role, arguments. |
| GET | `/api/workspaces/{workspace_id}/agent/runs` | user | Your runs (admins: everyone's), newest first. |
| POST | `/api/workspaces/{workspace_id}/agent/runs` | user | Plan and start a run (`{goal}`, analyst+). 502 `AGENT_PLAN_FAILED` when the model's plan is invalid after one retry; nothing is stored then. |
| GET | `/api/agent/runs/{run_id}` | user | The run with its steps, arguments and results. |
| POST | `/api/agent/runs/{run_id}/steps/{step_id}/approve` | user | Approve a waiting write step (needs the tool's role); runs the stored arguments and continues. 409 `APPROVAL_EXPIRED` / `NOT_AWAITING_APPROVAL`. |
| POST | `/api/agent/runs/{run_id}/steps/{step_id}/reject` | user | Reject a waiting step; the remaining steps are skipped. |
| POST | `/api/agent/runs/{run_id}/cancel` | user | Cancel a run that is still waiting. |

### Assistant conversations (Sprint 8)

Chats are private to their creator (anyone else, including a workspace owner, gets 404). Membership (viewer+) is re-checked on every call.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/workspaces/{workspace_id}/conversations` | user | Your conversations in the workspace, newest first. |
| POST | `/api/workspaces/{workspace_id}/conversations` | user | Start a conversation (`{title?}`); 409 `CONVERSATION_LIMIT` at the per-user cap. |
| GET | `/api/conversations/{conversation_id}` | user | The conversation and its messages (answers keep their cited sources). |
| PATCH | `/api/conversations/{conversation_id}` | user | Rename (`{title}`). |
| DELETE | `/api/conversations/{conversation_id}` | user | Delete the conversation and its messages. |
| POST | `/api/conversations/{conversation_id}/messages` | user | Send `{question}`; returns the stored user and assistant messages. Answers come from the workspace's documents with validated citations and follow-up context; no relevant passage means no model call (`intent: data_question` points to the Query page). 409 `CONVERSATION_FULL` at the message cap. Shared AI budget. |

### Data quality (Sprint 6)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/datasets/{dataset_id}/quality` | user | Latest data-quality report (profile and findings) plus a short history (viewer+; `limit` 1-50). `latest` is `null` before the first successful import. |

### Product pipeline, sessions, reports & health

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/ai-usage` | user | Read-only, owner-scoped view of the caller's most recent AI invocations. Additive: the |
| GET | `/api/ai/traces/{trace_id}` | user | Every provider attempt behind one of the caller's AI operations (task, provider, model, outcome, error code, retry index, latency, tokens, cost) plus a summary. `trace_id` comes from `/api/generate-sql`. Another user's trace is `404 TRACE_NOT_FOUND`. |
| POST | `/api/analyze-reliability` | user |  |
| GET | `/api/history` | user | Query history of the current user (`q`, `limit`≤100, `offset`); owner-scoped. |
| GET | `/api/history/{entry_id}` | user | One saved entry (question, SQL, saved result); 404 for other users' entries. |
| DELETE | `/api/history/{entry_id}` | user | Delete one entry. |
| POST | `/api/history/clear` | user | Delete all of the user's history. |
| GET | `/api/answer-trace` | user | Return sanitized provenance from the authenticated session only. |
| GET | `/api/dataset/preview` | user | Dataset Explorer: one page of one profiled table (default 10 rows; optional `page` and `page_size`, capped at 50; owner-scoped, read-only, uses no query quota). Response adds `page`, `page_size`, `total_pages`. |
| POST | `/api/connect/sample` | user |  |
| POST | `/api/connect/upload` | user | Accepts .csv, .json, .xlsx, .xlsm, .parquet, .db, .sqlite, .sqlite3. Multipart `file` + `session_id`; optional `workspace_id` catalogs the dataset in that workspace (analyst or above; personal workspace when omitted). Returns `202` + `job_id`; the job result adds `dataset_id` and `workspace_id`. |
| GET | `/api/evaluation/product-summary` | public |  |
| POST | `/api/execute-sql` | user |  |
| POST | `/api/generate-insights` | user |  |
| POST | `/api/generate-report` | user |  |
| POST | `/api/generate-sql` | user | Generate SQL for a question. The response includes `trace_id` for `GET /api/ai/traces/{trace_id}`. |
| GET | `/api/health` | public |  |
| GET | `/api/jobs/{job_id}` | user |  |
| GET | `/api/product-info` | public |  |
| GET | `/api/provider-status` | user | Tells the frontend whether an LLM provider is already configured |
| GET | `/api/ready` | public | Readiness: CRITICAL dependencies (database, Redis) decide 200 vs 503; non-critical ones (worker, queue, object storage, |
| GET | `/api/reports/{report_id}/download` | user | Owner-only download of a stored report artifact (streamed through the API; the object key and storage |
| POST | `/api/retrieve-tables` | user |  |
| POST | `/api/schema-intelligence` | user |  |
| POST | `/api/semantic-layer` | user |  |
| POST | `/api/session` | user |  |
| POST | `/api/session/flush` | user | Clear the active session completely without creating a stale successor. |
| DELETE | `/api/session/{session_id}` | user | Delete one of the caller's sessions and release its dataset (only if no other session shares it). |
| GET | `/api/sessions` | user | Owner-scoped list of the caller's sessions plus quota usage, so a client can free space when it hits a |
| GET | `/api/usage` | user |  |
| POST | `/api/validate-sql` | user |  |

## Billing details

* `GET /api/billing` returns billing **state only** (never a way to change it): `status`, `plan`, `provider`,
  `current_period_start/end`, `cancel_at_period_end`, `billing_enabled`, `purchasable_plans`, `portal_url`.
  `POST /api/billing` does not exist (405) by design.
* `POST /api/billing/checkout` body `{"plan": "pro"}` -> `{"checkout_id", "checkout_url", "plan", "expires_at"}`.
  Errors: `503 BILLING_NOT_CONFIGURED`, `422 PLAN_NOT_PURCHASABLE` (anything except `pro`), `409 ALREADY_SUBSCRIBED`,
  `502 BILLING_PROVIDER_ERROR`. It never changes the plan.
* `POST /api/billing/webhook/{provider_name}` is called by the payment provider. Authenticated by signature over the raw body.
  `400` bad/stale signature or malformed event, `404` provider disabled/not the configured one, `413` body over
  `BILLING_WEBHOOK_MAX_BYTES`, `503` + `Retry-After` for an event that cannot be tied to an account yet (provider should retry),
  `500` on an internal error (provider should retry), `200 {"received": true, "outcome": ...}` otherwise where outcome is one of
  `processed | duplicate | ignored | stale | rejected`. Full contract: [BILLING.md](BILLING.md).

## Account details

* `GET /api/account/export` -> `application/json` attachment (`Cache-Control: private, no-store`): account, billing state and
  payments, daily usage, sessions (id, timestamps, question, SQL, dataset size), report metadata, AI invocation metadata. It
  never contains password hashes, token hashes, storage keys, dataset contents, query result rows, or other users' data.
* `POST /api/account/delete` body `{"confirm_email", "password"}` (or `google_credential` for accounts that have no password).
  `409 SUBSCRIPTION_ACTIVE` (cancel with the provider first), `409 JOBS_IN_PROGRESS`, `503 DELETE_INCOMPLETE` (storage
  cleanup failed; nothing was deleted, retry), `401 REAUTH_FAILED`, `422 CONFIRMATION_MISMATCH`, `200 {"deleted": true}`.
  Details: [02_ARCHITECTURE.md](02_ARCHITECTURE.md#data-privacy-and-account-deletion).
