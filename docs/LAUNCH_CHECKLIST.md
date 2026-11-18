# Launch checklist

Run top to bottom before pointing real users at a deployment. Every gate has a command and a pass condition; tick it only when the condition holds.
Environment variables are described in [ENVIRONMENT.md](ENVIRONMENT.md); procedures in [OPERATIONS.md](OPERATIONS.md).

## 1. Build gates (CI runs these; run them locally before tagging)

| Gate | Command | Pass when |
|---|---|---|
| Lint | `ruff check .` | no findings |
| Dependencies | `pip-audit -r requirements.txt` | no known vulnerabilities |
| Tests (SQLite) | `pytest -q` | 0 failed |
| Tests (PostgreSQL + MySQL sources) | set `DATABASE_URL`, `TEST_DATABASE_URL`, `TEST_SOURCE_POSTGRES_URL`, `TEST_SOURCE_MYSQL_URL`, then `pytest -q` | 0 failed; skips only for browser stack |
| Route authentication matrix | `pytest tests/test_s12_hardening.py -q` | every `/api` route rejects anonymous requests except the reviewed public list |
| Safety-adversarial suite | `python -m evaluation.platform --suite safety` | exit 0, 36/36 blocked |
| Retrieval baseline | `python -m evaluation.platform --suite retrieval` | keyword recall@3 >= 0.9, paraphrase >= 0.65, off-topic false positives <= 0.25 |
| Migrations | `alembic upgrade head && alembic check` on a copy of production data, then `alembic downgrade -1 && alembic upgrade head` | exit 0 each time |
| Image | `docker build .` then `docker run --rm --user 10001 --entrypoint python <image> -c "import api"` | builds, imports as the non-root user |
| Browser E2E | `pytest tests/test_sprint5_e2e.py -q` (needs `npm ci`) | 2 passed |

## 2. Production configuration (the app refuses to start in production without the first four, and without `EMAIL_PROVIDER`)

- [ ] `ENVIRONMENT=production`, `JWT_SECRET_KEY` (32+ chars), PostgreSQL `DATABASE_URL`, `REDIS_URL`, `TRUSTED_PROXY_IPS`
- [ ] `CONNECTIONS_ENCRYPTION_KEY` set to a Fernet key (connection passwords are encrypted with it; without it creating a connection fails, and losing it makes stored connections unusable; rotate with the documented procedure)
- [ ] `EMAIL_PROVIDER` set to `smtp`, `brevo` or `resend`
- [ ] S3 storage configured (datasets, reports, upload staging); bucket private, versioning on
- [ ] An LLM key for the server (`GROQ_API_KEY` or another provider); user-supplied keys are ignored in production by design
- [ ] `ADMIN_EMAILS` lists the operators who may open `/ops`
- [ ] Email provider configured (verification and reset mails) and a test mail received
- [ ] Limits reviewed for your plan: `KNOWLEDGE_SEARCH_MAX_CHUNKS` (25000), `INVESTIGATION_MAX_TABLE_ROWS` (500000), `AI_*` budgets, `AGENT_MAX_STEPS`
- [ ] Kill switches known to the on-call: `AI_KILL_SWITCH`, `AGENT_ENABLED`, `QUALITY_ENABLED`

## 3. Processes

- [ ] `api` replicas healthy on `/api/health`; `/api/ready` shows no critical component down
- [ ] One `worker` (background jobs) and one `scheduler` running (`docker compose ps` shows both healthy; the scheduler heartbeat file is touched every tick)
- [ ] Migrations applied before the new image takes traffic (`alembic upgrade head`)

## 4. Smoke test on the deployed URL (use a throw-away account)

1. Register, verify the email, sign in.
2. Upload a small CSV; ask a question; the answer shows SQL, validation and a reliability score.
3. Explore: the dataset has a quality status; Overview loads; run an analysis on it in Analyze; download the HTML export.
4. Upload a short `.md` document; ask the Assistant a question it answers; delete the document.
5. Ask the Agent to "list my datasets"; ask it to delete the document and confirm it waits for approval, then reject.
6. As admin: AI usage card shows the calls just made.
7. Delete the throw-away account (Settings) and confirm its data is gone.

## 5. Backups and recovery (see OPERATIONS.md section 10)

- [ ] Postgres backup taken and a **restore rehearsed** into a scratch database in the last 30 days (a backup never restored is not a backup)
- [ ] S3 versioning/lifecycle verified; report and dataset objects readable after restore
- [ ] Rollback plan written: previous image tag, and that migrations are additive (downgrade drops only the newest tables, losing their data)

## 6. Known limits to tell users (also in the README)

- Knowledge search is lexical (TF-IDF); paraphrase-heavy questions miss about 28% of the time in the evaluation (`docs/baseline/platform_eval.txt`). Workspaces are capped at 25,000 passages.
- Citations are checked for existence, not factual support; answers can still be wrong.
- The agent has six tools; it cannot run SQL, shell or HTTP. Write actions need a person's approval.
- Investigations describe data; hypotheses are unverified; tables over 500,000 rows are refused.
- Live LLM quality (SQL, plans, answers) has not been measured in this repository's CI: it needs funded provider keys.

## 7. Production-mode rehearsal (Sprint 13)

`scripts/launch_rehearsal.sh` runs gates 2-5 above on one machine, with Docker only, and cleans up after itself (own compose project, own volumes, throw-away credentials):
image build; services started with `ENVIRONMENT=production` (so the production guards, the migration step in the image's start command and the S3 requirement are all exercised, with MinIO as the S3-compatible store); health and readiness; `scripts/launch_smoke.py` against the API (register, login, upload a CSV, overview, investigation and its HTML export, knowledge upload and search, delete, delete account); `pg_backup.sh`; `pg_restore_verify.sh` into a scratch database; `alembic current` at head and `alembic check` clean; restart of api, worker and scheduler; data still present.

**Last run: passed** (2026-11-07, macOS, Docker, 66 s after the image was cached). What it does **not** prove, and what you still must do on the real target:
- E-mail delivery: the rehearsal has no mail server, so verification is done directly in the database. Send a real verification mail.
- Anything an LLM does (SQL generation, Assistant answers, Agent plans): no provider key is used. Run `scripts/live_quality.sh`.
- Cloud networking, TLS termination, a managed PostgreSQL, real S3, real load. Re-run the smoke test against the deployed URL: `python scripts/launch_smoke.py https://your-host --mail-cmd "<command that prints the mailbox>"`.
