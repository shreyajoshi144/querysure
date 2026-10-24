# Billing

QuerySure sells one plan online: **Pro**. Billing is **provider-neutral and off by default**. No payment company is
hard-coded, and **no real payment-provider integration ships with this repository**. What ships is the boundary
(`payment_provider.py`), a vendor-neutral reference adapter (`generic`), and the idempotent webhook/entitlement engine
(`billing_service.py`). A real launch needs an adapter for your provider, tested against that provider's sandbox (see
"Adding a provider").

## What is and is not supported

| | |
|---|---|
| Plans purchasable online | **Pro only** (`PURCHASABLE_PLANS = {"pro"}`) |
| Team, Business | Entitlement tiers only. There is **no organization, seat or shared-workspace model**, so Team cannot be bought and no webhook can grant it (rejected as `plan_not_purchasable`). Business is arranged manually (`PRODUCT_CONTACT_EMAIL`). |
| Checkout | Hosted by the provider; QuerySure creates a server-side session and redirects. No card data ever touches QuerySure. |
| Cancel / change payment method | In the provider's customer portal (`PAYMENT_PORTAL_URL`). QuerySure has no cancel button; the `generic` adapter cannot cancel via API. |
| Refunds, proration, taxes, invoices, dunning e-mails | **Not implemented.** Refunds are recorded in the payments ledger but do not revoke access by themselves; the provider must also send a subscription event. |
| Trials, coupons, multiple currencies in UI | Not implemented. |

With `PAYMENT_PROVIDER` unset: `POST /api/billing/checkout` -> `503 BILLING_NOT_CONFIGURED`, the webhook route 404s, the UI shows
"Payments are not enabled on this deployment", and every account stays on the plan it has.

## Trust model

* The browser never states a plan or status. `POST /api/billing` does not exist (405). `GET /api/billing` is read-only.
* `User.plan` (the only value authorization reads) changes in exactly two places: `apply_verified_billing_event` (after a
  signature-verified webhook) and `reconcile_lapsed_subscriptions` (which can only move an account **down** to free).
* A webhook is tied to a user **only** through (a) the `reference` of a `CheckoutSession` QuerySure created, or (b) a
  provider subscription / customer id already stored. A `user_id`, e-mail or plan in a payload is never used for identity.
* Two accounts cannot share a provider subscription id (`rejected: subscription_owned_by_another_account`).

## Flow

```
Browser            QuerySure                              Provider
  | POST /api/billing/checkout {plan:"pro"}                  |
  |------------------>| create CheckoutSession(id=R)         |
  |                   | adapter.create_checkout_session(R)   |
  |<------------------| {checkout_url}  (plan unchanged)     |
  | redirect ----------------------------------------------->| user pays
  |                   |<--- signed webhook: subscription.updated {reference:R, status:active, ...}
  |                   | verify signature -> ledger claim -> apply -> User.plan = pro
  |<-- provider redirects to /?billing=return                 |
  | UI polls /api/billing + /api/account/usage (<= 60 s) until the plan flips, else says "still processing"
```

## Generic adapter wire format

Header `X-Signature: t=<unix seconds>,v1=<hex>[,v1=<hex>...]`, where `v1 = HMAC_SHA256(secret, "<t>." + raw_body)`.
Accepted only if `|now - t| <= BILLING_WEBHOOK_TOLERANCE_SECONDS` (default 300) and some `v1` matches some secret in
`PAYMENT_WEBHOOK_SECRETS` (several allowed -> rotate: add new, switch the provider, remove old).

```json
{"id": "evt_1", "type": "subscription.updated", "created": 1700000000,
 "data": {"reference": "<checkout id>", "customer_id": "cus_1", "subscription_id": "sub_1", "plan": "pro",
          "status": "active", "current_period_start": 1700000000, "current_period_end": 1702592000,
          "cancel_at_period_end": false}}
```

Types: `checkout.completed`, `subscription.created|updated|canceled`, `payment.succeeded|failed|refunded` (payments carry
`payment_id`, `subscription_id`, `amount_minor`, `currency`). Anything else is acknowledged and ignored. Statuses:
`inactive | active | past_due | canceled`. Checkout links are signed with `PAYMENT_CHECKOUT_SIGNING_SECRET`
(`sig` = HMAC-SHA256 over the sorted query parameters) so a hosted page can detect a tampered plan or reference. The link
contains the opaque reference, plan, customer pseudonym and expiry; **no e-mail address**.

## Processing guarantees

| Situation | Behaviour |
|---|---|
| Missing / malformed / mismatched / stale signature | `400`, nothing stored |
| Replay of a captured request | rejected by the timestamp window |
| Same event id delivered again (provider retry) | `200 duplicate`, no state change (`UNIQUE(provider, event_id)`) |
| Event older than the newest applied one for that subscription | `200 stale`, ignored |
| Same timestamp, different status | the more restrictive status wins (canceled > past_due > active > inactive) |
| Late `canceled` for an *old* subscription while a *newer* one is active | ignored (`other_subscription`) |
| Event arrives before the one that links it to an account | `503` + `Retry-After` for `BILLING_UNRESOLVED_RETRY_HOURS` (default 24), then `200 ignored` |
| Internal error while applying | whole change rolled back, ledger row `failed`, `500` -> provider retries -> processed |
| Worker died holding a claim | reclaimable after a 5-minute lease |
| Valid signature, malformed event | `400` (loud on the provider dashboard) |
| `team` / `business` / unknown plan | `rejected`, plan unchanged |

`past_due` and `active` entitle Pro until `current_period_end + BILLING_GRACE_HOURS` (default 72 h). A `past_due` row with no
known period end entitles nothing. `canceled` / `inactive` entitle nothing.

## State

* `subscriptions` (one per user): plan, status, provider ids, period, `cancel_at_period_end`, `provider_event_at` (ordering).
* `billing_events`: idempotency + audit ledger. Stores a SHA-256 of the payload, **not** the payload.
* `payments`: succeeded / failed / refunded, amount in minor units, currency. No card data. Payments never change entitlement.
* `checkout_sessions`: the opaque references handed to the provider.
* On account deletion payments and ledger rows are kept with the user reference removed (accounting); everything else is deleted.

## Operations

* Run `python scripts/maintenance.py` every 30 minutes (the Render blueprint defines `querysure-maintenance`). It downgrades
  subscriptions whose period + grace ended without a renewal event (**fail closed**), expires old checkouts, purges dead sessions.
  Without it a missing renewal event leaves the user on Pro indefinitely.
* Stuck events: `SELECT provider, event_id, status, outcome, attempts FROM billing_events WHERE status IN ('failed','deferred')`.
  `failed`/`deferred` rows are re-processed when the provider redelivers (use the provider's "resend" tool).
* Structured log events: `billing_webhook` (outcome) and `billing_webhook_failed`; error counter `qs_errors_window{component="billing",type="bad_signature|invalid_event|webhook_processing_failed"}`.
  No payloads, signatures or secrets are logged.
* Support cannot see customers' prompts, SQL or data; billing tables contain none.

## Adding a provider

1. Subclass `PaymentProvider` (six abstract members; the base class has no behaviour).
2. `verify_event(payload, signature)` must authenticate with the provider's own scheme and return a `VerifiedPaymentEvent`
   (raise `WebhookVerificationError` / `InvalidEventError`). Map provider price ids to plan keys with `BILLING_PLAN_MAP_JSON`.
   Set `signature_header` to the provider's header name.
3. `create_checkout_session(customer_id, plan, reference=, return_url=, expires_at=)` must pass `reference` through so it is
   echoed back in webhooks, and return `CheckoutResult(url, provider_session_id)`.
4. Implement `cancel_subscription` if account deletion should cancel automatically; otherwise users must cancel in the
   portal first (deletion returns `409 SUBSCRIPTION_ACTIVE`).
5. `register_provider("name", factory)` at import time and set `PAYMENT_PROVIDER=name`.
6. Prove it against the provider's sandbox: signature, duplicate, out-of-order, cancel, failed payment. The existing
   `tests/test_sprint5_billing.py` is the template.

## Known limits

Revenue correctness depends on the provider delivering webhooks and on the maintenance job running. There is no
reconciliation against the provider's API (`get_subscription` is part of the contract but unused by QuerySure).
