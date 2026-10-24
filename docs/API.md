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

### Product pipeline, sessions, reports & health

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/ai-usage` | user | Read-only, owner-scoped view of the caller's most recent AI invocations. Additive: the |
| POST | `/api/analyze-reliability` | user |  |
| GET | `/api/history` | user | Query history of the current user (`q`, `limit`≤100, `offset`); owner-scoped. |
| GET | `/api/history/{entry_id}` | user | One saved entry (question, SQL, saved result); 404 for other users' entries. |
| DELETE | `/api/history/{entry_id}` | user | Delete one entry. |
| POST | `/api/history/clear` | user | Delete all of the user's history. |
| GET | `/api/answer-trace` | user | Return sanitized provenance from the authenticated session only. |
| GET | `/api/dataset/preview` | user | Dataset Explorer: one page of one profiled table (default 10 rows; optional `page` and `page_size`, capped at 50; owner-scoped, read-only, uses no query quota). Response adds `page`, `page_size`, `total_pages`. |
| POST | `/api/connect/sample` | user |  |
| POST | `/api/connect/upload` | user |  |
| GET | `/api/evaluation/product-summary` | public |  |
| POST | `/api/execute-sql` | user |  |
| POST | `/api/generate-insights` | user |  |
| POST | `/api/generate-report` | user |  |
| POST | `/api/generate-sql` | user |  |
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
