# Product backlog

The open work, in priority order, plus a summary of what was delivered. Delivered stories with their acceptance criteria and evidence are in [SPRINT_HISTORY.md](../SPRINT_HISTORY.md).

**Priority:** P0 = security, data loss or launch blocker; P1 = core value; P2 = improvement. **Size:** S (≤ 1 day), M (2–4 days), L (a sprint or more). Sizes are estimates.

## Open items

| ID | User story | Acceptance criteria | Pri | Size | Status |
|---|---|---|---|---|---|
| B-04 | As an operator, I want the demo's database and Key Value store on paid plans, so the data isn't deleted every 30 days | Database and Key Value on paid plans; a backup restored once | P0 | S | Open. The free database expired on 2026-11-25 and was replaced |
| B-13 | As an operator, I want datasets and reports in S3, so they survive restarts and redeploys | `S3_*` settings on the deployed service; an upload survives a redeploy | P0 | S | Open |
| B-12 | As a new user, I want the verification e-mail in my inbox, not spam | Sender on a verified domain (SPF/DKIM); a test mail lands in the inbox | P0 | S | Open. Sending from a @gmail.com address lands in spam |
| B-01 | As the product owner, I want a full live evaluation, so I can quote accuracy with confidence | `scripts/live_quality.sh` completes on the 62-case benchmark and the 5-file suite; exact and semantic accuracy, hallucination rate and cost per question published | P0 | M | Open. Blocked by the free quota |
| B-08 | As a user, I want the "Low confidence" label to mean something on real model output | Threshold recalibrated on the live results from B-01 | P1 | S | Open, after B-01 |
| B-09 | As a user asking in French, German, Spanish or Hindi, I want my words matched to the data's English values | "ventes" and "Umsatz" match "Sales"; the multi-language live cases improve | P1 | M | Open. Main cause of wrong live answers |
| B-06 | As a user, I want to be asked what I meant when my question is ambiguous | Ambiguity already detected in grounding now produces a question instead of a guess | P1 | M | Open |
| B-10 | As a reviewer, I want two-table answers independently verified too | Reference recalculation for simple two-table joins; a wrong join is caught | P1 | L | Open |
| B-07 | As a security reviewer, I want the session token out of `localStorage` | HttpOnly cookie with CSRF protection; uploaded SQLite opened with `trusted_schema=OFF` | P1 | M | Open |
| B-02 | As a maintainer, I want the browser and PostgreSQL test suites run on every change | Both run in CI (PostgreSQL already does); the Compose worker reports healthy | P2 | S | Partly done: CI runs PostgreSQL tests; browser tests run locally with `CHROME_PATH` |
| B-05 | As an operator, I want alerts and recovery exercised for real | Real Sentry, alert webhook and S3 restore tested; capacity measured on multi-core hardware | P2 | M | Open |
| B-03 | As the business, I want to sell the Pro plan | A real payment adapter, tested against the provider's sandbox | P2 | L | Open. The webhook engine exists |
| B-11 | As a user, I want reworded questions to find the right document | Revisit embeddings only if live misses trace back to rewording (decision rule in `evaluation/platform/rag.py`) | P2 | M | Parked by decision |
| B-14 | As a user, I want MFA on my account | TOTP second factor | P2 | M | Open |

## Delivered, by epic

| Epic | What was delivered | Sprints |
|---|---|---|
| E1 Text-to-SQL pipeline | Grounding, planning, SQL generation with up to 2 repairs, validation, read-only execution, independent verification, answer sentence | MVP, 6, 7 |
| E2 Platform and deployment | Config guards, 25 migrations, Docker, Compose, Render blueprint, release packaging | 1, 5, 6, 7 |
| E3 Background work | Jobs, RQ worker, leases, admission control | 2, 3, 4 |
| E4 AI control plane | Structured output, prompt-injection defence, budgets, circuit breaker, provider fallback, call trace | 3, 4, 6, 7, P2 |
| E5 Observability | Redacted logs, metrics, alerts, health checks, backups, load tests | 4 |
| E6 Accounts and billing | Auth, revocable sessions, export and delete, plans and quotas, webhook billing engine | MVP, 5 |
| E7 Quality and evaluation | Test suite, CI, benchmark and evaluation harnesses, docs | All |
| E8 Team platform | Workspaces, roles, audit trail, connectors, sync, scheduler, data quality | P0–P6 |
| E9 AI features | Knowledge hub, assistant, agent with approvals, investigations, AI usage report | P7–P11 |
| E10 Launch and UX | Overview, route-auth matrix, launch checklist, rehearsal, journey redesign | P12, P13, redesign |

"P*n*" means AI Operations Platform Sprint *n*.
